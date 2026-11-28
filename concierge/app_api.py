"""The threads app's HTTP API, served by the dashboard under /api/... and /app.

GETs are reads. Every POST goes through the dashboard's origin check first.
Anything that types into a session (say, answer, undo) runs on a worker thread:
tmuxctl.submit waits seconds for the box to clear, and the page polls for the result.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path

from concierge import chatdraft, config, emaildraft, inbox_waiting, registry, skills, spawn, threads, usage

APP_HTML = Path(__file__).with_name("app.html")
ROUTE = re.compile(r"^/api/threads/(\d+)(?:/(\w+))?$")
UPLOADS = Path.home() / ".local" / "share" / "concierge" / "uploads"
MAX_FILES = 10
MAX_UPLOAD_BODY = 40 * 1024**2  # the dashboard's cap on a POST that carries files (New thread, a reply), as base64
IMAGE_MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp", b"GIF8": ".gif"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
BLOB = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/blob/[^/]+/(.+)$")
LIMITS = {"text": 8000, "spec": 4000, "rules": 8000, "note": 2000, "to": 2000, "cc": 2000, "subject": 1000}


def manifest(prefix: str = "") -> str:
    """His one phone app. On the tailnet's main address (prefix /claude) its scope is the whole
    address, so every productivity page there (Tasks, Brief, Money, Inbox) is a pane of it, joined by
    appbar.js. Served bare on :18787 it is the older install, kept working until he moves."""
    shortcuts = [{"name": "New thread", "url": f"{prefix}/app#new"}]
    if prefix:  # long-press the icon: straight to a pane
        shortcuts += [{"name": n, "url": u} for n, u in
                      (("Tasks", "/claude/tasks"), ("Brief", "/brief/"),
                       ("Browser controls", "https://bos-desktop.fish-grouper.ts.net:16081/"))]
    return json.dumps({
        "id": f"{prefix}/app", "name": "Concierge", "short_name": "Concierge", "start_url": f"{prefix}/app", "scope": "/",
        "display": "standalone", "background_color": "#0e1116", "theme_color": "#0e1116",
        "icons": [{"src": f"{prefix}/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
        "shortcuts": shortcuts,
        # The phone's share sheet: share text, a link, photos or files to Concierge and New thread opens
        # with them, with a "Send to" for an open thread instead. Files need sw.js (it catches the POST),
        # which only the main address has, so the older :18787 install shares text only.
        "share_target": {"action": f"{prefix}/app/share", "method": "POST", "enctype": "multipart/form-data",
                         "params": {"title": "title", "text": "text", "url": "url",
                                    "files": [{"name": "files", "accept": ["image/*", "video/*", "application/pdf", "*/*"]}]}}
        if prefix else {"action": "/app", "method": "GET", "params": {"title": "title", "text": "text", "url": "url"}},
    })


def image(url: str, repos=None) -> tuple[int, bytes, str]:
    """An image a job linked, from the repo's checkout here: the repos are private, so the phone can't
    load the GitHub link itself. Only image files inside a configured repo, nothing else on disk."""
    m = BLOB.match(url.split("?")[0])
    repo = m and next((r for r in (config.REPOS if repos is None else repos)
                       if (r.github or "").lower() == f"https://github.com/{m.group(1)}".lower()), None)
    if not repo:
        return 404, b"not a linked image", "text/plain"
    root = repo.path.expanduser().resolve()
    f = (root / urllib.parse.unquote(m.group(2))).resolve()
    if not f.is_relative_to(root) or f.suffix.lower() not in IMAGE_TYPES or not f.is_file():
        return 404, b"no such image", "text/plain"
    return 200, f.read_bytes(), IMAGE_TYPES[f.suffix.lower()]


# Photos a job shows him (--image, --card) are copied here, not pushed to a repo: a day folder each,
# and folders older than MEDIA_DAYS go the next time a photo is stored.
MEDIA = Path.home() / ".local" / "share" / "concierge" / "media"
MEDIA_DAYS = 60
MEDIA_REF = re.compile(r"^media:(\d{4}-\d{2}-\d{2}/[0-9a-f]{16}\.\w+)$")


def store_media(path: Path, root: Path | None = None, today: datetime | None = None) -> str:
    """Copy a photo into the app's store and return its `media:` ref; prune day folders past MEDIA_DAYS."""
    import hashlib
    from datetime import timedelta

    root, today = root or MEDIA, today or datetime.now()
    path = Path(path).expanduser()
    ext = path.suffix.lower()
    if ext not in IMAGE_TYPES or not path.is_file():
        raise ValueError(f"not a photo: {path}")
    data = path.read_bytes()
    day = today.strftime("%Y-%m-%d")
    out = root / day / (hashlib.sha256(data).hexdigest()[:16] + ext)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    cutoff = (today - timedelta(days=MEDIA_DAYS)).strftime("%Y-%m-%d")
    for d in root.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and d.name < cutoff:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
    return f"media:{day}/{out.name}"


