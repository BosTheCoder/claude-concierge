"""Keep `claude remote-control` — the server — up. Not the same as rc.py.

Three things wear the name "remote control" in this repo and they are worth
keeping straight:

  * `--remote-control concierge` (supervisor.py) publishes the one long-lived
    concierge session so the phone can talk to *it*.
  * `rc.py` sweeps every ordinary session that has silently fallen off the
    bridge and types `/rc` to put it back.
  * this module runs `claude remote-control` in **server mode**, which is what
    puts the machine itself in the Claude app's device list. Sessions are then
    created on demand from the phone, in the default repo, up to the CLI's
    default capacity of 32.

Started by hand it works until the next reboot, which is exactly the failure
this repo exists to close. Hence a logon task and a five-minute watchdog, in
their own scheduled-task namespace rather than riding the concierge's — a
service should not be able to take another service down by failing to start.

Health is read off the pane, and it has to be. Measured against a live server:
the server process writes **no** row to ~/.claude/sessions, and
the sessions it spawns declare `entrypoint: "sdk-cli"` with the
`bridgeSessionId` key *absent* rather than null. So the registry trick rc.py
relies on has nothing to read here. (The same measurement is why rc.py is safe:
`Session.participates` is False for every one of those rows, so the sweep will
never type `/rc` into a server pane.)

What the pane says, from the CLI's own renderer: `Connecting` while it comes
up, the session title once connected, and `Reconnecting · retrying in Xs ·
disconnected Ys` while its backoff runs. That backoff is real and usually
wins — a ten-minute network drop recovers on its own — so a single bad reading
must not trigger a restart. Only a run of them does, because recycling the
window kills whatever was open from the phone.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

from concierge import config, tmuxctl

# Five-minute ticks. Four of them is twenty minutes of never reaching
# "Connected", which is well past anything the CLI's own backoff recovers from.
DEGRADED_STRIKES = 4
TICK_MINUTES = 5

# Every server start mints a fresh session about ten seconds later — measured
# 2026-08-17, server up at 03:19:34 and cse_013ubZ2uJAa2vxwETaD43kmj at 03:19:44
# — and each of those shows up in the Claude app as an empty "Bos-Desktop" chat.
# So the restart rate IS the empty-chat rate, and until now nothing recorded it:
# ensure_up runs 288 times a day, returns a string the wst task discards, and
# only speaks on the four-strike path. When the app filled with empty chats the
# history needed to explain it did not exist, and could not be reconstructed.
#
# Written on transitions only. Healthy ticks stay silent, which is nearly all of
# them, so this cannot churn the state file.
HISTORY_LIMIT = 40

# Cap for the captured pane log. The server repaints its status line
# continuously and each repaint is a full ANSI redraw — measured 2026-08-18 at
# 15,000 bytes per 30 seconds, about 41 MB/day. Unbounded logging into the guest
# is the exact shape of the journald write storm in the 11 Aug WSL diagnosis, so
# the file is trimmed to its tail on every tick. The tail is the part worth
# keeping: what we are after is the server's last words before it exits.
LOG_TAIL_BYTES = 256 * 1024

# Word boundaries because "Connecting" contains no "Connected" but the eye
# does not have to be fooled for a substring check to be.
STATUS = re.compile(r"\b(Reconnecting|Connecting|Connected)\b")
_STATUS_NAMES = {
    "Connected": "connected",
    "Connecting": "connecting",
    "Reconnecting": "reconnecting",
}


def server_argv() -> list[str]:
    return [
        "claude",
        "remote-control",
        "--name",
        config.RC_SERVER_NAME,
        "--spawn",
        config.RC_SERVER_SPAWN_MODE,
        "--permission-mode",
        config.PERMISSION_MODE,
    ]


def pane_status(pane_text: str) -> str:
    """connected | connecting | reconnecting | unknown.

    The last match wins. The banner printed at startup stays above the status
    line, so a pane that has been up for a day still has the word "Connecting"
    on screen from when it was starting; reading top-down would report a
    healthy server as permanently mid-connect.
    """
    matches = STATUS.findall(pane_text or "")
    if not matches:
        return "unknown"
    return _STATUS_NAMES[matches[-1]]


def alive(tmux) -> bool:
    """Session exists AND its window still holds a claude process.

    `has_session` alone is not liveness — the same reason it isn't for the
    concierge. Here the trap is smaller but real: the window's shell outlives
    the server if it exits, leaving a session that looks present and answers
    nothing.
    """
    if not tmux.has_session(config.RC_SERVER_TMUX_SESSION):
        return False
    command = tmux.window_command(
        config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW
    )
    return bool(command) and "claude" in command


# --- strike counter ---------------------------------------------------------


def _state_path(path: Path | None) -> Path:
    return path or config.RC_SERVER_FILE


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


def _record(state: dict, event: str, detail: str = "", *, now: datetime | None = None) -> None:
    """Append one transition to the bounded history. Mutates, does not save.

    `why` is the useful half: "started" alone does not distinguish a reboot from
    a window that had dropped to a shell from a four-strike recycle, and those
    call for completely different fixes.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    history = state.setdefault("history", [])
    history.append({"at": stamp, "event": event, "why": detail})
    # Oldest first, so the tail is the recent past. 40 entries is weeks of a
    # healthy server and still bounds a flapping one.
    del history[:-HISTORY_LIMIT]


