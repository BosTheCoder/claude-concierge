"""Close the tmux window of a job that has stopped working.

WHY THIS EXISTS
---------------
`claude '<brief>'` does not exit when its turn ends — it drops to an
interactive REPL and sits there. Nothing closed that window until the 7-day
prune in `supervisor.prunable`, and the prune only runs on the branch of
`ensure_up` that had to restart the concierge, so in practice a finished job
lived until someone noticed.

On 2026-08-29 four of them had: G5, S9, W6 and D9 had all reported to Telegram
and gone quiet, the oldest 30 hours earlier. Killing them by hand took Claude
processes from 6.8 GB to 3.5 GB and free memory from 1.0 GB to 2.6 GB on a
13 GB machine.

WHY NOT THE SESSION-REAPER
--------------------------
`win-scheduled-tasks/scripts/session-reaper.py` already sweeps leaked Claude
sessions every 6 hours, and it did not miss these — it spared them, by name,
on every run. `PROTECTED_TMUX = {"concierge", "rc"}` covers every pane in this
tmux session, and that protection has to stay: the session-reaper cannot read
`state/jobs.json`, so it cannot tell a finished job's window from window 0,
which is the concierge itself. Only the registry knows which is which, so the
decision belongs here.

WHAT IT DOES
------------
  A. `killed`/`orphaned`/`respawned`/`reaped` — the row is already over.
     Close the window with no grace and no message.
  B. `done`/`failed` — the job said its last word. Close the window once the
     grace period has passed AND the pane's CPU counter has not moved since the
     previous tick.
  C. `waiting` — blocked on a human. NOT reaped on the same timer. After
     `waiting_nudge_hours` it re-sends the question once; if it is still
     waiting `waiting_grace_hours` after that, it is closed.
  D. `running` — never touched, at any age.

WHAT MAKES IT SAFE
------------------
Four guards, each of which spares the job and says so in the log:

  1. The pane must still be running `claude`. A pane that has fallen back to a
     shell is closed freely; a pane running something else is left alone.
  2. The pane must be burning CPU at an idle rate. Measured 2026-08-29 across
     three real sessions, an idle Claude REPL is not still — it sits at a
     remarkably consistent 0.80-0.84 ticks/sec of utime+stime, while a session
     actually working measured 4.18. So the test is a RATE, not the
     session-reaper's "counter unchanged": that test never passes here and
     would have made the whole reaper a no-op. This costs one tick of lag
     (`ensure-up` runs every five minutes) and catches the job that reported
     `done` and then kept working — committing and pushing after notifying is
     the ordinary case.
  3. The job's task folder must exist and contain a file written at or after
     the job opened. A job whose output exists only in its context is never
     killed; it is left to the 7-day prune instead.
  4. A `waiting` job is never closed without its question being re-sent first.

Reaping is recoverable in a way that killing an arbitrary session is not: the
registry keeps the brief and the task folder, so `respawn <id>` starts the work
again from where it was written down. That is why a grace period measured in
hours is enough, and why the cost of closing one slightly early is one command
rather than lost work.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from concierge import config, registry, tmuxctl

# /proc field numbers, 1-based as in proc(5). Same fields the session-reaper
# uses for the same purpose; see its rule B.
UTIME_FIELD = 14
STIME_FIELD = 15

# Above this many CPU ticks per second, the pane is doing something and is left
# alone. Measured on this machine, 2026-08-29, over a 88-second window:
#
#     M1 (idle, finished)   0.84 ticks/sec
#     T7 (idle, waiting)    0.82
#     Q4 (idle, finished)   0.80
#     F8 (working)          4.18
#
# An idle Claude Code REPL is never at zero — it polls, and it holds an MCP
# stack open. That is why the session-reaper's rule B test ("counter unchanged
# since last run") cannot be reused here: applied to these panes it is never
# true, and the reaper would spare everything forever while reporting success.
# 2.0 sits at 2.4x the idle rate and half the working rate.
BUSY_TICKS_PER_SECOND = 2.0

# A rate is only meaningful over a sane interval. `ensure-up` fires every 5
# minutes; a gap far outside that means the machine was asleep, and averaging a
# burst of real work across ten hours of downtime would read as idle.
MIN_SAMPLE_SECONDS = 30
MAX_SAMPLE_SECONDS = 1800


# --- /proc ------------------------------------------------------------------


def _proc_field(pid: int, index: int) -> str | None:
    """A field of /proc/<pid>/stat, or None if the process is gone.

    Split after the LAST ')': comm is parenthesised and may itself contain
    spaces and brackets, so naive whitespace splitting misreads every field
    after it.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rpartition(")")[2].split()
    offset = index - 3  # fields[0] is field 3 (state)
    return fields[offset] if 0 <= offset < len(fields) else None


