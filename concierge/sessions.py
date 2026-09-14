"""Every Claude process on the machine, and whether it is still working.

WHY THIS EXISTS
---------------
`reaper.py` answers one narrow question well: may the concierge close the tmux
window of a job it started? It reads `state/jobs.json`, so a session Bosire
opened by hand in a terminal is invisible to it, and always will be.

The RAM is not. On 2026-09-04 the box was holding four hand-started sessions
and two finished jobs, and the finished ones alone were ~700 MB before their
MCP children are counted. He is often on his phone with no terminal open at
all, which is the case where nothing on this machine tells him any of that.

So this module answers the wider question — what claude is running here, whose
is it, is it busy, and how much is it holding — from three sources that each
know something the others do not:

  /proc      every process, its RSS, its CPU counter, its cwd. The only
             source that sees a session nobody registered.
  tmux       which pane a session lives in, and what its UI is drawing right
             now. The pane text is the session's own account of itself.
  registry   the concierge's job rows: id, title, and the status the job
             reported. Authoritative for jobs, silent about everything else.

WHAT "RUNNING" MEANS HERE
-------------------------
Getting this wrong in the direction of "idle" is the expensive mistake: he
kills something mid-turn from his phone and loses the work. So nothing is
called idle on one signal, and anything unmeasured is called `unknown` and left
alone. See `classify` — every branch names the evidence it used, and that
sentence is shown in the UI rather than kept for the log.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from concierge import config, registry, tmuxctl

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")

# /proc/<pid>/stat fields, 1-based as in proc(5), measured after comm.
PPID_FIELD = 4
UTIME_FIELD = 14
STIME_FIELD = 15
STARTTIME_FIELD = 22

# The same threshold reaper.py uses, for the same reason and off the same
# measurements. Re-checked on this machine 2026-09-04 over a 30-second window:
# a working session sat at 4.53 ticks/s, four idle ones at 0.80-1.43. See the
# long comment on reaper.BUSY_TICKS_PER_SECOND for why "counter unchanged" —
# the obvious test — is useless here: an idle Claude REPL is never at zero.
BUSY_TICKS_PER_SECOND = 2.0

# A transcript write this recent means the session was doing something a moment
# ago. Only ever used to say "busy", never "idle": a single long model turn
# writes nothing for minutes, so silence here proves nothing.
TRANSCRIPT_BUSY_SECONDS = 45.0

# Subcommands that are infrastructure rather than a conversation. None of these
# is a session anyone would want to reap, and `daemon` in particular is shared.
HELPER_SUBCOMMANDS = frozenset(
    {
        "daemon",
        "bg-spare",
        "bg-pty-host",
        "mcp",
        "update",
        "doctor",
        "install",
        "migrate-installer",
        "setup-token",
        "plugin",
        "config",
    }
)

# claude-mem and friends shell out to `claude --no-session-persistence` to
# summarise things. Those are throwaway, live for seconds, and are nobody's
# conversation — but they are 200 MB+ each while they last, so they are counted
# in the totals rather than hidden.
THROWAWAY_FLAG = "--no-session-persistence"

CLAUDE_BINS = frozenset({"claude", "claude.exe"})

# Claude Code draws one status line just above the input box. Working, it is a
# verb with an ellipsis and a live elapsed timer in brackets:
#     ✽ Twisting… (3m 52s · ↓ 12.0k tokens · thought for 1s)
# Finished, the brackets are gone and it names the wall-clock time it stopped:
#     ✻ Cooked for 11m 33s · done 9:20 AM
# Both captured from real panes on 2026-09-04. This is the session's own
# account of itself and is the single most direct signal available — but the
# wording is Claude Code's and changes between releases, so it corroborates the
# CPU rate rather than replacing it.
PANE_BUSY = re.compile(r"\((?:\d+h\s*)?(?:\d+m\s*)?\d+s\s*[·)]")
PANE_DONE = re.compile(r"·\s*done\s+\d{1,2}:\d{2}")
PANE_INTERRUPT = "esc to interrupt"

# Where a live session keeps its scratch directory. The path carries the
# session's own uuid, which is otherwise nowhere in the process table, and that
# uuid is the key to its transcript — so this fd is how a bare `claude` in a
# terminal gets a name instead of a pid.
SCRATCH_ROOT = Path(f"/tmp/claude-{os.getuid()}")
SCRATCH_FD = re.compile(rf"^{re.escape(str(SCRATCH_ROOT))}/([^/]+)/([0-9a-f-]{{36}})/")

# The footer Claude Code draws under the input box carries the session id:
#   tasks | Opus 5 (1M context) | ctx 6% | main synced | 69a210da-a26b-…
# Truncated on a narrow pane, so this only fires when the whole id is on
# screen — which is the normal case at any usable width.
UUID_IN_TEXT = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")

TRANSCRIPTS = Path.home() / ".claude" / "projects"


def slug_for(cwd: str | None) -> str | None:
    """The transcript directory name Claude Code derives from a cwd.

    Every separator becomes a dash, leading slash included — so
    /home/bosire/projects/personal/tasks is -home-bosire-projects-personal-tasks.
    """
    if not cwd:
        return None
    return cwd.replace("/", "-")


def transcript_near(cwd: str | None, started_at: float, *, window: float = 120.0) -> Path | None:
    """The transcript of the session that started in `cwd` at `started_at`.

    The last resort, for a session with no scratch fd and no readable pane. It
    answers ONLY when exactly one transcript in that project began inside the
    window — two candidates means a guess, and a row labelled with the wrong
    conversation is worse than a row labelled by its pid.
    """
    slug = slug_for(cwd)
    if not slug:
        return None
    folder = TRANSCRIPTS / slug
    if not folder.is_dir():
        return None
    hits = []
    for path in folder.glob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < started_at:
            continue
        # st_ctime moves on every append, so it is useless here; the first
        # line's own timestamp is when the conversation actually opened.
        opened = _opened_at(path)
        if opened is not None and abs(opened - started_at) <= window:
            hits.append(path)
    return hits[0] if len(hits) == 1 else None


_OPENED: dict[str, float | None] = {}


def _opened_at(path: Path) -> float | None:
    """Epoch seconds of a transcript's first timestamped line. Cached — the
    first line of an append-only file never changes."""
    key = str(path)
    if key in _OPENED:
        return _OPENED[key]
    value = None
    try:
        with path.open("rb") as handle:
            for _ in range(5):
                line = handle.readline()
                if not line:
                    break
                try:
                    stamp = json.loads(line).get("timestamp")
                except ValueError:
                    continue
                if isinstance(stamp, str):
                    from datetime import datetime

                    try:
                        value = datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        value = None
                    break
    except OSError:
        value = None
    _OPENED[key] = value
    return value


# --- /proc ------------------------------------------------------------------


@dataclass
class Proc:
    pid: int
    ppid: int
    argv: tuple[str, ...]
    rss_kb: int
    ticks: int
    starttime: int
    cwd: str | None = None

    @property
    def cmdline(self) -> str:
        return " ".join(self.argv)


def _stat_fields(pid: int) -> list[str] | None:
    """Everything after comm in /proc/<pid>/stat.

    Split after the LAST ')': comm is parenthesised and may itself contain
    spaces and brackets, so naive whitespace splitting misreads every field
    after it. Same trick, same reason, as reaper._proc_field.
    """
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    except OSError:
        return None


def _field(fields: list[str], index: int) -> int:
    offset = index - 3  # fields[0] is field 3 (state)
    try:
        return int(fields[offset])
    except (IndexError, ValueError):
        return 0


def read_proc(pid: int) -> Proc | None:
    """One process, or None if it went away while we were reading it.

    Everything here is best-effort by design: /proc is a race by construction
    and a scan that raises on a process exiting mid-sweep would fail exactly
    when the machine is busiest.
    """
    fields = _stat_fields(pid)
    if fields is None:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    argv = tuple(part for part in raw.decode("utf-8", "replace").split("\0") if part)
    if not argv:
        return None  # a kernel thread
    try:
        # statm field 2 is resident pages — one small read rather than parsing
        # the 50-line /proc/<pid>/status for the same number.
        resident = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
    except (OSError, IndexError, ValueError):
        resident = 0
    return Proc(
        pid=pid,
        ppid=_field(fields, PPID_FIELD),
        argv=argv,
        rss_kb=resident * PAGE_SIZE // 1024,
        ticks=_field(fields, UTIME_FIELD) + _field(fields, STIME_FIELD),
        starttime=_field(fields, STARTTIME_FIELD),
        cwd=_readlink(f"/proc/{pid}/cwd"),
    )


def _readlink(path: str) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def scan() -> dict[int, Proc]:
    """Every process this user can see, by pid."""
    procs: dict[int, Proc] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        proc = read_proc(int(entry.name))
        if proc is not None:
            procs[proc.pid] = proc
    return procs


def boot_time() -> float:
    """Epoch seconds at boot, for turning a starttime into an age."""
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return 0.0


def session_uuid(pid: int) -> tuple[str, str] | None:
    """(project slug, session uuid) from the scratch directory the session holds
    open, or None.

    Present for every ordinary `claude` session — the concierge's own, jobs,
    and anything started in a terminal — and absent for `claude.exe --print`
    sessions driven over Remote Control, which take a different code path.
    Absence therefore means "no name available", never "not a session".
    """
    try:
        entries = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None
    for name in entries:
        target = _readlink(f"/proc/{pid}/fd/{name}")
        if not target:
            continue
        match = SCRATCH_FD.match(target + "/")
        if match:
            return match.group(1), match.group(2)
    return None


# --- what kind of thing is this ---------------------------------------------


def subcommand(argv: tuple[str, ...]) -> str | None:
    """The first positional argument, which is claude's subcommand if any.

    A brief is also positional (`claude '<brief>'`), so this only means
    anything against the known subcommand list.
    """
    for arg in argv[1:]:
        if not arg.startswith("-"):
            return arg
        if "=" not in arg and arg.startswith("--"):
            # A flag that takes a separate value would swallow the next token;
            # we cannot know which flags those are, so stop at the first flag
            # and rely on subcommands coming first, which they do.
            return None
    return None


def is_claude(proc: Proc) -> bool:
    return Path(proc.argv[0]).name in CLAUDE_BINS


def kind_of(proc: Proc) -> str:
    """`server`, `helper` or `session`."""
    sub = subcommand(proc.argv)
    if sub == "remote-control":
        return "server"
    if sub in HELPER_SUBCOMMANDS:
        return "helper"
    if THROWAWAY_FLAG in proc.argv:
        return "helper"
    return "session"


def cloud_session_id(argv: tuple[str, ...]) -> str | None:
    """The `cse_…` id of a session started from the phone, if this is one.

    These come down a different path from everything else — `claude.exe
    --print --sdk-url …`, spawned by the Remote Control server — and leave no
    transcript under ~/.claude/projects and no scratch fd, so this argument is
    the only name they have.
    """
    for i, arg in enumerate(argv):
        if arg == "--session-id" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--session-id="):
            return arg.split("=", 1)[1]
    return None


def is_concierge(proc: Proc) -> bool:
    """The concierge's own session — the one thing that must never be killed.

    Two independent marks, because either alone can be absent: the
    `--remote-control concierge` pair, and the rendered system prompt only the
    concierge is ever started with. Matching either is deliberate; a false
    positive costs a row he cannot reap, a false negative costs the concierge.
    """
    argv = list(proc.argv)
    for i, arg in enumerate(argv[:-1]):
        if arg == "--remote-control" and argv[i + 1] == "concierge":
            return True
    return any("concierge.rendered.md" in arg for arg in argv)


# --- tmux -------------------------------------------------------------------


@dataclass(frozen=True)
class Pane:
    pane_pid: int
    session: str
    window_index: str
    window_name: str
    pane_id: str

    @property
    def label(self) -> str:
        return f"{self.session}:{self.window_name}"


def panes(runner=None) -> list[Pane]:
    """Every pane on the tmux server, or [] when there is no server."""
    runner = runner or tmuxctl._run
    result = runner(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_pid}\t#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}",
        ]
    )
    if result.returncode != 0:
        return []
    found = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and parts[0].isdigit():
            found.append(Pane(int(parts[0]), parts[1], parts[2], parts[3], parts[4]))
    return found


def pane_read(pane_id: str, runner=None) -> tuple[str | None, str | None]:
    """(state, session uuid) from one capture of the pane.

    State is `busy`, `idle`, or None when the pane says nothing either way —
    see the PANE_BUSY / PANE_DONE comments. The LAST matching line wins: the
    status line is always the lowest one on screen, and older completed turns
    are still scrolled above it.

    The uuid comes from the same text. Claude Code prints it in the footer
    beside the model and the git state, which makes it the one identity source
    that works for every session in a tmux pane — including the ones with no
    scratch fd to read it from. Two reads for the price of one subprocess,
    which matters at one capture per pane every five seconds.
    """
    runner = runner or tmuxctl._run
    result = runner(["tmux", "capture-pane", "-p", "-t", pane_id])
    if result.returncode != 0:
        return None, None
    text = result.stdout or ""
    match = UUID_IN_TEXT.search(text)
    uuid = match.group(0) if match else None
    if PANE_INTERRUPT in text:
        return "busy", uuid
    verdict = None
    for line in text.splitlines():
        if PANE_DONE.search(line):
            verdict = "idle"
        elif PANE_BUSY.search(line):
            verdict = "busy"
    return verdict, uuid


def pane_state(pane_id: str, runner=None) -> str | None:
    """Just the state, for callers that do not need the id."""
    return pane_read(pane_id, runner)[0]


# --- transcripts -------------------------------------------------------------


def transcript_path(slug: str, uuid: str) -> Path | None:
    candidate = TRANSCRIPTS / slug / f"{uuid}.jsonl"
    return candidate if candidate.exists() else None


_TITLES: dict[str, str] = {}


def transcript_title(path: Path) -> str:
    """The first thing a human said to this session, as its name.

    Cached forever: it is the first line of a file that only ever gets appended
    to, and re-reading a 5 MB transcript on every 5-second poll would make the
    dashboard the busiest thing on the box.
    """
    key = str(path)
    if key in _TITLES:
        return _TITLES[key]
    title = ""
    try:
        with path.open("rb") as handle:
            for _ in range(60):
                line = handle.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "user":
                    continue
                content = (row.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                    )
                if not isinstance(content, str):
                    continue
                text = content.strip()
                # Skip the harness's own opening injections — hook output,
                # system reminders, resumed-session banners — which are user
                # rows carrying no user in them.
                if not text or text.startswith(("<", "Caveat:", "[Request")):
                    continue
                title = " ".join(text.split())[:120]
                break
    except OSError:
        pass
    _TITLES[key] = title
    return title


# --- the state decision ------------------------------------------------------


@dataclass
class Session:
    pid: int
    kind: str
    protected: bool
    state: str
    why: str
    rss_self_kb: int
    rss_tree_kb: int
    cwd: str | None
    title: str
    job_id: str | None
    job_status: str | None
    tmux: str | None
    tmux_target: str | None
    uuid: str | None
    cpu_rate: float | None
    transcript_age: float | None
    age_seconds: float
    starttime: int
    children: list[int] = field(default_factory=list)

    @property
    def reapable(self) -> bool:
        return not self.protected and self.state in ("idle", "finished")

    def as_dict(self) -> dict:
        return {
            "pid": self.pid,
            "kind": self.kind,
            "protected": self.protected,
            "state": self.state,
            "why": self.why,
            "reapable": self.reapable,
            "rssSelfMb": round(self.rss_self_kb / 1024),
            "rssTreeMb": round(self.rss_tree_kb / 1024),
            "cwd": self.cwd,
            "where": _short_path(self.cwd),
            "title": self.title,
            "jobId": self.job_id,
            "jobStatus": self.job_status,
            "tmux": self.tmux,
            "tmuxTarget": self.tmux_target,
            "uuid": self.uuid,
            "cpuRate": None if self.cpu_rate is None else round(self.cpu_rate, 2),
            "transcriptAge": (
                None if self.transcript_age is None else round(self.transcript_age)
            ),
            "ageSeconds": round(self.age_seconds),
            "fingerprint": self.starttime,
            "childCount": len(self.children),
        }


def _short_path(path: str | None) -> str:
    """Enough of the cwd to recognise it on a phone, without the whole path."""
    if not path:
        return "?"
    parts = [part for part in Path(path).parts if part != "/"]
    if not parts:
        return path
    return "/".join(parts[-2:])


def classify(
    *,
    protected: bool,
    cpu_rate: float | None,
    pane: str | None,
    job_status: str | None,
    transcript_age: float | None,
) -> tuple[str, str]:
    """One session's state and the sentence that justifies it.

    Pure, and ordered busy-first on purpose. Every branch that can say
    "running" is checked before any branch that can say "idle", so a session
    that looks busy to ANY source is never offered as reapable — the mistake
    that matters is the other one.
    """
    if job_status == "running":
        return "running", "the concierge has this job registered as running"
    if job_status == "waiting":
        return "waiting", "blocked on you — it asked a question and is holding it"
    if pane == "busy":
        return "running", "its pane is drawing a live turn timer"
    if cpu_rate is not None and cpu_rate > BUSY_TICKS_PER_SECOND:
        return "running", f"CPU at {cpu_rate:.1f} ticks/s (idle sits under 1.5)"
    if transcript_age is not None and transcript_age < TRANSCRIPT_BUSY_SECONDS:
        return "running", f"wrote to its transcript {transcript_age:.0f}s ago"

    # Nothing says busy. That is not the same as knowing it is idle.
    if cpu_rate is None:
        return "unknown", "no CPU sample yet — give it half a minute"
    if protected:
        return "idle", f"idle at {cpu_rate:.1f} ticks/s"
    if job_status in config.FINISHED_STATUSES:
        return "finished", f"reported {job_status}, idle at {cpu_rate:.1f} ticks/s"
    if job_status in config.CLOSED_STATUSES:
        return "finished", f"row is {job_status}, idle at {cpu_rate:.1f} ticks/s"
    detail = f"idle at {cpu_rate:.1f} ticks/s"
    if pane == "idle":
        detail += ", pane shows a finished turn"
    if transcript_age is not None:
        detail += f", last wrote {_ago(transcript_age)} ago"
    return "idle", detail


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


# --- putting it together ------------------------------------------------------


def owners(procs: dict[int, Proc], claude_pids: set[int]) -> dict[int, int]:
    """Which Claude process each process on the box belongs to.

    The MCP servers are the point. A session's own process is ~350 MB, but it
    holds a node process per MCP server open beside it — hevy, ynab,
    sequential-thinking, playwright, claude-mem — and on this machine those add
    as much again. Reaping the session takes all of them with it, so the tree
    is the number that answers "where is my memory going".

    Ownership is the NEAREST Claude ancestor, inclusive, which is what stops
    the Remote Control server from claiming the RAM of the session it spawned:
    that session is a Claude process itself, so it owns its own subtree and the
    server's number is only the server. Every process therefore has exactly one
    owner and nothing is counted twice.

    RSS double-counts pages shared between parent and child, so a tree total is
    an over-estimate. It is the number `ps` shows and the one he asked for; the
    alternative (PSS, from /proc/<pid>/smaps_rollup) costs a page-table walk
    per process per poll and is not worth it here.
    """
    resolved: dict[int, int] = {}

    def owner_of(pid: int) -> int | None:
        chain: list[int] = []
        cursor = pid
        while True:
            if cursor in resolved:
                found = resolved[cursor]
                break
            if cursor in claude_pids:
                found = cursor
                break
            proc = procs.get(cursor)
            if proc is None or proc.ppid <= 1 or proc.ppid in chain:
                found = None
                break
            chain.append(cursor)
            cursor = proc.ppid
        for step in chain:
            if found is not None:
                resolved[step] = found
        return found

    trees: dict[int, int] = {}
    for pid in procs:
        found = owner_of(pid)
        if found is not None:
            trees[pid] = found
    return trees


def snapshot(
    rates: dict[int, float] | None = None,
    *,
    procs: dict[int, Proc] | None = None,
    jobs: dict[str, dict] | None = None,
    tmux_panes: list[Pane] | None = None,
    pane_reader=pane_read,
    now: float | None = None,
) -> dict:
    """Everything the dashboard shows, in one pass.

    `rates` comes from the sampler in dashboard.py — this module never sleeps,
    so a caller that has not been sampling gets `unknown` rows rather than a
    guess.
    """
    now = now or time.time()
    rates = rates or {}
    procs = procs if procs is not None else scan()
    jobs = jobs if jobs is not None else registry.load()
    tmux_panes = tmux_panes if tmux_panes is not None else panes()
    btime = boot_time()

    claude_procs = [p for p in procs.values() if is_claude(p)]
    owner_by_pid = owners(procs, {p.pid for p in claude_procs})
    owned: dict[int, list[int]] = {}
    for pid, owner in owner_by_pid.items():
        if pid != owner:
            owned.setdefault(owner, []).append(pid)

    def tree_kb(pid: int) -> int:
        return procs[pid].rss_kb + sum(
            procs[child].rss_kb for child in owned.get(pid, ()) if child in procs
        )

    pane_by_pid = {pane.pane_pid: pane for pane in tmux_panes}
    job_by_window = {
        (job.get("tmux_window") or job_id): (job_id, job)
        for job_id, job in jobs.items()
    }

    rows: list[Session] = []
    helper_kb = 0
    helper_count = 0

    for proc in sorted(claude_procs, key=lambda p: -p.rss_kb):
        kind = kind_of(proc)
        if kind == "helper":
            helper_kb += tree_kb(proc.pid)
            helper_count += 1
            continue

        pane = _pane_for(proc, pane_by_pid, procs)
        job_id = job_status = None
        if pane is not None and pane.session == config.TMUX_SESSION:
            match = job_by_window.get(pane.window_name)
            if match:
                job_id, job = match
                job_status = job.get("status")

        pane_verdict, pane_uuid = (
            pane_reader(pane.pane_id) if pane is not None else (None, None)
        )
        started_at = btime + proc.starttime / CLOCK_TICKS
        uuid, transcript = _identify(proc, pane_uuid, started_at)

        transcript_age = None
        if transcript is not None:
            try:
                transcript_age = now - transcript.stat().st_mtime
            except OSError:
                transcript_age = None

        protected = kind == "server" or is_concierge(proc)
        state, why = classify(
            protected=protected,
            cpu_rate=rates.get(proc.pid),
            pane=pane_verdict,
            job_status=job_status,
            transcript_age=transcript_age,
        )

        rows.append(
            Session(
                pid=proc.pid,
                kind="concierge" if is_concierge(proc) else kind,
                protected=protected,
                state=state,
                why=why,
                rss_self_kb=proc.rss_kb,
                rss_tree_kb=tree_kb(proc.pid),
                cwd=proc.cwd,
                title=_title_for(proc, job_id, jobs, transcript, pane),
                job_id=job_id,
                job_status=job_status,
                tmux=pane.label if pane else None,
                tmux_target=f"{pane.session}:{pane.window_index}" if pane else None,
                uuid=uuid,
                cpu_rate=rates.get(proc.pid),
                transcript_age=transcript_age,
                age_seconds=max(0.0, now - started_at),
                starttime=proc.starttime,
                children=owned.get(proc.pid, []),
            )
        )

    # Protected first — he should not have to scroll past his own sessions to
    # be reminded which row is the one that must never go — then heaviest.
    rows.sort(key=lambda s: (not s.protected, -s.rss_tree_kb))
    return {
        "sessions": [row.as_dict() for row in rows],
        "helpers": {"count": helper_count, "rssMb": round(helper_kb / 1024)},
        "totals": {
            "claudeMb": round(
                (sum(row.rss_tree_kb for row in rows) + helper_kb) / 1024
            ),
            "reapableMb": round(
                sum(r.rss_tree_kb for r in rows if r.reapable) / 1024
            ),
            **memory(),
        },
        "at": now,
        "graceMinutes": config.REAPER.finished_grace_minutes,
    }


def _identify(
    proc: Proc, pane_uuid: str | None, started_at: float
) -> tuple[str | None, Path | None]:
    """This session's id and transcript, best source first.

    1. The scratch fd it holds open. Exact, and the only one that works for a
       session outside tmux — but a session that has used no tools has not
       opened one, so it is not universal.
    2. The id printed in its own pane footer. Exact, tmux only.
    3. A transcript in this cwd that opened when the process did, and only when
       there is exactly one such. See `transcript_near` for why that condition
       is not negotiable.
    """
    found = session_uuid(proc.pid)
    if found:
        return found[1], transcript_path(*found)
    if pane_uuid:
        slug = slug_for(proc.cwd)
        return pane_uuid, (transcript_path(slug, pane_uuid) if slug else None)
    transcript = transcript_near(proc.cwd, started_at)
    if transcript is not None:
        return transcript.stem, transcript
    return None, None


def _pane_for(
    proc: Proc, pane_by_pid: dict[int, Pane], procs: dict[int, Proc]
) -> Pane | None:
    """The tmux pane this session is running in, if it is in one.

    Concierge jobs are `exec claude`, so the pane pid IS the claude pid. A
    session started by hand is a child of the pane's shell instead, so walk up
    until a pid matches a pane or we run out of parents.

    The walk stops at another Claude process, which is not a detail: a session
    the Remote Control server spawned would otherwise inherit the SERVER's
    pane, be labelled `rc:0`, and — worse — be judged on a pane that is showing
    the server's log rather than any conversation at all.
    """
    seen, cursor = set(), proc.pid
    while cursor > 1 and cursor not in seen:
        seen.add(cursor)
        if cursor in pane_by_pid:
            return pane_by_pid[cursor]
        current = procs.get(cursor)
        if current is None:
            return None
        parent = procs.get(current.ppid)
        if parent is not None and is_claude(parent):
            return None
        cursor = current.ppid
    return None


def _title_for(
    proc: Proc,
    job_id: str | None,
    jobs: dict[str, dict],
    transcript: Path | None,
    pane: Pane | None,
) -> str:
    """A name he can recognise on a phone, best source first."""
    if job_id and jobs.get(job_id, {}).get("title"):
        return jobs[job_id]["title"]
    if is_concierge(proc):
        return "the concierge itself"
    if subcommand(proc.argv) == "remote-control":
        return "Remote Control server (this machine in the Claude app)"
    cloud = cloud_session_id(proc.argv)
    if cloud:
        return f"started from the Claude app · {cloud}"
    if transcript is not None:
        title = transcript_title(transcript)
        if title:
            return title
    if pane is not None:
        return f"tmux {pane.label}"
    return " ".join(proc.argv[:3])[:120]


def memory() -> dict:
    """Free and total RAM, because that is the number he is actually watching."""
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                values[key] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        return {}
    return {
        "memTotalMb": round(values.get("MemTotal", 0) / 1024),
        "memAvailableMb": round(values.get("MemAvailable", 0) / 1024),
    }


# --- reaping -----------------------------------------------------------------


class RefusedError(RuntimeError):
    """The reap was not safe to do, and nothing was killed."""


def reap(
    pid: int,
    fingerprint: int,
    *,
    rates: dict[int, float] | None = None,
    tmux=tmuxctl,
    wait=time.sleep,
) -> str:
    """Close one session, after re-deciding from scratch that it may be closed.

    The UI's own judgement is not trusted here, and neither is its pid. A page
    left open on a phone overnight is showing a snapshot of a machine that has
    moved on, and pids get reused — so the row is re-derived now, and the
    process's start time (which a recycled pid cannot reproduce) has to match
    what the page was looking at.

    A concierge job goes out the way the reaper closes one — kill the window,
    mark the row `reaped` — so the registry stays true and `respawn <id>` still
    works. Anything else gets a SIGTERM, then a SIGKILL if it is still there.
    """
    view = snapshot(rates)
    row = next((r for r in view["sessions"] if r["pid"] == pid), None)
    if row is None:
        raise RefusedError(f"pid {pid} is no longer a Claude session")
    if row["fingerprint"] != fingerprint:
        raise RefusedError(
            f"pid {pid} is a different process now — reload before reaping"
        )
    if row["protected"]:
        raise RefusedError(f"pid {pid} is protected and is never reaped from here")
    if not row["reapable"]:
        raise RefusedError(f"pid {pid} is {row['state']} — {row['why']}")

    job_id = row["jobId"]
    if job_id:
        window = registry.load().get(job_id, {}).get("tmux_window") or job_id
        tmux.kill_window(config.TMUX_SESSION, window)
        registry.upsert(job_id, touch=False, status="reaped")
        return f"closed {job_id} ({row['rssTreeMb']} MB)"

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return f"pid {pid} was already gone"
    except PermissionError as exc:
        raise RefusedError(f"not allowed to signal pid {pid}") from exc

    for _ in range(20):
        wait(0.25)
        if not Path(f"/proc/{pid}").exists():
            return f"closed pid {pid} ({row['rssTreeMb']} MB)"
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return f"closed pid {pid} the hard way ({row['rssTreeMb']} MB)"
