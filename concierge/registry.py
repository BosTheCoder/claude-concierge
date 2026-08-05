"""The job registry — the source of truth that survives a restart."""

from __future__ import annotations

import json
import os
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from concierge import config


def _path(path: Path | None) -> Path:
    return path or config.STATE_FILE


def _chat_path(path: Path | None) -> Path:
    """The last-chat file, which lives beside the registry it belongs to.

    `path` is the registry path throughout, so an injected state path keeps
    both files together and the callers hermetic.
    """
    return config.LAST_CHAT_FILE if path is None else path.parent / "last_chat"


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write a sibling temp file then rename over the target. An in-place
    # truncate loses the file if the process dies mid-write, and needs write
    # permission on the file rather than the directory.
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def remember_chat(chat_id: str, path: Path | None = None) -> None:
    """Record the last chat we spoke to, so alerts have somewhere to go."""
    if not chat_id:
        return
    _atomic_write(_chat_path(path), str(chat_id).strip() + "\n")


def last_chat(path: Path | None = None) -> str | None:
    p = _chat_path(path)
    if not p.exists():
        return None
    return p.read_text().strip() or None


def load(path: Path | None = None) -> dict[str, dict]:
    p = _path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def save(jobs: dict[str, dict], path: Path | None = None) -> None:
    _atomic_write(_path(path), json.dumps(jobs, indent=2, sort_keys=True))


def active(jobs: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in jobs.items() if v.get("status") in config.ACTIVE_STATUSES}


def allocate_id(jobs: dict[str, dict]) -> str:
    """Pick an id no row in the registry has ever held.

    Finished ids are NOT reusable. Nothing kills a job's tmux window when the
    work ends — `claude '<brief>'` leaves an interactive REPL behind — and
    tmux allows duplicate window names, so a recycled id would capture the
    wrong pane, overwrite the wrong row, and let /kill destroy a live session.
    The 7-day prune is what returns ids to the pool.
    """
    taken = set(jobs)
    candidates = [
        f"{letter}{digit}"
        for letter in config.ID_LETTERS
        for digit in config.ID_DIGITS
        if f"{letter}{digit}" not in taken
    ]
    if not candidates:
        raise RuntimeError("no free job id — the registry is full, prune it")
    # Shuffle so consecutive jobs don't get confusingly adjacent ids.
    return random.choice(candidates)


def upsert(job_id: str, path: Path | None = None, **fields) -> dict:
    jobs = load(path)
    job = jobs.setdefault(job_id, {})
    job.update(fields)
    job["last_update"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save(jobs, path)
    return job
