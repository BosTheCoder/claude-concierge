"""Scheduled jobs: a brief that spawns itself as a job at a set time.

`concierge later <when> <title> <brief>` queues one. `ensure-up` (every five
minutes, the most reliably run thing on the machine) spawns whatever is due,
so a scheduled job is at most one tick late and survives reboots: the queue is
a file, not a sleeping process.

Each entry fires once. It is marked fired *before* the spawn, so a spawn that
raises is recorded as an error and never retried — a broken brief must not
open a new window every five minutes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from concierge import config, registry

LATER_FILE = config.STATE_FILE.parent / "later.json"
LOCAL = ZoneInfo("Europe/London")


def parse_when(when: str) -> datetime:
    """ISO datetime; no offset means UK time, which is what he'll be thinking in."""
    dt = datetime.fromisoformat(when)
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL)


def load(path: Path | None = None) -> list[dict]:
    p = path or LATER_FILE
    return json.loads(p.read_text() or "[]") if p.exists() else []


def save(entries: list[dict], path: Path | None = None) -> None:
    registry._atomic_write(path or LATER_FILE, json.dumps(entries, indent=2))


def add(when: str, title: str, brief: str, cwd: str, chat_id: str,
        path: Path | None = None) -> dict:
    from concierge import spawn

    entry = {
        "when": parse_when(when).isoformat(),
        "title": title,
        "brief": brief,
        "cwd": str(spawn.resolve_cwd(cwd)),  # fail now, not at fire time
        "chat_id": chat_id,
        "fired": None,
    }
    entries = load(path)
    entries.append(entry)
    save(entries, path)
    return entry


def run_due(now: datetime | None = None, path: Path | None = None,
            spawner=None) -> str:
    """Spawn every due, unfired entry. Returns a one-line summary."""
    if spawner is None:
        from concierge.spawn import spawn_job as spawner
    now = now or datetime.now(timezone.utc)
    entries = load(path)
    due = [e for e in entries if not e["fired"] and datetime.fromisoformat(e["when"]) <= now]
    if not due:
        return "later: nothing due"
    out = []
    for e in due:
        e["fired"] = now.isoformat(timespec="seconds")
        save(entries, path)
        try:
            job = spawner(title=e["title"], brief=e["brief"], cwd=e["cwd"], chat_id=e["chat_id"])
            e["job"] = job["id"]
            out.append(f"{e['title']} -> {job['id']}")
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the rest
            e["error"] = str(exc)
            out.append(f"{e['title']} failed: {exc}")
        save(entries, path)
    return "later: " + "; ".join(out)
