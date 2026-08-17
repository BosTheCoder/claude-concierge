"""ensure-up — idempotent, silent when healthy, safe every 5 minutes.

Called by both the logon task and the watchdog task. The watchdog is the
actual guarantee; the logon task only makes startup prompt.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from concierge import config, registry, settings, telegram, tmuxctl


def blocking_env(env: dict) -> list[str]:
    return [name for name in config.BLOCKING_ENV_VARS if env.get(name)]


def concierge_argv() -> list[str]:
    return [
        "claude",
        "--settings",
        config.CONCIERGE_SETTINGS,
        "--channels",
        f"plugin:{config.TELEGRAM_PLUGIN}",
        "--remote-control",
        "concierge",
        "--permission-mode",
        config.PERMISSION_MODE,
        "--append-system-prompt-file",
        str(settings.render_prompt("concierge")),
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


def concierge_alive(tmux) -> bool:
    """The session exists AND window 0 still has a claude process.

    Every job is a window in this same session, so a live job keeps the
    session up after the concierge's own process is OOM-killed or crashes.
    A bare has_session would report 'healthy' while messages pile up unread.
    """
    if not tmux.has_session(config.TMUX_SESSION):
        return False
    command = tmux.window_command(config.TMUX_SESSION, "0")
    return bool(command) and "claude" in command


def alert_destination(state_path: Path | None = None) -> str | None:
    """One destination, never a broadcast.

    The most recently touched job's chat — an active job winning a same-second
    tie — falling back to the last chat we spoke to, which is the only thing
    that exists on a fresh registry.
    """
    rows = [j for j in registry.load(state_path).values() if j.get("chat_id")]
    if rows:
        newest = max(
            rows,
            key=lambda j: (
                j.get("last_update") or "",
                j.get("status") in config.ACTIVE_STATUSES,
            ),
        )
        return newest["chat_id"]
    return registry.last_chat(state_path)


def _default_notifier(message: str, state_path: Path | None = None) -> None:
    """Best effort. A broken notify must never stop the supervisor."""
    # Always leave a trace: a hidden detached run with no destination at all
    # would otherwise swallow the one alert that matters most.
    print(message, file=sys.stderr)

    chat_id = alert_destination(state_path)
    if not chat_id:
        return
    try:
        telegram.send(chat_id, message)
    except Exception as exc:  # noqa: BLE001 - never let notify kill the supervisor
        print(f"concierge: notify to {chat_id} failed: {exc}", file=sys.stderr)


def _start_concierge(tmux) -> None:
    """Start window 0, whether or not the session is already there.

    If jobs are still running the session exists and new_session would fail,
    so replace window 0 in place instead.
    """
    shell_command = tmux.build_shell_command(
        str(config.SETTINGS.default_repo.path), concierge_argv()
    )
    if not tmux.has_session(config.TMUX_SESSION):
        tmux.new_session(config.TMUX_SESSION, "0", shell_command)
        return
    if "0" in tmux.list_windows(config.TMUX_SESSION):
        tmux.kill_window(config.TMUX_SESSION, "0")
    tmux.new_window(config.TMUX_SESSION, "0", shell_command)


def ensure_up(
    *,
    env: dict | None = None,
    tmux=tmuxctl,
    state_path: Path | None = None,
    notifier=None,
) -> str:
    env = os.environ if env is None else env
    notifier = notifier or (lambda msg: _default_notifier(msg, state_path))

    if concierge_alive(tmux):
        # Healthy. Say nothing, and never interrupt a live turn.
        return "healthy"

    blocked = blocking_env(env)
    if blocked:
        notifier(
            "concierge NOT started — these env vars break Remote Control or "
            f"Channels: {', '.join(blocked)}"
        )
        return "blocked"

    try:
        _start_concierge(tmux)
    except RuntimeError as exc:
        if "duplicate session" in str(exc):
            # The logon task and the 5-minute watchdog can fire together and
            # both see no session. The loser lost a race, not a concierge.
            return "healthy"
        notifier(f"concierge failed to start: {exc}")
        raise

    jobs = registry.load(state_path)
    orphans = reconcile(jobs, tmux.list_windows(config.TMUX_SESSION))
    for job_id in orphans:
        registry.upsert(job_id, state_path, status="orphaned")
        title = jobs[job_id].get("title", "")
        notifier(
            f"[{job_id}] {title} was mid-flight when the machine restarted. "
            f"Reply `respawn {job_id}` to start it again from its task folder, "
            f"or /kill {job_id} to close it."
        )

    now = datetime.now(timezone.utc)
    remaining = registry.load(state_path)
    stale = prunable(remaining, now)
    if stale:
        for job_id in stale:
            # The prune is what returns an id to the pool, so the window it
            # names has to go with it — a finished job leaves its REPL open.
            window = remaining[job_id].get("tmux_window")
            if window:
                tmux.kill_window(config.TMUX_SESSION, window)
            remaining.pop(job_id, None)
        registry.save(remaining, state_path)

    return "started"
