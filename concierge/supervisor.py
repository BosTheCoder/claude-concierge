"""ensure-up — idempotent, silent when healthy, safe every 5 minutes.

Called by both the logon task and the watchdog task. The watchdog is the
actual guarantee; the logon task only makes startup prompt.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from concierge import config, registry, telegram, tmuxctl


def blocking_env(env: dict) -> list[str]:
    return [name for name in config.BLOCKING_ENV_VARS if env.get(name)]


def concierge_argv() -> list[str]:
    return [
        "claude",
        "--channels",
        "plugin:telegram@claude-plugins-official",
        "--remote-control",
        "concierge",
        "--append-system-prompt-file",
        str(config.PROMPTS_DIR / "concierge.md"),
    ]


def reconcile(jobs: dict, windows: list[str]) -> list[str]:
    """Active jobs whose tmux window no longer exists."""
    live = set(windows)
    return sorted(
        job_id
        for job_id, job in registry.active(jobs).items()
        if job.get("tmux_window") not in live
    )


def prunable(jobs: dict, now: datetime, days: int = 7) -> list[str]:
    cutoff = now - timedelta(days=days)
    return sorted(
        job_id
        for job_id, job in jobs.items()
        if job.get("status") not in config.ACTIVE_STATUSES
        and job.get("last_update")
        and datetime.fromisoformat(job["last_update"]) < cutoff
    )


def _default_notifier(message: str, state_path: Path | None = None) -> None:
    """Best effort. A broken notify must never stop the supervisor."""
    jobs = registry.load(state_path)
    chat_ids = {j.get("chat_id") for j in jobs.values() if j.get("chat_id")}
    for chat_id in chat_ids:
        try:
            telegram.send(chat_id, message)
        except Exception as exc:  # noqa: BLE001 - never let notify kill the supervisor
            print(f"concierge: notify to {chat_id} failed: {exc}", file=sys.stderr)


def ensure_up(
    *,
    env: dict | None = None,
    tmux=tmuxctl,
    state_path: Path | None = None,
    notifier=None,
) -> str:
    env = os.environ if env is None else env
    notifier = notifier or (lambda msg: _default_notifier(msg, state_path))

    if tmux.has_session(config.TMUX_SESSION):
        # Healthy. Say nothing, and never interrupt a live turn.
        return "healthy"

    blocked = blocking_env(env)
    if blocked:
        notifier(
            "concierge NOT started — these env vars break Remote Control or "
            f"Channels: {', '.join(blocked)}"
        )
        return "blocked"

    argv = concierge_argv()
    try:
        tmux.new_session(
            config.TMUX_SESSION,
            "0",
            tmux.build_shell_command(str(config.TASKS_REPO), argv),
        )
    except RuntimeError as exc:
        notifier(f"concierge failed to start: {exc}")
        raise

    jobs = registry.load(state_path)
    orphans = reconcile(jobs, tmux.list_windows(config.TMUX_SESSION))
    for job_id in orphans:
        registry.upsert(job_id, state_path, status="orphaned")
        title = jobs[job_id].get("title", "")
        notifier(
            f"[{job_id}] {title} was mid-flight when the machine restarted. "
            f"Reply to resume it, or /kill {job_id} to close it."
        )

    now = datetime.now(timezone.utc)
    stale = prunable(registry.load(state_path), now)
    if stale:
        remaining = registry.load(state_path)
        for job_id in stale:
            remaining.pop(job_id, None)
        registry.save(remaining, state_path)

    return "started"
