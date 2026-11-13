"""Spawn a job: one tmux window, one Claude Code session, one registry entry."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from concierge import config, registry, settings, tmuxctl


def claude_argv(
    job_id: str, title: str, brief: str | None, prompt_file: Path, session_id: str | None = None
) -> list[str]:
    """`brief=None` resumes `session_id`; otherwise a new session starts under it.

    Choosing the session id up front is what lets a job be resumed later: the
    Remote Control URL's id is a different one (session_01…), measured 2026-11-13.
    Resuming keeps that URL, so a link to the chat survives the reaper.
    """
    # The id leads the brief so the model can read it in prose too; the
    # authoritative copy is CONCIERGE_JOB_ID in the environment.
    argv = [
        "claude",
        "--remote-control",
        f"[{job_id}] {title}",
        "--permission-mode",
        config.PERMISSION_MODE,
        "--append-system-prompt-file",
        str(prompt_file),
    ]
    if brief is None:
        return argv + ["--resume", session_id]
    if session_id:
        argv += ["--session-id", session_id]
    return argv + [f"Your job id is {job_id}.\n\n{brief}"]


def resolve_cwd(cwd: str) -> Path:
    """Reject anything that is not one of the repos in concierge.toml.

    tmux forking a shell says nothing about the command surviving: a bad path
    makes `cd` fail, `&&` short-circuit, and the window close instantly.
    """
    resolved = Path(cwd).expanduser().resolve()
    if resolved not in config.SETTINGS.repo_paths():
        raise ValueError(f"cwd not a permitted repo: {cwd}")
    return resolved


TRANSCRIPTS = Path.home() / ".claude" / "projects"


def rc_url_from_transcript(session_id: str | None, root: Path | None = None) -> str | None:
    """The Remote Control URL Claude Code logged for this session (its last bridge_status).

    The pane is no good for this: the URL is often redrawn away before anyone looks,
    which is why no job had a link before 2026-11-13. The transcript keeps it.
    """
    if not session_id:
        return None
    url = None
    for path in (root or TRANSCRIPTS).glob(f"*/{session_id}.jsonl"):
        for line in path.read_text(errors="replace").splitlines():
            if '"bridge_status"' in line:
                try:
                    url = json.loads(line).get("url") or url
                except ValueError:
                    pass
    return url


def poll_for_rc_url(
    window: str,
    *,
    attempts: int = 20,
    delay: float = 1.0,
    capture=None,
    sleeper=None,
    session_id: str | None = None,
) -> str | None:
    """Wait for the Remote Control URL: the transcript first, the pane as a fallback.

    A miss is not fatal: the session is named '[A3] <title>' so it is still
    findable in the session list at claude.ai/code.
    """
    capture = capture or (lambda w: tmuxctl.capture(config.TMUX_SESSION, w))
    sleeper = sleeper or time.sleep

    for i in range(attempts):
        url = rc_url_from_transcript(session_id) or tmuxctl.extract_rc_url(capture(window))
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
    ticktick_task_id: str | None = None,
    state_path: Path | None = None,
    tmux=tmuxctl,
    poller=None,
) -> dict:
    repo = str(resolve_cwd(cwd))
    session_id = str(uuid.uuid4())

    jobs = registry.load(state_path)
    job_id = registry.allocate_id(jobs)

    # Rendered rather than read straight from prompts/: the committed template
    # names no one's paths, so this installation's own are pasted in here.
    prompt_file = settings.render_prompt("job")
    argv = claude_argv(job_id, title, brief, prompt_file, session_id)
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
        claude_session_id=session_id,
        ticktick_task_id=ticktick_task_id,
        rc_url=rc_url,
        opened_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    registry.remember_chat(chat_id, state_path)

    if died:
        raise RuntimeError(
            f"job {job_id} died immediately — check the brief and cwd"
        )
    return registry.load(state_path)[job_id]


def resume_job(job_id: str, *, state_path: Path | None = None, tmux=tmuxctl) -> dict:
    """Bring a closed job's chat back: same session, same history, same RC URL."""
    job = registry.load(state_path)[job_id]
    if not job.get("claude_session_id"):
        raise ValueError(f"{job_id} has no session id to resume")
    if job_id in tmux.list_windows(config.TMUX_SESSION):
        return job
    argv = claude_argv(job_id, job.get("title", job_id), None, settings.render_prompt("job"),
                       job["claude_session_id"])
    tmux.new_window(
        config.TMUX_SESSION,
        job_id,
        tmux.build_shell_command(job["cwd"], argv, {"CONCIERGE_JOB_ID": job_id}),
    )
    return registry.upsert(job_id, state_path, status="running")
