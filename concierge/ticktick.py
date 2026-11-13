"""Read-only TickTick for the tasks page: every open task, cached briefly.

Talks to TickTick's MCP endpoint with Claude Code's own TickTick login, the way
ticktick-triage does. That token has no refresh: when it expires the page says
so, and re-connecting TickTick via /mcp in Claude Code fixes it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from concierge import registry

MCP_URL = "https://mcp.ticktick.com/"
CREDENTIALS = Path.home() / ".claude/.credentials.json"


def token() -> str:
    creds = json.loads(CREDENTIALS.read_text())
    for key, entry in creds.get("mcpOAuth", {}).items():
        if key.startswith("ticktick|") and entry.get("accessToken"):
            if entry.get("expiresAt", 0) / 1000 < time.time():
                raise RuntimeError("TickTick login expired: reconnect TickTick via /mcp in Claude Code")
            return entry["accessToken"]
    raise RuntimeError("TickTick isn't connected in Claude Code (/mcp)")


def call(name: str, arguments: dict):
    req = urllib.request.Request(MCP_URL, data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).encode(),
        headers={"Authorization": f"Bearer {token()}", "Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    if "error" in resp or resp["result"].get("isError"):
        raise RuntimeError(f"TickTick {name}: {resp.get('error') or resp['result']}")
    sc = resp["result"].get("structuredContent")
    return sc.get("result", sc) if isinstance(sc, dict) else json.loads(resp["result"]["content"][0]["text"])


CACHE_SECONDS = 600
_cache: dict = {"at": 0.0, "tasks": []}
_lock = threading.Lock()
KEEP = ("id", "projectId", "title", "dueDate", "isAllDay", "timeZone", "priority", "tags", "parentId", "kind",
        "etag", "repeatFlag", "status")


def _slim(t: dict, project: dict) -> dict:
    body = t.get("content") or t.get("desc") or ""
    items = [i.get("title") or "" for i in t.get("items") or []]
    return {k: t.get(k) for k in KEEP} | {"content": body, "items": items,
                                          "list": project["name"], "groupId": project.get("groupId")}


FILTER_CAP = 200  # filter_tasks silently stops at 200 results
COUNTS = Path(__file__).resolve().parent.parent / "state" / "ticktick-counts.json"


def group_lists(list_ids, counts, limit):
    """Pack lists into filter_tasks calls whose last-known open-task totals stay under `limit`.
    A list with no known count, or one that alone reaches the limit, gets a call of its own.
    Copied from ticktick-triage (core.group_lists)."""
    groups, cur, size = [], [], 0
    for lid in sorted(list_ids, key=lambda l: -counts.get(l, limit)):
        n = counts.get(lid)
        if n is None or n >= limit:
            groups.append([lid])
        elif size + n <= limit:
            cur.append(lid)
            size += n
        else:
            groups.append(cur)
            cur, size = [lid], n
    return groups + ([cur] if cur else [])


def _fetch() -> list[dict]:
    """~8 calls once list sizes are known (TickTick allows 300 per 5 minutes, shared with
    every other TickTick tool); one per list the very first time."""
    # Note lists (Gratitude Journal…) never show in his filters. A note inside a task list does
    # (Learning Log, tagged #today), so only whole note lists are skipped.
    projects = {p["id"]: p for p in call("list_projects", {}) if not p.get("closed") and p.get("kind") != "NOTE"}
    projects["inbox115800449"] = projects.pop("inbox", {"id": "inbox", "name": "Inbox"})
    counts = json.loads(COUNTS.read_text()) if COUNTS.exists() else {}

    def read(group):
        got = call("filter_tasks", {"filter": {"projectIds": group, "status": [0]}}) if len(group) > 1 else []
        if len(group) == 1 or len(got) >= FILTER_CAP - 20:  # one list, or possibly truncated
            got = [t for pid in group for t in
                   call("get_project_with_undone_tasks", {"project_id": projects[pid]["id"]}).get("tasks") or []]
        return got

    with ThreadPoolExecutor(4) as pool:
        raw = [t for ts in pool.map(read, group_lists(list(projects), counts, FILTER_CAP - 20)) for t in ts]
    fresh_counts = {pid: 0 for pid in projects}
    for t in raw:
        fresh_counts[t["projectId"]] = fresh_counts.get(t["projectId"], 0) + 1
    registry._atomic_write(COUNTS, json.dumps(fresh_counts))
    return [_slim(t, projects.get(t["projectId"], {"name": "?"})) for t in raw]


def _refresh() -> None:
    try:
        tasks = _fetch()
        _cache.update(at=time.time(), tasks=tasks, error=None)
    except Exception as exc:  # noqa: BLE001 - the page shows it beside the stale list
        _cache.update(error=str(exc), failed_at=time.time())
    finally:
        _lock.release()


def all_open(fresh: bool = False) -> dict:
    """Every open task in every open list: a handful of filter_tasks calls.

    Answers from the cache straight away and refreshes behind it once it is older than
    CACHE_SECONDS, so the page opens instantly. Only the very first call waits.
    """
    # After a failure (rate limit: 300 calls per 5 min, shared with all his TickTick tools) wait a
    # minute before trying again, or every page load would keep the limit topped up.
    backing_off = time.time() - _cache.get("failed_at", 0) < 60
    if (fresh or time.time() - _cache["at"] > CACHE_SECONDS) and not backing_off and _lock.acquire(blocking=False):
        if _cache["tasks"]:
            threading.Thread(target=_refresh, daemon=True).start()
        else:
            _refresh()
    elif not _cache["tasks"]:
        with _lock:  # someone else's first fetch is running
            pass
    if not _cache["tasks"] and _cache.get("error"):
        raise RuntimeError(_cache["error"])
    return {"tasks": _cache["tasks"], "at": _cache["at"], "error": _cache.get("error")}


def one(task_id: str) -> dict:
    t = call("get_task_by_id", {"task_id": task_id})
    t = t.get("result", t)
    return _slim(t, {"name": "", "groupId": None}) | {"status": t.get("status")}


class Conflict(Exception):
    """The task changed in TickTick since the page loaded it. Carries the task as it is now."""

    def __init__(self, msg: str, task: dict):
        super().__init__(msg)
        self.task = task


def _dates(cur: dict, day: str | None) -> dict:
    """Move a task to `day` (YYYY-MM-DD) or clear its date. Keeps the time of a timed task and the
    length of a span; a task with no date becomes all-day."""
    if day is None:
        return {"startDate": None, "dueDate": None}
    tz = ZoneInfo(cur.get("timeZone") or "Europe/London")
    target = date.fromisoformat(day)
    if not cur.get("dueDate"):
        new = datetime.combine(target, datetime.min.time(), tz)
        return {"startDate": new.isoformat(), "dueDate": new.isoformat(), "isAllDay": True, "timeZone": tz.key}
    due = datetime.fromisoformat(cur["dueDate"]).astimezone(tz)
    shift = timedelta(days=(target - due.date()).days)
    out = {"dueDate": (due + shift).isoformat()}
    if cur.get("startDate"):
        out["startDate"] = (datetime.fromisoformat(cur["startDate"]).astimezone(tz) + shift).isoformat()
    return out


def update(task_id: str, etag: str, changes: dict) -> dict:
    """Write the page's edits: content, priority, day (date or None), status (2 done, -1 won't do).
    Refuses if the task changed in TickTick since the page read it, the way Tickfind does.
    Returns the task as TickTick now has it; the cache follows."""
    cur = call("get_task_by_id", {"task_id": task_id})
    cur = cur.get("result", cur)
    project = {"name": "", "groupId": None}
    old = next((t for t in _cache["tasks"] if t["id"] == task_id), None)
    if old:
        project = {"name": old["list"], "groupId": old["groupId"]}
    if cur.get("status"):
        raise Conflict("It's already done or won't-do in TickTick.", _slim(cur, project))
    if etag != cur.get("etag"):
        raise Conflict("It changed in TickTick since this page loaded it. This is the latest; try again.",
                       _slim(cur, project))
    body = {"id": task_id, "projectId": cur["projectId"]}
    if "content" in changes:
        body["desc" if cur.get("kind") == "CHECKLIST" else "content"] = changes["content"]
    if "priority" in changes:
        body["priority"] = changes["priority"]
    if "day" in changes:
        body |= _dates(cur, changes["day"])
    if changes.get("status") == -1:
        body["status"] = -1
    if len(body) > 2:
        call("update_task", {"task_id": task_id, "task": body})
    if changes.get("status") == 2:  # complete_task, not status=2: a repeating task moves to its next date
        call("complete_task", {"project_id": cur["projectId"], "task_id": task_id})
    now = call("get_task_by_id", {"task_id": task_id})
    now = _slim(now.get("result", now), project)
    # ponytail: a background refresh already in flight can put the old copy back until the next one
    _cache["tasks"] = [t for t in _cache["tasks"] if t["id"] != task_id] + ([] if now["status"] else [now])
    return now
