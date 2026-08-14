"""Keep `claude remote-control` — the server — up. Not the same as rc.py.

Three things wear the name "remote control" in this repo and they are worth
keeping straight:

  * `--remote-control concierge` (supervisor.py) publishes the one long-lived
    concierge session so the phone can talk to *it*.
  * `rc.py` sweeps every ordinary session that has silently fallen off the
    bridge and types `/rc` to put it back.
  * this module runs `claude remote-control` in **server mode**, which is what
    puts the machine itself in the Claude app's device list. Sessions are then
    created on demand from the phone, in TASKS_REPO, up to the CLI's default
    capacity of 32.

It was started by hand on 2026-08-12 and worked for a day, which is exactly
the failure mode this repo exists to close: it does not survive a reboot, the
nightly shutdown task, or `wsl --shutdown`. Hence a logon task and a
five-minute watchdog, in their own `remote-control` wst namespace rather than
riding the concierge's — a service should not be able to take another service
down by failing to start.

Health is read off the pane, and it has to be. Measured against a live server
on 2026-08-13: the server process writes **no** row to ~/.claude/sessions, and
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
from pathlib import Path

from concierge import config, tmuxctl

# Five-minute ticks. Four of them is twenty minutes of never reaching
# "Connected", which is well past anything the CLI's own backoff recovers from.
DEGRADED_STRIKES = 4
TICK_MINUTES = 5

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


# --- the supervisor ---------------------------------------------------------


def _default_notifier(message: str, registry_path: Path | None = None) -> None:
    from concierge import supervisor

    supervisor._default_notifier(message, registry_path)


def _start(tmux) -> None:
    """Start the server window, whether or not the session is already there."""
    shell_command = tmux.build_shell_command(str(config.TASKS_REPO), server_argv())
    if not tmux.has_session(config.RC_SERVER_TMUX_SESSION):
        tmux.new_session(
            config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW, shell_command
        )
        return
    if config.RC_SERVER_WINDOW in tmux.list_windows(config.RC_SERVER_TMUX_SESSION):
        tmux.kill_window(config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW)
    tmux.new_window(
        config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW, shell_command
    )


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

    if not alive(tmux):
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
    save_state(state, state_path)
    if strikes < DEGRADED_STRIKES:
        # Its own backoff usually wins from here. Say nothing yet.
        return f"degraded: {status} ({strikes}/{DEGRADED_STRIKES})"

    state["strikes"] = 0
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
