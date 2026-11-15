"""Plannotator Inbox replies waiting for a Claude chat that has closed, and the Resume button.

When he answers in the Inbox and the chat that asked has closed, the Inbox holds the reply
("Queued for Claude Code; it gets this when its session is back") and delivers it once that
chat is resumed. The Inbox can't resume a chat, so the Review page offers it.

Built to survive Plannotator updates: it reads only Plannotator's documented agent commands
(`inbox projects --json`, `inbox call read_thread`) and Claude's own transcripts, never the
Inbox's data files or page. If those commands change shape, the Review page shows the error
instead of quietly showing nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from concierge import config, registry, sessions, spawn, tmuxctl

PLANNOTATOR = shutil.which("plannotator") or str(Path.home() / ".local" / "bin" / "plannotator")


class PlannotatorChanged(RuntimeError):
    """Plannotator answered, but not in the shape this reads: an update changed it."""


def _call(*args: str) -> dict:
    result = subprocess.run([PLANNOTATOR, *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"plannotator {args[0]} {args[1]}: {result.stderr.strip() or 'failed'}")
    return json.loads(result.stdout)


def person_replies(run=_call) -> list[dict]:
    """Open threads whose last word is his, in every Inbox project."""
    out = []
    try:
        for project in run("inbox", "projects", "--json")["projects"]:
            if not os.path.isdir(project["root"]):  # read_thread refuses a folder that's gone
                continue
            listing = run("inbox", "call", "read_thread",
                          json.dumps({"asked_by": "anyone", "project_path": project["root"], "limit": 50}))
            for t in listing["threads"]:
                if t["last_author"] == "person" and t.get("last_person_message_id"):
                    out.append({"thread_id": t["thread_id"], "reply_id": t["last_person_message_id"],
                                "subject": t.get("thread_name") or t["subject"],
                                "at": t.get("last_person_at"), "project": project["name"]})
    except (KeyError, TypeError) as exc:
        raise PlannotatorChanged(f"Plannotator's reply no longer has {exc}") from exc
    return out


def _is_send(use: dict) -> bool:
    if "plannotator" in use.get("name", ""):
        return True
    return use.get("name") == "Bash" and "plannotator inbox call send_message" in str(use.get("input"))


def _read(path: Path, thread_id: str, reply_id: str) -> dict:
    """Did this chat send into the thread, and has it seen his reply?"""
    sends, sent, cwd = set(), False, None
    text = path.read_text(errors="replace")
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        cwd = entry.get("cwd") or cwd
        content = (entry.get("message") or {}).get("content")
        for item in content if isinstance(content, list) else []:
            if item.get("type") == "tool_use" and _is_send(item):
                sends.add(item.get("id"))
            elif (item.get("type") == "tool_result" and item.get("tool_use_id") in sends
                  and thread_id in json.dumps(item.get("content"))):
                sent = True
    # The reply's id anywhere in the chat means it got it (the wake, a read_thread, its answer).
    return {"sent": sent, "delivered": reply_id in text, "cwd": cwd}


def owner(thread_id: str, reply_id: str, root: Path = spawn.TRANSCRIPTS) -> dict | None:
    """The newest Claude chat that sent into this thread, from its transcript."""
    grep = [shutil.which("rg") or "rg", "-l", "-F", thread_id, str(root), "--glob", "*.jsonl"]
    try:
        files = subprocess.run(grep, capture_output=True, text=True, timeout=30).stdout.split()
    except FileNotFoundError:
        files = [str(p) for p in root.glob("*/*.jsonl") if thread_id in p.read_text(errors="replace")]
    best = None
    for name in files:
        path = Path(name)
        found = _read(path, thread_id, reply_id)
        if found["sent"] and (best is None or path.stat().st_mtime > best["mtime"]):
            best = {**found, "session_id": path.stem, "mtime": path.stat().st_mtime}
    return best


def live_sessions() -> set[str]:
    return {u[1] for pid, proc in sessions.scan().items()
            if sessions.is_claude(proc) and (u := sessions.session_uuid(pid))}


def waiting(run=_call, root: Path = spawn.TRANSCRIPTS, live=live_sessions) -> list[dict]:
    """His Inbox replies that a closed chat hasn't received yet. A running chat gets its own."""
    alive = live()
    out = []
    for reply in person_replies(run):
        chat = owner(reply["thread_id"], reply["reply_id"], root)
        if chat and not chat["delivered"] and chat["session_id"] not in alive and chat["cwd"]:
            out.append({**reply, "session_id": chat["session_id"], "cwd": chat["cwd"]})
    return out


def resume(session_id: str, *, found=None, tmux=tmuxctl, state_path: Path | None = None) -> str:
    """Reopen a chat from the waiting list (nothing else) so the Inbox can hand it his reply."""
    chat = next((w for w in (found if found is not None else waiting()) if w["session_id"] == session_id), None)
    if chat is None:
        raise ValueError("that chat has nothing waiting")
    for job_id, job in registry.load(state_path).items():
        if job.get("claude_session_id") == session_id:  # a Concierge job: keep its prompt and RC name
            spawn.resume_job(job_id, state_path=state_path, tmux=tmux)
            return job_id
    window = f"inbox-{session_id[:8]}"
    if window not in tmux.list_windows(config.TMUX_SESSION):
        argv = ["claude", "--remote-control", chat["subject"], "--permission-mode", config.PERMISSION_MODE,
                "--resume", session_id]
        tmux.new_window(config.TMUX_SESSION, window, tmux.build_shell_command(chat["cwd"], argv))
    return window