def media(ref: str, root: Path | None = None) -> tuple[int, bytes, str]:
    m = MEDIA_REF.match("media:" + ref)
    f = (root or MEDIA) / m.group(1) if m else None
    if not f or not f.is_file() or f.suffix.lower() not in IMAGE_TYPES:
        return 404, b"gone", "text/plain"
    return 200, f.read_bytes(), IMAGE_TYPES[f.suffix.lower()]


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
            return 200, {"threads": threads.mark_working(threads.home(db, q=q, closed=bool(query.get("closed")))),
                         "brief": brief(), "usage": usage.get(),
                         "mode": (m := threads.active_mode(db)) and m["name"]}
        if (m := ROUTE.match(path)) and m.group(2) in ("draft", "chat"):
            msg = db.execute("SELECT ref, body FROM messages WHERE id=? AND thread=? AND kind=?",
                             (int((query.get("msg") or ["0"])[0]), int(m.group(1)), m.group(2))).fetchone()
            if not msg:
                return 404, {"error": "no such card"}
            try:
                if m.group(2) == "chat":
                    return 200, chatdraft.load(msg["ref"]) | {"draft": msg["body"] or ""}
                return 200, emaildraft.load(msg["ref"])
            except RuntimeError as exc:
                return 502, {"error": str(exc) if m.group(2) == "chat" else f"Gmail: {exc}"}
        if m := ROUTE.match(path):
            try:
                d = threads.mark_working([threads.detail(db, int(m.group(1)))])[0]
                return 200, d | {"chat_url": (registry.load().get(d["job"] or "") or {}).get("rc_url")}
            except KeyError:
                return 404, {"error": "no such thread"}
        if path == "/api/skills":  # the composer's "/" autocomplete
            return 200, {"skills": skills.available()}
        if path.startswith("/api/alarms/"):
            row = db.execute("SELECT * FROM alarms WHERE id=?", (path.rsplit("/", 1)[1],)).fetchone()
            return (200, {k: row[k] for k in ("id", "level", "title", "body", "open")}) if row else (404, {"error": "no"})
        if path == "/api/inbox/waiting":
            try:
                return 200, {"waiting": inbox_waiting.waiting()}
            except Exception as exc:  # noqa: BLE001 - the Review page shows it, rather than an empty list
                return 200, {"waiting": [], "error": f"{type(exc).__name__}: {exc}"}
        if path == "/api/settings/appbar":
            return 200, json.loads(threads._kv(db, "appbar") or "{}")
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


def save_files(files, root: Path | None = None) -> list[Path]:
    """Attachments from the app (New thread, a reply), each {name, data: a data: URL}, as files a job can Read.
    Any kind of file. Saved as 01-<name>, 02-<name>… in a fresh folder, the name cut to safe characters
    so nothing he sends becomes a path."""
    if not files:
        return []
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError(f"files: a list of up to {MAX_FILES}")
    blobs = []
    for i, f in enumerate(files, 1):
        name, url = (f.get("name"), f.get("data")) if isinstance(f, dict) else ("", f)
        try:
            raw = base64.b64decode(str(url).split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"file {i} is not base64") from None
        safe = re.sub(r"[^\w.-]+", "-", str(name or ""))[-80:].strip(".-")
        if not safe:  # a pasted screenshot can come with no name
            safe = "file" + next((e for magic, e in IMAGE_MAGIC.items() if raw.startswith(magic)), "")
        blobs.append((f"{i:02d}-{safe}", raw))
    folder = (root or UPLOADS) / datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    for name, raw in blobs:
        (folder / name).write_bytes(raw)
    return [folder / name for name, _ in blobs]


