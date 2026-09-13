"""Thin, injectable wrapper over the tmux commands we need."""

from __future__ import annotations

import re
import shlex
import subprocess
import time

RC_URL = re.compile(r"https://claude\.ai/code/\S+")

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Claude Code's own prompt marker. Queued messages are drawn with it too, so
# the input box is the last one on screen.
PROMPT = "❯"

# Text and its Enter must reach Claude Code as separate reads. Measured
# 2026-09-13 on Claude Code 2.1.270: `send-keys '<long text>' Enter` in one call
# is taken as a paste, the Enter becomes part of it, and the message sits in the
# input box unsent. A short message in the same call goes through, which is why
# it looked intermittent. Job E6 waited six hours on one.
SUBMIT_PAUSE_SECONDS = 0.5
# How long Claude Code gets to take the message before we look at the box.
SUBMIT_SETTLE_SECONDS = 2.0
SUBMIT_BUFFER = "concierge-submit"
# Seconds a freshly spawned job gets to draw its input box.
SUBMIT_STARTUP_CHECKS = 30


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


def pipe_pane(session: str, window: str, command: str, *, runner=None) -> bool:
    """Copy a pane's output to `command`, leaving the pane's process alone.

    Not a pipeline in the shell line, which is the obvious alternative and is
    wrong: measured 2026-08-18, `exec claude ... | tee log` makes tmux report
    `#{pane_current_command}` as the SHELL, so rcserver.alive() — which tests
    for "claude" in that string — would return False on every tick and recycle
    the server every five minutes forever. Redirecting to a file instead is
    equally wrong in the other direction: it empties the pane that
    rcserver.pane_status() reads to decide whether the server is healthy.

    pipe-pane touches neither. -O sends only output, so nothing is written back
    into the pane.

    Best-effort: losing the copy is not a reason to fail a server start.
    """
    runner = runner or _run
    return (
        runner(["tmux", "pipe-pane", "-O", "-t", f"{session}:{window}", command]).returncode
        == 0
    )


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


def input_box_content(pane_text: str) -> str | None:
    """What the user has actually typed and not yet sent, or None.

    Claude Code renders the last submitted input back into the empty box as a
    dim SGR-2 placeholder, so the plain text of the pane cannot tell "nothing
    pending" from "an instruction waiting to be sent". Stripping the dim run
    first is what makes the difference visible. Needs a `capture-pane -e`.
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


def submit(pane: str, text: str, *, runner=None, sleeper=None) -> str | None:
    """Type `text` into a Claude Code pane and send it. None once it has gone,
    otherwise why it has not.

    Pasted (`paste-buffer -p`) so newlines stay newlines inside the message,
    then Enter on its own after a pause — see SUBMIT_PAUSE_SECONDS. Gone means
    the input box is empty afterwards; a busy session queues the message and
    clears the box just the same.
    """
    runner = runner or _run
    sleeper = sleeper or time.sleep
    # A job spawned seconds ago has no input box yet, and keys typed before it
    # draws one vanish: J8 on 2026-08-21 and R5 on 2026-09-05 both lost a
    # message that way. No prompt on screen also means an empty read below
    # proves nothing, so wait for it rather than guess.
    for _ in range(SUBMIT_STARTUP_CHECKS):
        screen = capture_pane_escaped(pane, runner=runner) or ""
        if PROMPT in screen:
            break
        sleeper(1)
    else:
        return "it has no input box on screen — still starting, or its window is gone"
    typed = input_box_content(screen)
    if typed:
        # Enter now would send whatever is already there glued to ours.
        return f"its input box already holds unsent text: {typed!r}"
    if (
        runner(["tmux", "set-buffer", "-b", SUBMIT_BUFFER, "--", text]).returncode
        or runner(
            ["tmux", "paste-buffer", "-p", "-d", "-b", SUBMIT_BUFFER, "-t", pane]
        ).returncode
    ):
        return "tmux could not type into it — is its window still open?"
    sleeper(SUBMIT_PAUSE_SECONDS)
    for _ in range(2):
        runner(["tmux", "send-keys", "-t", pane, "Enter"])
        sleeper(SUBMIT_SETTLE_SECONDS)
        left = input_box_content(capture_pane_escaped(pane, runner=runner))
        if not left:
            return None
    return f"still sitting in its input box after two Enters: {left!r}"
