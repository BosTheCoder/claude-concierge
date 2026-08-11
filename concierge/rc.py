"""Remote Control drops silently, and a dropped session looks perfectly healthy.

`ensure_up` asks one question — is window 0 running a claude process — and the
answer stayed "yes" through three days in which every concierge window had been
cut off from claude.ai. The process is fine, the tmux session is fine; what died
is the bridge, and nothing local notices, because from the terminal's point of
view nothing happened. Messages sent from the phone simply stop arriving.

The signal is in Claude Code's own session registry. ~/.claude/sessions/<pid>.json
carries `bridgeSessionId`: a string while the bridge is up, and explicitly null
once it goes — the field is written either way, so null is a positive statement
that the bridge is down rather than an absence we have to guess at. Measured on
2026-08-11 against four dead concierge windows and two live sessions: null for
all four, set for both.

The lever is the `/remote-control` slash command typed into the pane. Three
things about that were verified against the live session rather than assumed,
and each one shapes the code below:

  * It is a toggle. On an already-connected session it opens a menu with
    "Continue" preselected rather than disconnecting outright — but that menu
    still has to be dismissed by a human, so we only ever send it to a pane
    whose registry entry says the bridge is null.
  * An empty input box is not blank. Claude Code redraws the last thing you
    typed as a dim (SGR 2) placeholder, so a plain `capture-pane` shows text
    that is not really there. The reverse mistake is the dangerous one: Enter
    on a box that genuinely holds an unsent instruction submits it, and one of
    the four dead windows was holding "please post those reviews. Go ahead and
    post them." `input_box_content` is what separates the two, and a pane
    holding real text is left for a human.
  * Recovery is confirmed by re-reading the registry, never by reading the
    pane. A successful reconnect prints nothing to the terminal at all.
  * A pane is not a session. Subagents get their own registry row and inherit
    the pane of the session that spawned them, so window 0 had three rows
    against it — one real, two claude-mem observers with no bridge. Treating
    those as disconnections typed /rc into a healthy window and left a menu
    open on it. `participates` and the pane grouping in `sweep` are both there
    to stop that, and they are independent on purpose.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from concierge import config, tmuxctl

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# How long to let every `/rc` land before checking the registry. Measured
# reconnects completed in 5-20s; this is one wait shared by the whole sweep,
# not one per pane, so it stays well inside the 5-minute ensure-up tick.
RECONNECT_WAIT_SECONDS = 25

# A session that started seconds ago has a null bridge because it has not
# finished connecting, not because it dropped. Typing /rc into that is how you
# turn a healthy startup into a disconnected session.
MIN_AGE_SECONDS = 120

# Reconnects are silent; only the cases needing a human speak, and then not
# more than once an hour each.
ALERT_COOLDOWN_MINUTES = 60

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Claude Code's own prompt marker. Queued messages are drawn with it too, so
# the input box is the last one on screen.
PROMPT = "❯"


@dataclass(frozen=True)
class Session:
    """One row of Claude Code's session registry, as far as we care about it."""

    path: Path
    pid: int
    proc_start: str | None
    started_at: int | None
    name: str
    cwd: str
    tmux: str | None
    entrypoint: str
    declares_bridge: bool
    bridge_session_id: str | None

    @property
    def connected(self) -> bool:
        return bool(self.bridge_session_id)

    @property
    def participates(self) -> bool:
        """Is this a session Remote Control applies to at all?

        Subagents spawned inside a session get their own registry row, inherit
        the parent's tmux pane, and have no bridge — `entrypoint` is "sdk-cli"
        and the `bridgeSessionId` key is absent rather than null. Reading that
        absence as a disconnection is how a sweep ends up typing /rc into a
        perfectly healthy window: it happened on 2026-08-11, and two
        claude-mem observer sessions sharing window 0's pane were the cause.
        """
        return self.entrypoint == "cli" and self.declares_bridge

    @property
    def pane(self) -> str | None:
        """'concierge:@0.%0' -> '%0', for any tmux session, not just ours.

        Ad-hoc windows drop off the bridge exactly as concierge ones do, and a
        pane is a pane — the guards that make typing into one safe (registry
        says it is down, nothing else on the pane is connected, the box holds
        no unsent text, the pane is actually running Claude Code) do not care
        whose tmux session it belongs to.

        Pane ids rather than window indexes: indexes renumber when a window
        closes, and the cost of typing into the wrong Claude Code session is
        that it acts on it.
        """
        if not self.tmux:
            return None
        pane = self.tmux.rpartition(".")[2]
        return pane if pane.startswith("%") else None

    @property
    def tmux_session(self) -> str | None:
        return self.tmux.partition(":")[0] if self.tmux else None

    def age_seconds(self, now: datetime) -> float:
        if not self.started_at:
            return float("inf")
        return now.timestamp() - self.started_at / 1000