def cpu_ticks(pid: int) -> int | None:
    """utime + stime. Unchanged between ticks means the pane did nothing."""
    utime = _proc_field(pid, UTIME_FIELD)
    stime = _proc_field(pid, STIME_FIELD)
    if utime is None or stime is None:
        return None
    try:
        return int(utime) + int(stime)
    except ValueError:
        return None


def cpu_rate(
    previous_ticks: int | None, ticks: int | None, elapsed: float | None
) -> float | None:
    """Ticks per second between two samples, or None when unmeasurable.

    None means "no opinion", and every caller treats that as a reason to spare
    rather than to act.
    """
    if previous_ticks is None or ticks is None or elapsed is None:
        return None
    if not (MIN_SAMPLE_SECONDS <= elapsed <= MAX_SAMPLE_SECONDS):
        return None
    if ticks < previous_ticks:
        # The counter went backwards: the pane's process was replaced under a
        # recycled pid. Nothing to compare; take a fresh baseline next tick.
        return None
    return (ticks - previous_ticks) / elapsed


# --- state ------------------------------------------------------------------


def _path(state_path: Path | None) -> Path:
    return state_path or config.REAPER_FILE


def load_state(state_path: Path | None = None) -> dict:
    p = _path(state_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except (OSError, ValueError):
        # A truncated state file costs one tick of lag, never a wedged reaper.
        return {}


def save_state(state: dict, state_path: Path | None = None) -> None:
    p = _path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


# --- the guards -------------------------------------------------------------


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_hours(ts: str | None, now: datetime) -> float | None:
    when = _parse(ts)
    return None if when is None else (now - when).total_seconds() / 3600


def has_written_output(job: dict) -> bool:
    """Did this job leave anything behind on disk?

    The one thing that must never happen is killing a session whose work exists
    only in its own context. A job that has written nothing into its task folder
    since it opened is exactly that case, so it is spared here and left to the
    7-day prune, which is late enough that someone will have looked.

    A job with no `task_folder` recorded (older rows, and anything spawned
    without one) cannot be checked, so it is treated as unproven — spared.
    """
    folder = job.get("task_folder")
    cwd = job.get("cwd")
    if not folder or not cwd:
        return False
    path = Path(cwd) / folder
    if not path.is_dir():
        return False
    opened = _parse(job.get("opened_at"))
    if opened is None:
        # No timestamp to compare against: a non-empty folder is the best
        # evidence available, and is enough.
        return any(path.iterdir())
    cutoff = opened.timestamp()
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and entry.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def pane_pid(session: str, window: str, tmux=tmuxctl) -> int | None:
    """The pid of the window's first pane, or None if the window is gone."""
    result = tmux._run(
        ["tmux", "list-panes", "-t", f"{session}:{window}", "-F", "#{pane_pid}"]
    )
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").split():
        if line.isdigit():
            return int(line)
    return None


# --- decisions ---------------------------------------------------------------


class Decision:
    """What to do with one job, and the sentence that explains it."""

    __slots__ = ("job_id", "action", "why")

    def __init__(self, job_id: str, action: str, why: str) -> None:
        self.job_id, self.action, self.why = job_id, action, why

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.action} {self.job_id}: {self.why}>"


