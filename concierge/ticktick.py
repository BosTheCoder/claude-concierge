"""TickTick for the tasks page, on ticktick-core (shared with his other TickTick tools).

ticktick-core holds the client, the shared cache of open tasks and his rules; this file only
shapes tasks for the page and keeps the page fast: it answers from the shared copy straight
away and refreshes it behind the reply.
"""

from __future__ import annotations

import threading
import time

import ticktick_core as core
from ticktick_core import call  # noqa: F401 - taskchats uses ticktick.call

CACHE_SECONDS = 600
_state: dict = {"error": None, "failed_at": 0.0}
_refreshing = threading.Lock()
KEEP = ("id", "projectId", "title", "dueDate", "isAllDay", "timeZone", "priority", "tags", "parentId", "kind",
        "etag", "repeatFlag", "status", "modifiedTime", "createdTime")


def _slim(t: dict, list_name: str | None = None) -> dict:
    return {k: t.get(k) for k in KEEP} | {
        "content": t.get("content") or t.get("desc") or "", "items": [i.get("title") or "" for i in t.get("items") or []],
        "list": t.get("list", list_name or "")}


def _refresh(max_age: float) -> None:
    try:
        core.open_tasks(max_age)
        _state["error"] = None
    except Exception as exc:  # noqa: BLE001 - the page shows it beside the older copy
        _state.update(error=str(exc), failed_at=time.time())
    finally:
        _refreshing.release()


def all_open(fresh: bool = False) -> dict:
    """Every open task outside calendar lists, the views from his rules, and when TickTick was read.
    Only the very first call (no shared copy yet) waits for TickTick."""
    tasks, at = core.cached()
    # After a failure (rate limit: 300 calls per 5 min, shared with all his TickTick tools) wait a
    # minute before trying again, or every page load would keep the limit topped up.
    backing_off = time.time() - _state["failed_at"] < 60
    if (fresh or time.time() - at > CACHE_SECONDS) and not backing_off and _refreshing.acquire(blocking=False):
        max_age = 0 if fresh else CACHE_SECONDS
        if at:
            threading.Thread(target=_refresh, args=(max_age,), daemon=True).start()
        else:
            _refresh(max_age)
            tasks, at = core.cached()
    if not at and _state["error"]:
        raise RuntimeError(_state["error"])
    return {"tasks": [_slim(t) for t in tasks if not core.is_calendar_list(t.get("list"))],
            "views": core.RULES["views"], "at": at, "error": _state["error"]}


def one(task_id: str) -> dict:
    return _slim(core.call("get_task_by_id", {"task_id": task_id}))


class Conflict(Exception):
    """The task changed in TickTick since the page loaded it. Carries the task as it is now."""

    def __init__(self, msg: str, task: dict):
        super().__init__(msg)
        self.task = task


def update(task_id: str, etag: str, changes: dict) -> dict:
    """Write the page's edits: content, priority, day (date or None), status (2 done, -1 won't do).
    Refuses if the task changed in TickTick since the page read it, the way Tickfind does.
    Returns the task as TickTick now has it; the shared copy follows."""
    cur = core.call("get_task_by_id", {"task_id": task_id})
    list_name = next((t["list"] for t in core.cached()[0] if t["id"] == task_id), "")
    if cur.get("status"):
        raise Conflict("It's already done or won't-do in TickTick.", _slim(cur, list_name))
    if etag != cur.get("etag"):
        raise Conflict("It changed in TickTick since this page loaded it. This is the latest; try again.",
                       _slim(cur, list_name))
    body = {"id": task_id, "projectId": cur["projectId"]}
    if "content" in changes:
        body["desc" if cur.get("kind") == "CHECKLIST" else "content"] = changes["content"]
    if "priority" in changes:
        body["priority"] = changes["priority"]
    if "day" in changes:
        body |= core.move_to(cur, changes["day"])
    if changes.get("status") == -1:
        body["status"] = -1
    if len(body) > 2:
        core.call("update_task", {"task_id": task_id, "task": body})
    if changes.get("status") == 2:  # complete_task, not status=2: a repeating task moves to its next date
        core.call("complete_task", {"project_id": cur["projectId"], "task_id": task_id})
    now = core.call("get_task_by_id", {"task_id": task_id}) | {"list": list_name}
    core.put(now)
    return _slim(now)
