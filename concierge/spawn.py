"""Spawn a job: one tmux window, one Claude Code session, one registry entry."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from concierge import config, registry, settings, tmuxctl


def claude_argv(job_id: str, title: str, brief: str, prompt_file: Path) -> list[str]:
    # The id leads the brief so the model can read it in prose too; the
    # authoritative copy is CONCIERGE_JOB_ID in the environment.
    return [
        "claude",
        "--remote-control",
        f"[{job_id}] {title}",
        "--permission-mode",
        config.PERMISSION_MODE,
        "--append-system-prompt-file",
        str(prompt_file),
        f"Your job id is {job_id}.\n\n{brief}",
    ]


def resolve_cwd(cwd: str) -> Path:
    """Reject anything that is not one of the repos in concierge.toml.

    tmux forking a shell says nothing about the command surviving: a bad path
    makes `cd` fail, `&&` short-circuit, and the window close instantly.
    """
    resolved = Path(cwd).expanduser().resolve()
    if resolved not in config.SETTINGS.repo_paths():
        raise ValueError(f"cwd not a permitted repo: {cwd}")
    return resolved


def session_id_from_url(rc_url: str | None) -> str | None:
    """https://claude.ai/code/<session-id> — what makes --resume possible."""
    if not rc_url:
        return None
    return rc_url.rstrip("/").rsplit("/", 1)[-1] or None


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
    repo = str(resolve_cwd(cwd))

    jobs = registry.load(state_path)
    job_id = registry.allocate_id(jobs)

    # Rendered rather than read straight from prompts/: the committed template
    # names no one's paths, so this installation's own are pasted in here.
    prompt_file = settings.render_prompt("job")
    argv = claude_argv(job_id, title, brief, prompt_file)
    tmux.new_window(
        config.TMUX_SESSION,
        job_id,
        tmux.build_shell_command(repo, argv, {"CONCIERGE_JOB_ID": job_id}),
    )

    poller = poller or poll_for_rc_url
    rc_url = poller(job_id)

    # No URL can mean a slow start — or a window that died on the first line.
    died = rc_url is None and job_id not in tmux.list_windows(config.TMUX_SESSION)

    registry.upsert(
        job_id,
        state_path,
        id=job_id,
        title=title,
        status="failed" if died else "running",
        chat_id=chat_id,
        root_message_id=root_message_id,
        cwd=repo,
        task_folder=task_folder,
        tmux_window=job_id,
        brief=brief,
        claude_session_id=session_id_from_url(rc_url),
        rc_url=rc_url,
        opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    registry.remember_chat(chat_id, state_path)

    if died:
        raise RuntimeError(
            f"job {job_id} died immediately — check the brief and cwd"
        )
    return registry.load(state_path)[job_id]
