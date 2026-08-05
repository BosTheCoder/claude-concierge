"""GitHub link building and age humanizing — domain logic used by the CLI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from concierge import config

GITHUB = {
    str(config.TASKS_REPO): "https://github.com/BosTheCoder/tasks",
    str(config.NPM_REPO):
        "https://github.com/BosTheCoder/nyakundi-property-management",
}


def github_link(cwd: str, task_folder: str, filename: str) -> str:
    base = GITHUB.get(str(Path(cwd)))
    if not base:
        raise ValueError(f"unknown repo: {cwd}")
    return f"{base}/blob/main/{task_folder}/{filename}"


def humanize_age(opened_at: str, now: datetime) -> str:
    delta = now - datetime.fromisoformat(opened_at)
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"