def decide(
    job: dict,
    *,
    now: datetime,
    rate: float | None,
    pane_command: str | None,
    settings=None,
) -> Decision:
    """One job's fate, from facts the caller has already gathered.

    Pure, so the whole rule set is testable without a tmux server, a /proc or a
    clock — which matters, because the expensive half of a wrong answer here is
    a session killed mid-turn.
    """
    settings = settings or config.REAPER
    job_id = job.get("id") or "?"
    status = job.get("status")

    if status == "running":
        return Decision(job_id, "spare", "running")

    if pane_command is None:
        return Decision(job_id, "gone", "window already closed")

    # A pane that has fallen back to a shell has no session left in it; the
    # window is an empty terminal. Anything else running there is someone
    # else's — leave it.
    if "claude" not in pane_command:
        if pane_command in ("zsh", "bash", "sh", "fish"):
            return Decision(job_id, "reap", f"pane is a bare {pane_command}")
        return Decision(job_id, "spare", f"pane is running {pane_command}")

    if status in config.CLOSED_STATUSES:
        return Decision(job_id, "reap", f"status {status}, window left behind")

    if status in config.FINISHED_STATUSES:
        idle = age_hours(job.get("last_update"), now)
        if idle is None:
            return Decision(job_id, "spare", "no last_update to measure idleness from")
        grace = settings.finished_grace_minutes / 60
        if idle < grace:
            return Decision(
                job_id, "spare", f"{status} {idle * 60:.0f}m ago, grace is {grace * 60:.0f}m"
            )
        if rate is None:
            return Decision(job_id, "spare", "no usable CPU sample yet")
        if rate > BUSY_TICKS_PER_SECOND:
            return Decision(
                job_id, "spare", f"still working — CPU at {rate:.2f} ticks/s"
            )
        if not has_written_output(job):
            return Decision(
                job_id, "spare", "nothing written to the task folder — left to the 7-day prune"
            )
        return Decision(
            job_id,
            "reap",
            f"{status} {idle:.1f}h ago, idle at {rate:.2f} ticks/s, output on disk",
        )

    if status == "waiting":
        idle = age_hours(job.get("last_update"), now)
        if idle is None:
            return Decision(job_id, "spare", "no last_update to measure idleness from")
        nudged = age_hours(job.get("nudged_at"), now)
        if nudged is None:
            if idle < settings.waiting_nudge_hours:
                return Decision(
                    job_id,
                    "spare",
                    f"waiting on a human {idle:.1f}h, nudge at "
                    f"{settings.waiting_nudge_hours:.0f}h",
                )
            return Decision(job_id, "nudge", f"waiting on a human {idle:.1f}h with no reply")
        if nudged < settings.waiting_grace_hours:
            return Decision(
                job_id,
                "spare",
                f"nudged {nudged:.1f}h ago, closing at "
                f"{settings.waiting_grace_hours:.0f}h",
            )
        if not has_written_output(job):
            return Decision(
                job_id, "spare", "nudged and unanswered, but nothing written to disk"
            )
        return Decision(
            job_id, "reap", f"nudged {nudged:.1f}h ago and still unanswered"
        )

    return Decision(job_id, "spare", f"unrecognised status {status!r}")


# --- the entry point ---------------------------------------------------------


NUDGE_TEMPLATE = (
    "[{job_id}] {title} — still waiting on you since {when} ({idle:.0f}h). "
    "It asked:\n\n{question}\n\nReply to it, or it closes in {grace:.0f}h and "
    "you can restart it with `respawn {job_id}`."
)


def _default_nudger(job: dict, text: str) -> None:
    """Re-send a blocked job's question, into the job's own conversation.

    Deliberately NOT the notifications channel: this is the job asking Bosire
    something, which is the one class of traffic the split exists to keep in
    the main chat.
    """
    from concierge import telegram

    telegram.send(job["chat_id"], text, reply_to=job.get("root_message_id"))