def load_sessions(sessions_dir: Path | None = None) -> list[Session]:
    """Every registry row we can parse. Unreadable rows are skipped, not fatal:
    the directory holds hundreds of files going back months, and one truncated
    write must not blind the sweep to the rest."""
    directory = sessions_dir or SESSIONS_DIR
    found: list[Session] = []
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return found

    for path in paths:
        try:
            data = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int):
            continue
        found.append(
            Session(
                path=path,
                pid=pid,
                proc_start=(
                    str(data["procStart"]) if data.get("procStart") is not None else None
                ),
                started_at=data.get("startedAt"),
                name=data.get("name") or str(pid),
                cwd=data.get("cwd") or "",
                tmux=data.get("tmux"),
                entrypoint=data.get("entrypoint") or "",
                # Present-and-null and absent mean different things here, which
                # `.get()` alone would flatten. See Session.participates.
                declares_bridge="bridgeSessionId" in data,
                bridge_session_id=data.get("bridgeSessionId"),
            )
        )
    return found


def read_proc_start(pid: int, proc_root: Path = Path("/proc")) -> str | None:
    """Field 22 of /proc/<pid>/stat, or None when the process is gone.

    Split after the LAST ')': the comm field is parenthesised and may itself
    contain spaces and brackets, so naive whitespace splitting misreads every
    field after it.
    """
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def is_running(session: Session, proc_start=read_proc_start) -> bool:
    """Is this exact process still alive?

    Registry files outlive their processes by months, and pids get recycled.
    Comparing the recorded start time is what stops us typing /rc into whatever
    happens to hold pid 5559 today.
    """
    actual = proc_start(session.pid)
    if actual is None:
        return False
    return session.proc_start is None or actual == session.proc_start


def input_box_content(pane_text: str) -> str | None:
    """What the user has actually typed and not yet sent, or None.

    The whole safety of this module rests here. Claude Code renders the last
    submitted input back into the empty box as a dim SGR-2 placeholder, so the
    plain text of the pane cannot tell "nothing pending" from "an instruction
    waiting to be sent". Stripping the dim run first is what makes the
    difference visible.
    """
    lines = [line for line in (pane_text or "").splitlines() if PROMPT in line]
    if not lines:
        return None
    after = lines[-1].split(PROMPT, 1)[1]
    # A dim run is the ghost of the last message, drawn only when the box is
    # empty. Anything left after removing it is really there.
    without_ghost = re.sub(r"\x1b\[2m.*?(?:\x1b\[0m|$)", "", after)
    # The marker is followed by U+00A0, which str.strip() does not touch.
    text = ANSI.sub("", without_ghost).replace("\xa0", " ").strip()
    return text or None


# --- alert cooldown ---------------------------------------------------------


def _state_path(path: Path | None) -> Path:
    return path or config.STATE_FILE.parent / "rc.json"


def load_state(path: Path | None = None) -> dict:
    p = _state_path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except ValueError:
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    p = _state_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


def _in_cooldown(state: dict, key: str, now: datetime) -> bool:
    stamp = (state.get("alerts") or {}).get(key)
    if not stamp:
        return False
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if not last.tzinfo:
        last = last.replace(tzinfo=timezone.utc)
    return now - last < timedelta(minutes=ALERT_COOLDOWN_MINUTES)


# --- the sweep --------------------------------------------------------------


def report(
    *,
    sessions_dir: Path | None = None,
    proc_start=read_proc_start,
) -> str:
    """Read-only: which sessions are on the bridge and which have fallen off.

    The sweep fixes what it can reach; this answers "what's disconnected?"
    asked from the phone, including the ad-hoc sessions it cannot fix. Kept to
    a couple of lines because the answer is read in a chat window.
    """
    live = [
        s
        for s in load_sessions(sessions_dir)
        if s.participates and is_running(s, proc_start)
    ]
    if not live:
        return "no Claude Code sessions running"

    connected_panes = {s.pane for s in live if s.connected and s.pane}
    down = [s for s in live if not s.connected and s.pane not in connected_panes]
    count = f"{len(live)} session{'' if len(live) == 1 else 's'}"
    if not down:
        return f"{count}, all connected"

    def describe(s: Session) -> str:
        where = f"tmux {s.tmux_session}" if s.pane else "not in tmux — run /rc there"
        return f"{s.name} ({where})"

    return f"{count}, {len(live) - len(down)} connected. Down: " + ", ".join(
        describe(s) for s in down
    )


