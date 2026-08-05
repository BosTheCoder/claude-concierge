"""Spawn a job: one tmux window, one Claude Code session, one registry entry."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from concierge import config, registry, tmuxctl


def claude_argv(job_id: str, title: str, brief: str, prompt_file: Path) -> list[str]:
    return [
        "claude",
        "--remote-control",
        f"[{job_id}] {title}",
        "--append-system-prompt-file",
        str(prompt_file),
        brief,
    ]


def poll_for_rc_url(
    window: str,
    *,
    attempts: int = 20,
    delay: float = 1.0,
    capture=None,
    sleeper=None,
) -> str | None:
    """Watch the pane for the printed Remote Control URL.

    A miss is not fatal: the session is named '[A3] <title>' so it is still
    findable in the session list at claude.ai/code.
    """
    capture = capture or (lambda w: tmuxctl.capture(config.TMUX_SESSION, w))
    sleeper = sleeper or time.sleep

    for i in range(attempts):
        url = tmuxctl.extract_rc_url(capture(window))
        if url:
            return url
        if i < attempts - 1:
            sleeper(delay)
    return None


def spawn_job(
    title: str,
    brief: str,
    cwd: str,
    chat_id: str,
    root_message_id: int | None = None,
    *,
    task_folder: str | None = None,
    state_path: Path | None = None,
    tmux=tmuxctl,
    poller=None,
) -> dict:
    jobs = registry.load(state_path)
    job_id = registry.allocate_id(jobs)

    prompt_file = config.PROMPTS_DIR / "job.md"
    argv = claude_argv(job_id, title, brief, prompt_file)
    tmux.new_window(
        config.TMUX_SESSION, job_id, tmux.build_shell_command(cwd, argv)
    )

    poller = poller or poll_for_rc_url
    rc_url = poller(job_id)

    registry.upsert(
        job_id,
        state_path,
        id=job_id,
        title=title,
        status="running",
        chat_id=chat_id,
        root_message_id=root_message_id,
        cwd=cwd,
        task_folder=task_folder,
        tmux_window=job_id,
        rc_url=rc_url,
        opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return registry.load(state_path)[job_id]
