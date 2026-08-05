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


def build_shell_command(cwd: str, argv: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in argv)
    return f"cd {shlex.quote(cwd)} && exec {quoted}"


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


def capture(session: str, window: str, *, runner=None) -> str:
    runner = runner or _run
    return runner(["tmux", "capture-pane", "-p", "-t", f"{session}:{window}"]).stdout