def start_thread(db, text: str, *, files: list[Path] = (), spawner=None, lock=None) -> dict:
    """Quick capture: a job and its thread, back as soon as the window exists."""
    title = " ".join(text.split())[:60] or "New thread"
    attached = threads.attached(files) + "\n" if files else ""
    brief_text = (f"Started from Kelvin's Concierge app. His message:\n<<<\n{text}\n>>>{skills.hint(text)}\n\n{attached}"
                  "Talk to him only through your thread: `concierge post`, `concierge ask` for decisions, "
                  "`concierge summary` for what he reads first, `concierge facts` for the key facts, `concierge close` when it's done.")
    chat = registry.last_chat() or ""
    with lock or threading.Lock():
        job = (spawner or spawn.spawn_job)(title=title, brief=brief_text, cwd=str(config.SETTINGS.default_repo.path),
                                          chat_id=chat, poller=lambda _w: None)
    th = threads.find_or_create(db, job=job["id"], title=title, pane=f"{config.TMUX_SESSION}:{job['id']}")
    threads.post(db, th, text + threads.clip(files), author="me")
    db.execute("UPDATE messages SET state='picked' WHERE thread=? AND author='me'", (th["id"],))
    return {"id": th["id"], "job": job["id"]}


def post(path: str, body: dict, *, lock=None, finish=None) -> tuple[int, dict]:
    db = threads.connect()
    try:
        if path == "/api/threads":
            files = save_files(body.get("files") or body.get("images"))  # images: a page from before files
            started = start_thread(db, _text(body, "text", required=not files) or "(see the attached files)",
                                   files=files, lock=lock)
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
        if path == "/api/inbox/resume":
            return 200, {"ok": True, "window": inbox_waiting.resume(_text(body, "session_id"))}
        if path == "/api/settings/appbar":
            # The phone bar: pane ids in order (appbar.js owns the catalogue) and how many sit in
            # the bar; the rest open from More.
            order = body.get("order")
            main = body.get("main")
            if (not isinstance(order, list) or not 1 <= len(order) <= 30
                    or not all(isinstance(i, str) and 0 < len(i) <= 40 for i in order)
                    or not isinstance(main, int) or not 1 <= main <= 6):
                return 400, {"ok": False, "error": "order: a list of pane ids; main: 1 to 6"}
            threads._set_kv(db, "appbar", json.dumps({"order": list(dict.fromkeys(order)), "main": main}))
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
            about = int(body["about"]) if body.get("about") is not None else None
            files = save_files(body.get("files"))
            _bg(threads.say, tid, _text(body, "text", required=not files) or "(see the attached files)",
                about=about, files=files)
        elif action == "answer":
            msg, choice = int(body["msg"]), _text(body, "choice")
            threads._decision(db, msg)  # refuse now (400) rather than on the worker
            _bg(threads.answer, msg, choice, note=_text(body, "note", required=False),
                grace=threads.UNDO_SECONDS)
        elif action in ("sendemail", "sendchat"):
            msg, _ = threads._decision(db, int(body["msg"]))
            if msg["kind"] != ("draft" if action == "sendemail" else "chat") or msg["thread"] != tid:
                return 400, {"ok": False, "error": "not that kind of card in this thread"}
            try:
                if action == "sendchat":
                    text = _text(body, "text")
                    chatdraft.send(msg["ref"], text)
                    db.execute("UPDATE messages SET body=? WHERE id=?", (text, msg["id"]))  # what went
                    edited = text != (msg["body"] or "").strip()
                    answer = "Sent"
                    told = (f'Kelvin sent the message to {msg["text"].removeprefix("Message to ")}'
                            f'{", after editing it, as: " + text if edited else ", as you drafted it."}')
                else:
                    html = body.get("html")
                    if html is not None and (not isinstance(html, str) or len(html) > 200_000 or not html.strip()):
                        return 400, {"ok": False, "error": "the email text is empty or too long"}
                    sender, to = _text(body, "from"), _text(body, "to", required=False)
                    emaildraft.send(msg["ref"], sender=sender, to=to, cc=_text(body, "cc", required=False),
                                    subject=_text(body, "subject", required=False), html=html)
                    answer = f"Sent from {sender}"
                    told = (f'Kelvin sent the email "{msg["text"]}" (Gmail draft {msg["ref"]}) from {sender} to {to}'
                            f'{", after editing it." if html is not None else ", as you drafted it."}')
            except RuntimeError as exc:
                return 502, {"ok": False, "error": str(exc)}
            _bg(threads.card_sent, msg["id"], answer=answer, told=told)
        elif action == "undo":
            _bg(threads.undo, int(body["msg"]))
        elif action == "setaside":
            msg = int(body["msg"])
            threads._decision(db, msg)  # refuse now (400) rather than on the worker
            _bg(threads.set_aside, msg)
        elif action == "bringback":
            _bg(threads.bring_back, int(body["msg"]))
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