def run(
    *,
    now: datetime | None = None,
    state_path: Path | None = None,
    registry_path: Path | None = None,
    cpu_path: Path | None = None,
    tmux=tmuxctl,
    settings=None,
    nudger=None,
    logger=None,
) -> str:
    """Sweep every registry row once. Silent and near-free when nothing is due.

    Called from `ensure-up`, which fires 288 times a day, so this writes to the
    log only on a tick that actually decided something.
    """
    now = now or datetime.now(timezone.utc)
    settings = settings or config.REAPER
    nudger = nudger or _default_nudger
    if not settings.enabled:
        return "disabled"

    jobs = registry.load(registry_path)
    if not jobs:
        return "no-jobs"

    state = load_state(cpu_path or state_path)
    previous = state.get("cpu") or {}
    last_run = _parse(state.get("ranAt"))
    elapsed = (now - last_run).total_seconds() if last_run else None
    fresh: dict[str, int] = {}

    reaped, nudged, notable = 0, 0, []

    for job_id, job in sorted(jobs.items()):
        job.setdefault("id", job_id)
        window = job.get("tmux_window") or job_id
        command = tmux.window_command(config.TMUX_SESSION, window)

        ticks = None
        if command and "claude" in command:
            pid = pane_pid(config.TMUX_SESSION, window, tmux=tmux)
            ticks = cpu_ticks(pid) if pid else None
            if ticks is not None:
                fresh[job_id] = ticks

        decision = decide(
            job,
            now=now,
            rate=cpu_rate(previous.get(job_id), ticks, elapsed),
            pane_command=command,
            settings=settings,
        )

        if decision.action == "gone":
            continue

        if decision.action == "spare":
            # Only worth a log line once the job is old enough to be a
            # candidate; sparing a job that finished 4 minutes ago every 5
            # minutes forever would bury the decisions that matter.
            if job.get("status") in config.FINISHED_STATUSES | {"waiting"} and (
                (age_hours(job.get("last_update"), now) or 0)
                >= settings.finished_grace_minutes / 60
            ):
                notable.append(f"  spare {job_id}: {decision.why}")
            continue

        if decision.action == "nudge":
            text = NUDGE_TEMPLATE.format(
                job_id=job_id,
                title=job.get("title", ""),
                when=(_parse(job.get("last_update")) or now).strftime("%a %d %b %H:%M UTC"),
                idle=age_hours(job.get("last_update"), now) or 0,
                question=(job.get("last_message") or "(its question was not recorded)").strip(),
                grace=settings.waiting_grace_hours,
            )
            try:
                nudger(job, text)
            except Exception as exc:  # noqa: BLE001 - a failed nudge must not reap
                notable.append(f"  spare {job_id}: nudge failed, not closing ({exc})")
                continue
            registry.upsert(
                job_id,
                registry_path,
                touch=False,
                nudged_at=now.isoformat(timespec="seconds"),
            )
            nudged += 1
            notable.append(f"  NUDGE {job_id}: {decision.why}")
            continue

        tmux.kill_window(config.TMUX_SESSION, window)
        registry.upsert(job_id, registry_path, touch=False, status="reaped")
        fresh.pop(job_id, None)
        reaped += 1
        notable.append(f"  REAP {job_id}: {decision.why}")

    save_state(
        {"cpu": fresh, "ranAt": now.isoformat(timespec="seconds")},
        cpu_path or state_path,
    )

    if notable:
        (logger or _write_log)(now, reaped, nudged, notable)

    if not reaped and not nudged:
        return "nothing-to-reap"
    return f"reaped:{reaped} nudged:{nudged}"


def _write_log(now: datetime, reaped: int, nudged: int, lines: list[str]) -> None:
    """Append to `state/reaper.log`. Never raises — this is a side channel."""
    try:
        config.REAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with config.REAPER_LOG.open("a") as handle:
            handle.write(
                f"{now.isoformat(timespec='seconds')} | reaped={reaped} nudged={nudged}\n"
            )
            for line in lines:
                handle.write(line + "\n")
    except OSError:
        pass


def report(*, now: datetime | None = None, registry_path: Path | None = None) -> str:
    """What the reaper would do right now, without doing any of it.

    `bin/concierge reap --dry-run`. The CPU rate is measured against the last
    real sweep rather than the next one, so this is not a prediction — a job
    spared here for "no usable CPU sample" may well be reaped on the tick
    after — but it is the honest picture of what the rules say about the
    registry as it stands.
    """
    now = now or datetime.now(timezone.utc)
    jobs = registry.load(registry_path)
    if not jobs:
        return "no jobs in the registry"

    state = load_state()
    previous = state.get("cpu") or {}
    last_run = _parse(state.get("ranAt"))
    elapsed = (now - last_run).total_seconds() if last_run else None
    lines = []
    for job_id, job in sorted(jobs.items()):
        job.setdefault("id", job_id)
        window = job.get("tmux_window") or job_id
        command = tmuxctl.window_command(config.TMUX_SESSION, window)
        ticks = None
        if command and "claude" in command:
            pid = pane_pid(config.TMUX_SESSION, window)
            ticks = cpu_ticks(pid) if pid else None
        decision = decide(
            job,
            now=now,
            rate=cpu_rate(previous.get(job_id), ticks, elapsed),
            pane_command=command,
        )
        lines.append(
            f"{decision.action.upper():6} {job_id} "
            f"({job.get('status')}) — {decision.why}"
        )
    return "\n".join(lines)
