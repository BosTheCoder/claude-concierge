"""Thin, injectable wrapper over the tmux commands we need."""

from __future__ import annotations

import re
import shlex
import subprocess

RC_URL = re.compile(r"https://claude\.ai/code/\S+")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def extract_rc_url(pane_text: str) -> str | None:
    matches = RC_URL.findall(pane_text or "")
    if not matches:
        return None
    # Terminal wrapping and prose can leave punctuation glued to the URL.
    return matches[-1].rstrip(".,);]'\"")


def build_shell_command(
    cwd: str, argv: list[str], env: dict[str, str] | None = None
) -> str:
    """Build the shell line tmux runs for a window.

    `env` becomes assignment prefixes on the `exec`, which every POSIX shell
    exports into the replacing process. This is how a job learns its own id:
    an env var survives context compaction, a system prompt does not.
    """
    quoted = " ".join(shlex.quote(a) for a in argv)
    assignments = "".join(
        f"{name}={shlex.quote(value)} " for name, value in (env or {}).items()
    )
    return f"cd {shlex.quote(cwd)} && {assignments}exec {quoted}"


def has_session(session: str, *, runner=None) -> bool:
    runner = runner or _run
    return runner(["tmux", "has-session", "-t", session]).returncode == 0


def new_session(session: str, window: str, shell_command: str, *, runner=None) -> None:
    runner = runner or _run
    result = runner([
        "tmux", "new-session", "-d", "-s", session, "-n", window, shell_command
    ])
    if result.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {result.stderr.strip()}")


def new_window(session: str, window: str, shell_command: str, *, runner=None) -> None:
    runner = runner or _run
    result = runner(["tmux", "new-window", "-t", session, "-n", window, shell_command])
    if result.returncode != 0:
        raise RuntimeError(f"tmux new-window failed: {result.stderr.strip()}")


def kill_window(session: str, window: str, *, runner=None) -> None:
    runner = runner or _run
    runner(["tmux", "kill-window", "-t", f"{session}:{window}"])


def list_windows(session: str, *, runner=None) -> list[str]:
    runner = runner or _run
    out = runner([
        "tmux", "list-windows", "-t", session, "-F", "#{window_name}"
    ]).stdout
    return [line for line in (out or "").splitlines() if line]


def window_command(session: str, window: str, *, runner=None) -> str | None:
    """The command currently running in that window's pane, or None.

    'Session exists' is not liveness: every job is a window in the same
    session, so a live job keeps the session up long after window 0's claude
    process has died.
    """
    runner = runner or _run
    result = runner([
        "tmux", "list-panes", "-t", f"{session}:{window}",
        "-F", "#{pane_current_command}",
    ])
    if result.returncode != 0:
        return None
    lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
    return lines[0] if lines else None


def capture(session: str, window: str, *, runner=None) -> str:
    runner = runner or _run
    return runner(["tmux", "capture-pane", "-p", "-t", f"{session}:{window}"]).stdout


def capture_pane_escaped(pane: str, *, runner=None) -> str:
    """Capture a pane by pane id, keeping its escape sequences.

    `-e` is not decoration here: Claude Code draws the ghost of your last
    message into the empty input box in dim SGR-2, and without the codes there
    is no way to tell that ghost from text genuinely waiting to be sent.
    """
    runner = runner or _run
    return runner(["tmux", "capture-pane", "-pe", "-t", pane]).stdout


def pane_command(pane: str, *, runner=None) -> str | None:
    """What that pane is currently running, or None if it is gone.

    A Claude Code session can hand its terminal to a shell and say so in the
    registry (`status: "shell"`). Typing "/rc" then executes it as a shell
    command instead. This is the ground truth the registry only reports.
    """
    runner = runner or _run
    result = runner(["tmux", "display-message", "-p", "-t", pane, "#{pane_current_command}"])
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def send_keys(pane: str, *keys: str, runner=None) -> bool:
    """Type into a pane by pane id. False when tmux refused.

    Pane ids rather than window indexes: indexes shift when a window closes,
    and the cost of typing into the wrong Claude Code session is that it acts
    on it.
    """
    runner = runner or _run
    return runner(["tmux", "send-keys", "-t", pane, *keys]).returncode == 0
