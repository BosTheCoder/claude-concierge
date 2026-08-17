"""GitHub link building and age humanizing — domain logic used by the CLI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from concierge import config


def github_base(cwd: str, repos=None) -> str | None:
    """The GitHub URL configured for the repo at `cwd`, if there is one."""
    target = Path(cwd).expanduser().resolve()
    for repo in config.REPOS if repos is None else repos:
        if repo.path.resolve() == target:
            return repo.github
    return None


def github_link(cwd: str, task_folder: str, filename: str, repos=None) -> str:
    base = github_base(cwd, repos)
    if not base:
        raise ValueError(f"no github url configured for repo: {cwd}")
    return f"{base}/blob/main/{task_folder}/{filename}"


def humanize_age(opened_at: str, now: datetime) -> str:
    delta = now - datetime.fromisoformat(opened_at)
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"
