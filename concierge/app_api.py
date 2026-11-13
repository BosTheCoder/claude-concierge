"""The threads app's HTTP API, served by the dashboard under /api/... and /app.

GETs are reads. Every POST goes through the dashboard's origin check first.
Anything that types into a session (say, answer, undo) runs on a worker thread:
tmuxctl.submit waits seconds for the box to clear, and the page polls for the result.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from concierge import config, registry, spawn, threads

APP_HTML = Path(__file__).with_name("app.html")
ROUTE = re.compile(r"^/api/threads/(\d+)(?:/(\w+))?$")
LIMITS = {"text": 8000, "spec": 4000, "rules": 8000, "note": 2000}


def manifest() -> str:
    return json.dumps({
        "name": "Concierge", "short_name": "Concierge", "start_url": "/app", "scope": "/app",
        "display": "standalone", "background_color": "#0e1116", "theme_color": "#0e1116",
        "icons": [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
        "shortcuts": [{"name": "New thread", "url": "/app#new"}],
        # The phone's share sheet: share text or a link to Concierge and it starts a thread.
        "share_target": {"action": "/app", "method": "GET",
                         "params": {"title": "title", "text": "text", "url": "url"}},
    })


def brief() -> dict | None:
    """The latest Morning Brief run: its headline and how many items need him."""
    app = config.SETTINGS.app
    if not app.brief_runs:
        return None
    runs = sorted(Path(app.brief_runs).expanduser().glob("*.json"))
    if not runs:
        return None
    try:
        run = json.loads(runs[-1].read_text())
    except (OSError, ValueError):
        return None
    items = run.get("items") or []
    return {"headline": (run.get("meta") or {}).get("headline", ""), "date": run.get("date"),
            "red": sum(1 for i in items if i.get("level") == "red"), "items": len(items), "url": app.brief_url}


def _bg(fn, *args, **kw) -> None:
    def run():
        db = threads.connect()
        try:
            fn(db, *args, **kw)
        finally:
            db.close()
    threading.Thread(target=run, daemon=True).start()


def get(path: str, query: dict) -> tuple[int, dict]:
    db = threads.connect()
    try:
        if path == "/api/threads":
            q = (query.get("q") or [""])[0][:200]
            return 200, {"threads": threads.home(db, q=q, closed=bool(query.get("closed"))), "brief": brief(),
                         "mode": (m := threads.active_mode(db)) and m["name"]}
        if m := ROUTE.match(path):
            try:
                return 200, threads.detail(db, int(m.group(1)))
            except KeyError:
                return 404, {"error": "no such thread"}
        if path.startswith("/api/alarms/"):
            row = db.execute("SELECT * FROM alarms WHERE id=?", (path.rsplit("/", 1)[1],)).fetchone()
            return (200, {k: row[k] for k in ("id", "level", "title", "body", "open")}) if row else (404, {"error": "no"})
        if path == "/api/settings":
            return 200, {"rules": threads._kv(db, "rules"),
                         "delegations": [dict(d) for d in db.execute(
                             "SELECT d.*, t.title FROM delegations d LEFT JOIN threads t ON t.id=d.thread "
                             "WHERE d.ended IS NULL ORDER BY d.id DESC")],
                         "modes": [dict(m) | {"allow": json.loads(m["allow"])} for m in db.execute("SELECT * FROM modes")],
                         "stats": threads.stats(db)}
        return 404, {"error": "no"}
    finally:
        db.close()


def _text(body: dict, key: str, required: bool = True) -> str:
    v = body.get(key) or ""
    if not isinstance(v, str) or len(v) > LIMITS.get(key, 500):
        raise ValueError(f"{key} must be text under {LIMITS.get(key, 500)} characters")
    if required and not v.strip():
        raise ValueError(f"{key} is empty")
    return v.strip()


def start_thread(db, text: str, *, spawner=None, lock=None) -> dict:
    """Quick capture: a job and its thread, back as soon as the window exists."""
    title = " ".join(text.split())[:60] or "New thread"
    brief_text = (f"Started from Kelvin's Concierge app. His message:\n<<<\n{text}\n>>>\n\n"
                  "Talk to him only through your thread: `concierge post`, `concierge ask` for decisions, "
                  "`concierge facts` for the key facts, `concierge close` when it's done.")
    chat = registry.last_chat() or ""
    with lock or threading.Lock():
        job = (spawner or spawn.spawn_job)(title=title, brief=brief_text, cwd=str(config.SETTINGS.default_repo.path),
                                          chat_id=chat, poller=lambda _w: None)
    th = threads.find_or_create(db, job=job["id"], title=title, pane=f"{config.TMUX_SESSION}:{job['id']}")
    threads.post(db, th, text, author="me")
    db.execute("UPDATE messages SET state='picked' WHERE thread=? AND author='me'", (th["id"],))
    return {"id": th["id"], "job": job["id"]}


def post(path: str, body: dict, *, lock=None, finish=None) -> tuple[int, dict]:
    db = threads.connect()
    try:
        if path == "/api/threads":
            started = start_thread(db, _text(body, "text"), lock=lock)
            if finish:
                threading.Thread(target=finish, args=(started["job"], str(config.SETTINGS.default_repo.path)),
                                 daemon=True).start()
            return 200, {"ok": True, **started}
        if path == "/api/settings/rules":
            threads._set_kv(db, "rules", _text(body, "rules", required=False))
            return 200, {"ok": True}
        if path == "/api/settings/mode":
            allow = [lv for lv in body.get("allow") or [] if lv in threads.LEVELS]
            fields = (_text(body, "name"), json.dumps(allow), int(body["start_day"]) % 7,
                      _hhmm(body["start_time"]), int(body["end_day"]) % 7, _hhmm(body["end_time"]),
                      1 if body.get("enabled", True) else 0)
            if body.get("id"):
                db.execute("UPDATE modes SET name=?, allow=?, start_day=?, start_time=?, end_day=?, end_time=?, "
                           "enabled=? WHERE id=?", (*fields, int(body["id"])))
            else:
                db.execute("INSERT INTO modes(name, allow, start_day, start_time, end_day, end_time, enabled) "
                           "VALUES (?,?,?,?,?,?,?)", fields)
            return 200, {"ok": True}
        if path == "/api/settings/mode/delete":
            db.execute("DELETE FROM modes WHERE id=?", (int(body["id"]),))
            return 200, {"ok": True}
        if path == "/api/delegations/end":
            threads.end_delegation(db, int(body["id"]))
            return 200, {"ok": True}
        m = ROUTE.match(path)
        if not m or not m.group(2):
            return 404, {"ok": False, "error": "no"}
        tid, action = int(m.group(1)), m.group(2)
        if not db.execute("SELECT 1 FROM threads WHERE id=?", (tid,)).fetchone():
            return 404, {"ok": False, "error": "no such thread"}
        if action == "say":
            _bg(threads.say, tid, _text(body, "text"))
        elif action == "answer":
            msg, choice = int(body["msg"]), _text(body, "choice")
            threads._decision(db, msg)  # refuse now (400) rather than on the worker
            _bg(threads.answer, msg, choice, note=_text(body, "note", required=False))
        elif action == "undo":
            _bg(threads.undo, int(body["msg"]))
        elif action == "close":
            threads.close(db, tid)
        elif action == "reopen":
            threads.reopen(db, tid)
        elif action == "floor":
            threads.set_floor(db, tid, body.get("floor") if body.get("floor") in threads.LEVELS else None)
        elif action == "delegate":
            scope = None if body.get("scope") == "all" else tid
            threads.delegate(db, spec=_text(body, "spec"), thread_id=scope,
                             kind=_text(body, "kind", required=False) or None,
                             until=_text(body, "until", required=False) or None)
        else:
            return 404, {"ok": False, "error": "no such action"}
        return 200, {"ok": True}
    except (KeyError, ValueError, TypeError) as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - report, never 500 silently
        return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


def _hhmm(v) -> str:
    v = str(v)
    if v == "sunset" or re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
        return v
    raise ValueError("time must be HH:MM or sunset")
