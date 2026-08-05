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


def load(path: Path | None = None) -> dict[str, dict]:
    p = _path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def save(jobs: dict[str, dict], path: Path | None = None) -> None:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: write a sibling temp file then rename over the target. An
    # in-place truncate loses the file if the process dies mid-write, and
    # needs write permission on the file rather than the directory.
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".jobs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(jobs, fh, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def active(jobs: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in jobs.items() if v.get("status") in config.ACTIVE_STATUSES}


def allocate_id(jobs: dict[str, dict]) -> str:
    taken = set(active(jobs))
    candidates = [
        f"{letter}{digit}"
        for letter in config.ID_LETTERS
        for digit in config.ID_DIGITS
        if f"{letter}{digit}" not in taken
    ]
    if not candidates:
        raise RuntimeError("no free job id — close some jobs first")
    # Shuffle so consecutive jobs don't get confusingly adjacent ids.
    return random.choice(candidates)


def upsert(job_id: str, path: Path | None = None, **fields) -> dict:
    jobs = load(path)
    job = jobs.setdefault(job_id, {})
    job.update(fields)
    job["last_update"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save(jobs, path)
    return job