def _default_notifier(message: str, state_path: Path | None = None) -> None:
    from concierge import supervisor

    supervisor._default_notifier(message, state_path)


def sweep(
    *,
    sessions_dir: Path | None = None,
    tmux=tmuxctl,
    sleeper=time.sleep,
    proc_start=read_proc_start,
    notifier=None,
    state_path: Path | None = None,
    registry_path: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Find sessions that have dropped off Remote Control and put them back.

    Silent and near-free when everything is connected, which is the common
    case on all 288 ticks a day.
    """
    now = now or datetime.now(timezone.utc)
    notifier = notifier or (lambda msg: _default_notifier(msg, registry_path))
    state = load_state(state_path)
    alerts = state.setdefault("alerts", {})

    def alert(key: str, message: str, *, once: bool = False) -> None:
        """`once` for things only he can fix by hand.

        An hourly reminder is right for a concierge window that keeps failing
        to reconnect — that is actionable and it matters. It is just nagging
        for an ordinary terminal session across the machine that he may not
        even want back; one message is the whole useful content.
        """
        if once and key in alerts:
            return
        if _in_cooldown(state, key, now):
            return
        notifier(message)
        alerts[key] = now.isoformat(timespec="seconds")

    live = [s for s in load_sessions(sessions_dir) if is_running(s, proc_start)]
    # A pane can host more than one registry row — subagents inherit their
    # parent's. If anything on this pane still holds a bridge, the pane is
    # connected, whatever its other rows say, and /rc would only toggle it off.
    connected_panes = {s.pane for s in live if s.connected and s.pane}

    down = [
        s
        for s in live
        if s.participates
        and not s.connected
        and s.pane not in connected_panes
        and s.age_seconds(now) >= MIN_AGE_SECONDS
    ]
    if not down:
        save_state(state, state_path)
        return "rc: all connected"

    outcomes: list[str] = []
    pending: list[Session] = []

    for session in down:
        key = f"{session.name}:{session.pid}"
        if not session.pane:
            # A plain terminal session. There is no pane to type into and no
            # safe way to reach its stdin, so this one really is his to fix —
            # a peer message reaches the session but arrives as text, and a
            # slash command sent that way is not executed (tested 2026-08-11).
            alert(
                key,
                f"{session.name} has dropped off Remote Control and isn't "
                f"running under tmux, so there's no pane for me to type into — "
                f"run /rc in that session ({session.cwd}).",
                once=True,
            )
            outcomes.append(f"{session.name}: no tmux pane")
            continue

        running = tmux.pane_command(session.pane)
        if not running or "claude" not in running:
            # The session handed its terminal to a shell, so "/rc" would be run
            # as a shell command. Seen on a real session sitting at `status:
            # "shell"` — harmless but useless, and it would loop forever.
            alert(
                key,
                f"{session.name} has dropped off Remote Control, but its pane "
                f"is running {running or 'nothing'} rather than Claude Code, so "
                f"I can't reconnect it from here.",
                once=True,
            )
            outcomes.append(f"{session.name}: pane not at the REPL")
            continue

        typed = input_box_content(tmux.capture_pane_escaped(session.pane))
        if typed:
            # Enter here would submit whatever he left in the box. One of the
            # four dead windows was holding an instruction to post reviews.
            alert(
                key,
                f"{session.name} has dropped off Remote Control, but its input "
                f"box holds unsent text so I won't press Enter on it: {typed!r}. "
                f"Clear the box and run /rc.",
            )
            outcomes.append(f"{session.name}: has unsent text")
            continue

        if not tmux.send_keys(session.pane, "/rc", "Enter"):
            alert(key, f"{session.name}: couldn't send /rc to its pane.")
            outcomes.append(f"{session.name}: send failed")
            continue
        pending.append(session)

    if pending:
        sleeper(RECONNECT_WAIT_SECONDS)
        # Re-read from disk: the bridge id is written by the session process,
        # so the file we loaded at the top of the sweep is stale by definition.
        fresh = {s.pid: s for s in load_sessions(sessions_dir)}
        for session in pending:
            key = f"{session.name}:{session.pid}"
            after = fresh.get(session.pid)
            if after and after.connected:
                # Self-healed. Nothing to say, and clear the cooldown so the
                # next drop alerts immediately instead of being swallowed.
                alerts.pop(key, None)
                outcomes.append(f"{session.name}: reconnected")
            else:
                alert(
                    key,
                    f"{session.name} has dropped off Remote Control and /rc "
                    f"didn't bring it back. Its window needs restarting.",
                )
                outcomes.append(f"{session.name}: still down")

    save_state(state, state_path)
    return "rc: " + ", ".join(outcomes)
