"""GitHub link building and age humanizing — domain logic used by the CLI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from concierge import config, publish


def github_base(cwd: str, repos=None, runner=None) -> str | None:
    """The GitHub URL for the repo at `cwd`, if there is one.

    The repo's own `origin` is asked first. concierge.toml only lists the repos
    a job may be *spawned* in, so a configured url is a second source of truth
    that goes stale the moment a remote is renamed — and says nothing at all
    about a repo nobody thought to add. The config value stays as the fallback
    for a repo with no origin.
    """
    slug = publish.remote_slug(cwd, runner)
    if slug:
        return f"https://github.com/{slug}"

    target = Path(cwd).expanduser().resolve()
    for repo in config.REPOS if repos is None else repos:
        if repo.path.resolve() == target:
            return repo.github
    return None


def github_link(
    cwd: str,
    task_folder: str,
    filename: str,
    repos=None,
    branch: str | None = None,
    runner=None,
) -> str:
    """A blob URL for one file. `branch` must be the ref the push landed on."""
    base = github_base(cwd, repos, runner)
    if not base:
        raise ValueError(f"no github url configured for repo: {cwd}")
    ref = branch or publish.current_branch(cwd, runner)
    folder = f"{task_folder.strip('/')}/" if task_folder else ""
    return f"{base}/blob/{ref}/{folder}{filename}"


def humanize_age(opened_at: str, now: datetime) -> str:
    delta = now - datetime.fromisoformat(opened_at)
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)}m"
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"