# --- the supervisor ---------------------------------------------------------


def _default_notifier(message: str, registry_path: Path | None = None) -> None:
    from concierge import supervisor

    supervisor._default_notifier(message, registry_path)


def _capture_output(tmux) -> None:
    """Tee the new pane's output to RC_SERVER_LOG.

    The server exits on its own every few hours — measured 2026-08-17, three
    starts in a day, each minting an empty chat ~10s later — and when it does,
    tmux destroys the single-window session and the pane scrollback goes with
    it. So the one artefact that would say why is the one thing not kept.

    `cat >>` rather than a log rotator: the volume is a status line, and a
    truncating tool would race the very exit we are trying to catch.
    """
    log = config.RC_SERVER_LOG
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tmux.pipe_pane(
        config.RC_SERVER_TMUX_SESSION,
        config.RC_SERVER_WINDOW,
        f"cat >> {shlex.quote(str(log))}",
    )


def _start(tmux) -> None:
    """Start the server window, whether or not the session is already there."""
    shell_command = tmux.build_shell_command(
        str(config.SETTINGS.default_repo.path), server_argv()
    )
    if not tmux.has_session(config.RC_SERVER_TMUX_SESSION):
        tmux.new_session(
            config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW, shell_command
        )
        _capture_output(tmux)
        return
    if config.RC_SERVER_WINDOW in tmux.list_windows(config.RC_SERVER_TMUX_SESSION):
        tmux.kill_window(config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW)
    tmux.new_window(
        config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW, shell_command
    )
    # Per-pane and not inherited, so it must be re-attached to every new window,
    # not just the first.
    _capture_output(tmux)


def _trim_log(log: Path | None = None) -> None:
    """Keep the last LOG_TAIL_BYTES of the capture file.

    Rewrites in place rather than rotating: `cat >>` from pipe-pane holds the
    file open, so renaming it would leave the writer appending to an unlinked
    inode and the log would silently stop growing. Truncating from the front and
    rewriting keeps the same inode and the same fd valid.
    """
    log = log or config.RC_SERVER_LOG
    try:
        if log.stat().st_size <= LOG_TAIL_BYTES:
            return
        with log.open("rb") as handle:
            handle.seek(-LOG_TAIL_BYTES, 2)
            tail = handle.read()
    except OSError:
        return
    # Drop the partial first line so the file always starts mid-nothing.
    _, _, rest = tail.partition(b"\n")
    try:
        with log.open("wb") as handle:
            handle.write(rest or tail)
    except OSError:
        pass


def ensure_up(
    *,
    tmux=tmuxctl,
    state_path: Path | None = None,
    registry_path: Path | None = None,
    notifier=None,
) -> str:
    """Idempotent, silent when healthy, safe to run every five minutes."""
    notifier = notifier or (lambda msg: _default_notifier(msg, registry_path))
    state = load_state(state_path)
    # Every tick, not just restarts: the writer is the server itself and it does
    # not stop between them.
    _trim_log()

    if not alive(tmux):
        # Captured before _start, because _start is what makes it stop being
        # true — and "was there a session at all" is the difference between a
        # reboot and a window that quietly dropped to a shell.
        why = (
            "no tmux session"
            if not tmux.has_session(config.RC_SERVER_TMUX_SESSION)
            else "window not running claude"
        )
        try:
            _start(tmux)
        except RuntimeError as exc:
            if "duplicate session" in str(exc):
                # The logon task and the watchdog can fire together and both
                # see no session. The loser lost a race, not a server.
                return "healthy"
            notifier(f"the Remote Control server failed to start: {exc}")
            raise
        state["strikes"] = 0
        _record(state, "started", why)
        save_state(state, state_path)
        return "started"

    status = pane_status(
        tmux.capture(config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW)
    )
    if status == "connected":
        if state.get("strikes"):
            state["strikes"] = 0
            save_state(state, state_path)
        return "healthy"

    strikes = int(state.get("strikes") or 0) + 1
    state["strikes"] = strikes
    if strikes == 1:
        # First strike only. Recording all four would bury the incident count
        # in repetition, and the first one is what carries the timestamp the
        # backoff started from.
        _record(state, "degraded", status)
    save_state(state, state_path)
    if strikes < DEGRADED_STRIKES:
        # Its own backoff usually wins from here. Say nothing yet.
        return f"degraded: {status} ({strikes}/{DEGRADED_STRIKES})"

    state["strikes"] = 0
    _record(state, "recycled", f"{status} for {DEGRADED_STRIKES * TICK_MINUTES}m")
    save_state(state, state_path)
    try:
        _start(tmux)
    except RuntimeError as exc:
        notifier(
            f"the Remote Control server on {config.RC_SERVER_NAME} has been "
            f"{status} for {DEGRADED_STRIKES * TICK_MINUTES} minutes and I "
            f"couldn't restart it: {exc}"
        )
        return f"recycle-failed: {exc}"

    notifier(
        f"the Remote Control server on {config.RC_SERVER_NAME} was {status} for "
        f"{DEGRADED_STRIKES * TICK_MINUTES} minutes, so I restarted it. Anything "
        f"you had open on it from the phone is gone — start a new session."
    )
    return "recycled"
