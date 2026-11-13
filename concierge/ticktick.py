"""Read-only TickTick for the tasks page: lists and their open tasks.

Talks to TickTick's MCP endpoint with Claude Code's own TickTick login, the way
ticktick-triage does. That token has no refresh: when it expires the page says
so, and re-connecting TickTick via /mcp in Claude Code fixes it.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

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


def lists() -> list[dict]:
    return [{"id": p["id"], "name": p["name"]} for p in call("list_projects", {})
            if not p.get("closed") and p.get("kind") != "NOTE"]


def open_tasks(project_id: str) -> list[dict]:
    """Top-level open tasks, newest first; subtasks ride along as their parent's titles."""
    tasks = call("get_project_with_undone_tasks", {"project_id": project_id}).get("tasks") or []
    by_id = {t["id"]: t for t in tasks}
    keep = ("id", "projectId", "title", "content", "desc", "dueDate", "priority", "tags", "parentId")
    out = [{k: t.get(k) for k in keep} | {"subtasks": [by_id[c]["title"] for c in t.get("childIds") or [] if c in by_id]}
           for t in tasks if not t.get("parentId") or t["parentId"] not in by_id]
    out.sort(key=lambda t: t["id"], reverse=True)  # ObjectIds start with their creation time
    return out
