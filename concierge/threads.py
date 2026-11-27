"""Threads: one specific thing per thread, the conversation about it, and its alerts.

Everything the app shows lives in one SQLite file. Jobs write through the CLI
(`post`, `ask`, `facts`, `close`, `decided`); Kelvin writes through the app's API.
Spec: https://claude.ai/artifact/5rUz9w4esiwr1D6eddcmLT

Two rules the rest of this file exists to keep:
- A decision is a row, answered only by `answer()` (the app, his tap) or by
  `decided()` under a delegation he set. Nothing typed into a session can pick
  an option, which is how a WhatsApp alert accepted a £450 quote on 30 Oct.
- His message is delivered with `tmuxctl.submit`, which reports whether it
  actually left the input box, and the row says so.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from concierge import config, sessions, tmuxctl

DB_FILE = config.REPO / "state" / "threads.db"
LONDON = ZoneInfo("Europe/London")
LEVELS = ("fyi", "needs", "urgent", "critical")
QUIET_DAYS = 3
CARDS = ("draft", "chat")  # an email / a chat message he edits and sends himself
UNDO_SECONDS = 10  # his answer waits this long before it goes to the job, so Undo can take it back quietly
# The facts card has the same rows on every thread, in this order. A job that wants
# another row is told these; anything else belongs in the summary.
FACT_KEYS = ("Next", "Waiting on", "Due", "Money", "Who", "Decided", "Link")
NTFY = "https://ntfy.sh/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, job TEXT, pane TEXT,
  status TEXT NOT NULL DEFAULT 'open', floor TEXT, facts TEXT NOT NULL DEFAULT '{}',
  created TEXT NOT NULL, updated TEXT NOT NULL, closed TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, thread INTEGER NOT NULL REFERENCES threads(id),
  author TEXT NOT NULL, text TEXT NOT NULL, urgency TEXT NOT NULL DEFAULT 'fyi',
  kind TEXT NOT NULL DEFAULT 'msg', options TEXT, recommend TEXT,
  answer TEXT, answered_by TEXT, answered_at TEXT, rule TEXT, undone INTEGER NOT NULL DEFAULT 0,
  state TEXT, error TEXT, held INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delegations (
  id INTEGER PRIMARY KEY, thread INTEGER, kind TEXT, spec TEXT NOT NULL,
  until TEXT, created TEXT NOT NULL, ended TEXT);
CREATE TABLE IF NOT EXISTS modes (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, allow TEXT NOT NULL,
  start_day INTEGER NOT NULL, start_time TEXT NOT NULL,
  end_day INTEGER NOT NULL, end_time TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS alarms (
  id TEXT PRIMARY KEY, level TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  open TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread, id);
"""

# Sabbath ships as the first mode: Friday sunset to Saturday sunset, only Critical.
DEFAULT_MODES = [("Sabbath", ["critical"], 4, "sunset", 5, "sunset")]


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    for table, col in (("threads", "summary"), ("messages", "context"), ("messages", "why"), ("messages", "revised"),
                       ("messages", "ref"), ("messages", "body")):
        if col not in {c["name"] for c in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    if not db.execute("SELECT 1 FROM modes LIMIT 1").fetchone() and not _kv(db, "modes_seeded"):
        for name, allow, sd, st, ed, et in DEFAULT_MODES:
            db.execute("INSERT INTO modes(name, allow, start_day, start_time, end_day, end_time) VALUES (?,?,?,?,?,?)",
                       (name, json.dumps(allow), sd, st, ed, et))
        _set_kv(db, "modes_seeded", "1")
    return db


def _kv(db, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_kv(db, key: str, value: str) -> None:
    db.execute("INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


# --- sunset, for modes like the Sabbath ----------------------------------------------
HOME = (51.507, 0.03)  # Royal Docks; a few minutes either way across London


def sunset(day: date, lat: float = HOME[0], lon: float = HOME[1]) -> datetime:
    """Local sunset (the standard sunrise equation, good to a minute or two)."""
    n = (date(day.year, day.month, day.day) - date(2000, 1, 1)).days + 0.0008
    j_star = n - lon / 360
    m = (357.5291 + 0.98560028 * j_star) % 360
    rad = math.radians
    c = 1.9148 * math.sin(rad(m)) + 0.02 * math.sin(rad(2 * m)) + 0.0003 * math.sin(rad(3 * m))
    lam = (m + c + 180 + 102.9372) % 360
    j_transit = 2451545.0 + j_star + 0.0053 * math.sin(rad(m)) - 0.0069 * math.sin(rad(2 * lam))
    decl = math.asin(math.sin(rad(lam)) * math.sin(rad(23.4397)))
    cos_w = (math.sin(rad(-0.833)) - math.sin(rad(lat)) * math.sin(decl)) / (math.cos(rad(lat)) * math.cos(decl))
    j_set = j_transit + math.degrees(math.acos(max(-1.0, min(1.0, cos_w)))) / 360
    return datetime.fromtimestamp((j_set - 2440587.5) * 86400, timezone.utc).astimezone(LONDON)


def _at(day: date, hhmm: str) -> datetime:
    if hhmm == "sunset":
        return sunset(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=LONDON)


def active_mode(db, at: datetime | None = None) -> sqlite3.Row | None:
    """The mode on right now, if any. A mode runs from start_day/time to end_day/time
    (Mon=0), wrapping round the week."""
    t = (at or now()).astimezone(LONDON)
    for mode in db.execute("SELECT * FROM modes WHERE enabled=1 ORDER BY id"):
        # Try the window that started this week and the one that started last week.
        for back in (0, 7):
            start_day = t.date() - timedelta(days=(t.weekday() - mode["start_day"]) % 7 + back)
            end_day = start_day + timedelta(days=(mode["end_day"] - mode["start_day"]) % 7)
            start, end = _at(start_day, mode["start_time"]), _at(end_day, mode["end_time"])
            if end <= start:
                end = _at(end_day + timedelta(days=7), mode["end_time"])
            if start <= t < end:
                return mode
    return None


# --- alerts, through glance ---------------------------------------------------------
def alarm_topic(account: str) -> str:
    """Glance's alarm topic: same derivation as its sync topic, its own prefix."""
    return "glance-" + hashlib.sha256(("glance-alarm:" + account.lower()).encode()).hexdigest()[:20]


def _post_ntfy(topic: str, body: str) -> None:
    req = urllib.request.Request(NTFY + topic, data=body.encode(), method="POST")
    urllib.request.urlopen(req, timeout=10).close()


def raise_alarm(db, level: str, title: str, body: str, open_url: str, *, sender=None) -> str | None:
    """Store the alarm, then tell glance where to fetch it. Only the URL leaves the
    tailnet; glance refuses anything not on *.ts.net, and an id it can't fetch never fires."""
    app = config.SETTINGS.app
    if not app.base_url or not app.alarm_account:
        return None
    aid = hashlib.sha256(f"{title}|{body}|{now().timestamp()}".encode()).hexdigest()[:16]
    db.execute("INSERT INTO alarms(id, level, title, body, open, created) VALUES (?,?,?,?,?,?)",
               (aid, level, title, body[:400], open_url, _iso(now())))
    try:
        (sender or _post_ntfy)(alarm_topic(app.alarm_account), f"{app.base_url}/api/alarms/{aid}")
    except Exception as exc:  # noqa: BLE001 - the thread still shows it; say why it didn't ring
        db.execute("UPDATE alarms SET body = body || ? WHERE id=?", (f" (alert not sent: {exc})", aid))
    if app.mirror_to_telegram and sender is None:
        _mirror(level, title, body, open_url)
    return aid


def _mirror(level: str, title: str, body: str, open_url: str) -> None:
    """While the app is new, every alert also goes to Telegram. Never raises."""
    from concierge import registry, telegram

    try:
        chat = registry.last_chat()
        if chat:
            mark = {"urgent": "⚠️ ", "critical": "🚨 "}.get(level, "")
            telegram.send(chat, f"{mark}{title}\n{body[:600]}\n{open_url}")
    except Exception:  # noqa: BLE001
        pass


def thread_url(thread_id: int) -> str:
    return f"{config.SETTINGS.app.base_url}/app#t={thread_id}"


def _alert(db, thread: sqlite3.Row, msg_id: int, urgency: str, text: str, *, sender=None) -> None:
    level = urgency
    if thread["floor"] and LEVELS.index(thread["floor"]) < LEVELS.index(level):
        level = thread["floor"]
    if level == "fyi":
        return
    mode = active_mode(db)
    if mode and level not in json.loads(mode["allow"]):
        db.execute("UPDATE messages SET held=1 WHERE id=?", (msg_id,))
        return
    raise_alarm(db, level, thread["title"], text, thread_url(thread["id"]), sender=sender)


# --- threads and messages -------------------------------------------------------------
def find_or_create(db, *, job: str | None, title: str | None, pane: str | None = None,
                   thread_id: int | None = None) -> sqlite3.Row:
    """The thread a job means. By id if given; else the job's open thread with that
    title (or its only open thread); else a new one."""
    if thread_id is not None:
        row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row:
            raise KeyError(f"no thread {thread_id}")
        return row
    q = "SELECT * FROM threads WHERE job IS ? AND status='open'"
    rows = db.execute(q + (" AND title=?" if title else "") + " ORDER BY id DESC",
                      (job, title) if title else (job,)).fetchall()
    if rows:
        return rows[0]
    if not title:
        raise KeyError("no open thread for this job; give it a title with --thread")
    t = _iso(now())
    cur = db.execute("INSERT INTO threads(title, job, pane, created, updated) VALUES (?,?,?,?,?)",
                     (title, job, pane, t, t))
    return db.execute("SELECT * FROM threads WHERE id=?", (cur.lastrowid,)).fetchone()


def post(db, thread: sqlite3.Row, text: str, *, author: str = "job", urgency: str = "fyi",
         options: list[str] | None = None, recommend: str | None = None, kind: str = "msg",
         context: str | None = None, why: str | None = None, ref: str | None = None, body: str | None = None,
         sender=None) -> int:
    if urgency not in LEVELS:
        raise ValueError(f"urgency must be one of {', '.join(LEVELS)}")
    if options and recommend and recommend not in options:
        raise ValueError("--recommend must be one of the options")
    t = _iso(now())
    cur = db.execute(
        "INSERT INTO messages(thread, author, text, urgency, kind, options, recommend, context, why, ref, body, created) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (thread["id"], author, text, urgency, kind, json.dumps(options) if options else None, recommend,
         context, why, ref, body, t))
    db.execute("UPDATE threads SET updated=?, status=CASE WHEN status='closed' AND ?='job' THEN 'open' ELSE status END "
               "WHERE id=?", (t, author, thread["id"]))
    if author != "me":
        # Anything the job says after his message means it was read and acted on.
        db.execute("UPDATE messages SET state='acted' WHERE thread=? AND author='me' AND state='picked'", (thread["id"],))
        # Re-read: the caller's row can predate a turn-down or a rename.
        fresh = db.execute("SELECT * FROM threads WHERE id=?", (thread["id"],)).fetchone()
        _alert(db, fresh, cur.lastrowid, urgency, text, sender=sender)
    return cur.lastrowid


def set_facts(db, thread_id: int, facts: dict) -> dict:
    bad = [k for k in facts if k not in FACT_KEYS]
    if bad:
        raise ValueError(f"not a fact row: {', '.join(bad)}. Rows are: {', '.join(FACT_KEYS)}. "
                         "Anything else goes in the summary (`concierge summary`).")
    row = db.execute("SELECT facts FROM threads WHERE id=?", (thread_id,)).fetchone()
    merged = {**json.loads(row["facts"]), **facts}
    merged = {k: v for k, v in merged.items() if v not in (None, "")}
    db.execute("UPDATE threads SET facts=?, updated=? WHERE id=?", (json.dumps(merged), _iso(now()), thread_id))
    return merged


def set_summary(db, thread_id: int, text: str) -> None:
    """What he reads first: what this is, where it stands, what happens next."""
    db.execute("UPDATE threads SET summary=?, updated=? WHERE id=?", (text.strip() or None, _iso(now()), thread_id))


def close(db, thread_id: int) -> None:
    db.execute("UPDATE threads SET status='closed', closed=? WHERE id=?", (_iso(now()), thread_id))


def reopen(db, thread_id: int) -> None:
    db.execute("UPDATE threads SET status='open', closed=NULL, updated=? WHERE id=?", (_iso(now()), thread_id))


def set_floor(db, thread_id: int, floor: str | None) -> None:
    db.execute("UPDATE threads SET floor=? WHERE id=?", (floor, thread_id))


# --- talking to the job ---------------------------------------------------------------
_PANE_LOCKS: dict[str, threading.Lock] = {}


def _deliver(pane: str | None, text: str, *, submitter=None) -> str | None:
    """None once it left the input box, else why not."""
    if not pane:
        return "the Concierge can't type into this chat (it was started from the Claude app): reply in its Claude chat, linked at the top"
    lock = _PANE_LOCKS.setdefault(pane, threading.Lock())
    with lock:  # two taps at once would type into each other
        return (submitter or tmuxctl.submit)(pane, text)


def _to_job(thread: sqlite3.Row, body: str) -> str:
    return (f"[thread {thread['id']}: {thread['title']}] {body}\n"
            f"(Answer in the app: {config.REPO / 'bin' / 'concierge'} post --thread {thread['id']} \"...\")")


def say(db, thread_id: int, text: str, *, author: str = "me", about: int | None = None, submitter=None) -> int:
    """His message: stored as sent, then picked up or failed, never silently lost.
    `about`: the decision he is asking about, so the job knows which one."""
    thread = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    ref = ""
    if about is not None:
        d = db.execute("SELECT text FROM messages WHERE id=? AND thread=? AND options IS NOT NULL",
                       (about, thread_id)).fetchone()
        if not d:
            raise KeyError(f"no decision {about} in this thread")
        ref = f' About decision {about} ("{d["text"][:200]}"):'
    shown = f"Re: “{d['text'][:60]}…”\n{text}" if about is not None else text
    mid = post(db, thread, shown, author=author)
    db.execute("UPDATE messages SET state='sent' WHERE id=?", (mid,))
    problem = _deliver(thread["pane"], _to_job(thread, f"Kelvin says:{ref} {text}"), submitter=submitter)
    db.execute("UPDATE messages SET state=?, error=? WHERE id=?",
               ("failed" if problem else "picked", problem, mid))
    return mid


def _decision(db, msg_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    msg = db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    if not msg or not msg["options"]:
        raise KeyError(f"message {msg_id} is not a decision")
    if msg["answer"] is not None:
        raise ValueError(f"already answered: {msg['answer']}")
    return msg, db.execute("SELECT * FROM threads WHERE id=?", (msg["thread"],)).fetchone()


def answer(db, msg_id: int, choice: str, *, note: str = "", submitter=None, grace: float = 0) -> str | None:
    """His tap on a decision. It waits `grace` seconds before going to the job, so Undo in that
    window takes it back without the job ever seeing it. Returns why it didn't reach the job, or None."""
    msg, thread = _decision(db, msg_id)
    if msg["kind"] in CARDS:
        raise ValueError("an email or message is sent from its card")
    if choice not in json.loads(msg["options"]):
        raise ValueError("not one of the options")
    db.execute("UPDATE messages SET answer=?, answered_by='me', answered_at=?, state='held', error=NULL WHERE id=?",
               (choice, _iso(now()), msg_id))
    if msg["kind"] == "close?":
        db.execute("UPDATE messages SET state=NULL WHERE id=?", (msg_id,))
        if choice == "Close":
            close(db, thread["id"])
        return None
    if grace:
        time.sleep(grace)
    return _send_answer(db, msg_id, note=note, submitter=submitter)


def _send_answer(db, msg_id: int, *, note: str = "", submitter=None) -> str | None:
    # Claim it out of the undo window first: once it leaves 'held', Undo has to tell the job instead.
    if not db.execute("UPDATE messages SET state='sending' WHERE id=? AND state='held'", (msg_id,)).rowcount:
        return "taken back before it was sent"
    msg = db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    thread = db.execute("SELECT * FROM threads WHERE id=?", (msg["thread"],)).fetchone()
    body = f'Decision on "{msg["text"]}": Kelvin chose "{msg["answer"]}".' + (f" {note}" if note else "")
    problem = _deliver(thread["pane"], _to_job(thread, body), submitter=submitter)
    db.execute("UPDATE messages SET state=?, error=? WHERE id=?", ("failed" if problem else "picked", problem, msg_id))
    return problem


def card_sent(db, msg_id: int, *, answer: str, told: str, submitter=None) -> str | None:
    """He sent a job's email or chat draft from its card. Recorded, then the job is told."""
    _, thread = _decision(db, msg_id)
    db.execute("UPDATE messages SET answer=?, answered_by='me', answered_at=?, state='picked' WHERE id=?",
               (answer, _iso(now()), msg_id))
    problem = _deliver(thread["pane"], _to_job(thread, told + " It is sent: log it, don't send it again."),
                       submitter=submitter)
    if problem:
        db.execute("UPDATE messages SET state='failed', error=? WHERE id=?", (problem, msg_id))
    return problem


def withdraw(db, msg_id: int, why: str) -> None:
    """The job takes back a decision it no longer needs: overtaken by a newer one, or by events.
    It leaves his list; the history keeps it, with why."""
    _decision(db, msg_id)
    db.execute("UPDATE messages SET answer=?, answered_by='withdrawn', answered_at=? WHERE id=?",
               (why.strip() or "no longer needed", _iso(now()), msg_id))


def revise(db, msg_id: int, *, text: str | None = None, options: list[str] | None = None,
           recommend: str | None = None, context: str | None = None, why: str | None = None) -> None:
    """The job updates an open decision in place (new facts, a changed option) instead of
    asking a second one. A tap on an option that has since gone is refused by answer()."""
    msg, _ = _decision(db, msg_id)
    opts = options or json.loads(msg["options"])
    rec = recommend if recommend is not None else msg["recommend"]
    if rec and rec not in opts:
        raise ValueError("--recommend must be one of the options")
    db.execute("UPDATE messages SET text=?, options=?, recommend=?, context=?, why=?, revised=? WHERE id=?",
               (text or msg["text"], json.dumps(opts), rec or None, context if context is not None else msg["context"],
                why if why is not None else msg["why"], _iso(now()), msg_id))
    db.execute("UPDATE threads SET updated=? WHERE id=?", (_iso(now()), msg["thread"]))


def open_decisions(db, thread_id: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM messages WHERE thread=? AND options IS NOT NULL AND answer IS NULL "
                      "AND kind!='close?' ORDER BY id", (thread_id,)).fetchall()


def delegation_for(db, thread_id: int, kind: str | None = None, at: datetime | None = None) -> sqlite3.Row | None:
    t = _iso(at or now())
    for d in db.execute("SELECT * FROM delegations WHERE ended IS NULL AND (thread IS NULL OR thread=?) "
                        "ORDER BY thread IS NULL, id DESC", (thread_id,)):
        if d["until"] and d["until"] <= t:
            continue
        if d["kind"] and d["kind"] != kind:
            continue
        return d
    return None


def decided(db, thread: sqlite3.Row, question: str, choice: str, *, rule: str, kind: str | None = None,
            sender=None) -> int:
    """Claude deciding for him. Refused unless a live delegation covers this thread."""
    d = delegation_for(db, thread["id"], kind)
    if not d:
        raise PermissionError("no delegation covers this thread: ask Kelvin with `ask` instead")
    if not rule.strip():
        raise ValueError("say which rule of the spec you relied on (--rule)")
    mid = post(db, thread, question, author="claude", urgency="fyi", options=[choice], sender=sender)
    db.execute("UPDATE messages SET answer=?, answered_by='claude', answered_at=?, rule=? WHERE id=?",
               (choice, _iso(now()), rule, mid))
    return mid


def undo(db, msg_id: int, *, submitter=None) -> str | None:
    """Take back a decision: his own answer (the question opens again) or one Claude took for him."""
    msg = db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    if msg and msg["kind"] in CARDS:
        raise ValueError("a sent email or message can't be taken back")
    if msg and msg["answered_by"] == "me":
        reopen_q = "UPDATE messages SET answer=NULL, answered_by=NULL, answered_at=NULL, state=NULL, error=NULL WHERE id=?"
        if msg["kind"] == "close?":  # the app's own question: nothing went to a job
            db.execute(reopen_q, (msg_id,))
            if msg["answer"] == "Close":
                reopen(db, msg["thread"])
            return None
        if db.execute(reopen_q + " AND state IN ('held', 'failed')", (msg_id,)).rowcount:
            return None  # the job never had it
        db.execute(reopen_q, (msg_id,))
        thread = db.execute("SELECT * FROM threads WHERE id=?", (msg["thread"],)).fetchone()
        body = (f'Kelvin took back his answer "{msg["answer"]}" on "{msg["text"]}". Undo what you did on it '
                "if you can and tell him what couldn't be undone. The decision is open again in the app: "
                "wait for his new answer.")
        return _deliver(thread["pane"], _to_job(thread, body), submitter=submitter)
    if not msg or msg["answered_by"] != "claude" or msg["undone"]:
        raise ValueError("only an answered decision can be undone")
    db.execute("UPDATE messages SET undone=1 WHERE id=?", (msg_id,))
    thread = db.execute("SELECT * FROM threads WHERE id=?", (msg["thread"],)).fetchone()
    body = (f'Kelvin reversed your decision "{msg["answer"]}" on "{msg["text"]}". '
            "Undo what you can, then ask him with `ask`.")
    return _deliver(thread["pane"], _to_job(thread, body), submitter=submitter)


def delegate(db, *, spec: str, thread_id: int | None = None, kind: str | None = None,
             until: str | None = None) -> int:
    return db.execute("INSERT INTO delegations(thread, kind, spec, until, created) VALUES (?,?,?,?,?)",
                      (thread_id, kind or None, spec, until or None, _iso(now()))).lastrowid


def end_delegation(db, delegation_id: int) -> None:
    db.execute("UPDATE delegations SET ended=? WHERE id=?", (_iso(now()), delegation_id))


def rules_text(db, thread_id: int | None = None) -> str:
    out = ["Global decision rules:", _kv(db, "rules") or "(none set)"]
    live = [d for d in db.execute("SELECT * FROM delegations WHERE ended IS NULL ORDER BY id")
            if (not d["until"] or d["until"] > _iso(now()))
            and (thread_id is None or d["thread"] in (None, thread_id))]
    out.append("\nDelegations you may act under:" if live else "\nNo delegation: every decision goes to Kelvin.")
    for d in live:
        scope = f"thread {d['thread']}" if d["thread"] else "all threads"
        out.append(f"- [{scope}{', kind ' + d['kind'] if d['kind'] else ''}"
                   f"{', until ' + d['until'] if d['until'] else ''}] {d['spec']}")
    return "\n".join(out)


# --- what the app reads ---------------------------------------------------------------
def _needs(db, thread_id: int) -> tuple[str, str]:
    """(what the thread needs from him, the urgency of what's waiting)."""
    pending = db.execute("SELECT urgency FROM messages WHERE thread=? AND options IS NOT NULL AND answer IS NULL "
                         "AND kind!='close?' ORDER BY id DESC LIMIT 1", (thread_id,)).fetchone()
    if pending:
        return "decision", pending["urgency"]
    last = db.execute("SELECT author, urgency, answer FROM messages WHERE thread=? ORDER BY id DESC LIMIT 1",
                      (thread_id,)).fetchone()
    # A tapped answer is his reply too: it lands on the job's message, not as a message of his.
    if last and last["author"] != "me" and last["answer"] is None and last["urgency"] != "fyi":
        return "reply", last["urgency"]
    return "nothing", "fyi"


def summary(db, row: sqlite3.Row) -> dict:
    needs, urgency = _needs(db, row["id"])
    if row["floor"] and LEVELS.index(row["floor"]) < LEVELS.index(urgency):
        urgency = row["floor"]
    last = db.execute("SELECT text, author FROM messages WHERE thread=? ORDER BY id DESC LIMIT 1",
                      (row["id"],)).fetchone()
    return {"id": row["id"], "title": row["title"], "status": row["status"], "needs": needs,
            "urgency": urgency, "floor": row["floor"], "updated": row["updated"], "job": row["job"],
            "pane": row["pane"], "last": (last["text"][:160] if last else ""), "last_author": last["author"] if last else None}


ORDER = {"decision": 0, "reply": 1, "nothing": 2}


def home(db, *, q: str = "", closed: bool = False) -> list[dict]:
    if q:
        like = f"%{q}%"
        rows = db.execute("SELECT DISTINCT t.* FROM threads t LEFT JOIN messages m ON m.thread=t.id "
                          "WHERE t.title LIKE ? OR m.text LIKE ? OR t.facts LIKE ? ORDER BY t.updated DESC LIMIT 100",
                          (like, like, like)).fetchall()
    else:
        rows = db.execute("SELECT * FROM threads WHERE status=? ORDER BY updated DESC LIMIT 200",
                          ("closed" if closed else "open",)).fetchall()
    out = [summary(db, r) for r in rows]
    if not q and not closed:
        # Newest first, then (stable) what it needs and how urgent.
        out.sort(key=lambda s: s["updated"], reverse=True)
        out.sort(key=lambda s: (ORDER[s["needs"]], -LEVELS.index(s["urgency"])))
    return out


def mark_working(rows: list[dict], *, live_panes=None, read=None) -> list[dict]:
    """Set `working` on each thread whose Claude is mid-turn, so the list can show it.

    The pane says so itself ("esc to interrupt", or a live turn timer; see
    sessions.pane_read). One `tmux list-panes` first, so a pane that is gone is
    never captured (tmux would prefix-match R1 to R10), then one capture per
    distinct pane: threads that share a session share its answer.
    """
    if live_panes is None:
        live_panes = {x for p in sessions.panes() for x in (p.session, f"{p.session}:{p.window_name}")}
    read = read or sessions.pane_state
    busy: dict[str, bool] = {}
    for t in rows:
        pane = t.get("pane")
        if pane in live_panes and pane not in busy:
            busy[pane] = read(pane) == "busy"
        t["working"] = busy.get(pane, False)
    return rows


def detail(db, thread_id: int) -> dict:
    row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not row:
        raise KeyError(thread_id)
    msgs = [dict(m) | {"options": json.loads(m["options"]) if m["options"] else None}
            for m in db.execute("SELECT * FROM messages WHERE thread=? ORDER BY id", (thread_id,))]
    d = delegation_for(db, thread_id)
    facts = json.loads(row["facts"])
    return {**summary(db, row), "summary": row["summary"],
            # Standard rows first, in order; rows set before FACT_KEYS existed come after.
            "facts": {k: facts[k] for k in FACT_KEYS if k in facts},
            "other_facts": {k: v for k, v in facts.items() if k not in FACT_KEYS}, "messages": msgs,
            "delegation": dict(d) if d else None}


def stats(db, weeks: int = 8) -> list[dict]:
    since = _iso(now() - timedelta(weeks=weeks))
    out: dict[str, dict] = {}
    for m in db.execute("SELECT urgency, created FROM messages WHERE author!='me' AND created>=?", (since,)):
        y, w, _ = datetime.fromisoformat(m["created"]).astimezone(LONDON).isocalendar()
        out.setdefault(f"{y}-W{w:02d}", dict.fromkeys(LEVELS, 0))[m["urgency"]] += 1
    return [{"week": k, **v} for k, v in sorted(out.items())]


# --- the 5-minute tick ----------------------------------------------------------------
def tick(db, *, at: datetime | None = None, sender=None) -> str:
    """Release what a mode held, and ask about threads that went quiet."""
    t = at or now()
    said = []
    mode = active_mode(db, t)
    held = db.execute("SELECT m.*, th.title FROM messages m JOIN threads th ON th.id=m.thread WHERE m.held=1 "
                      "ORDER BY m.id").fetchall()
    allow = set(json.loads(mode["allow"])) if mode else set(LEVELS)
    release = [m for m in held if m["urgency"] in allow]
    if release:
        db.execute(f"UPDATE messages SET held=0 WHERE id IN ({','.join('?' * len(release))})",
                   [m["id"] for m in release])
        top = max(release, key=lambda m: LEVELS.index(m["urgency"]))
        raise_alarm(db, "needs" if len(release) > 1 else top["urgency"],
                    f"{len(release)} waiting" if len(release) > 1 else top["title"],
                    "; ".join(sorted({m["title"] for m in release}))[:400] if len(release) > 1 else top["text"],
                    thread_url(top["thread"]) if len(release) == 1 else f"{config.SETTINGS.app.base_url}/app",
                    sender=sender)
        said.append(f"released {len(release)} held")
    # An answer whose worker died in its undo window (dashboard restart) still goes.
    for m in db.execute("SELECT id FROM messages WHERE state='held' AND answered_at < ?",
                        (_iso(t - timedelta(seconds=UNDO_SECONDS * 3)),)).fetchall():
        _send_answer(db, m["id"])
        said.append(f"sent held answer {m['id']}")
    cutoff = _iso(t - timedelta(days=QUIET_DAYS))
    for th in db.execute("SELECT * FROM threads WHERE status='open' AND updated < ?", (cutoff,)).fetchall():
        asked = db.execute("SELECT 1 FROM messages WHERE thread=? AND kind='close?' AND answer IS NULL",
                           (th["id"],)).fetchone()
        if not asked:
            post(db, th, f"Quiet for {QUIET_DAYS} days. Close it?", author="app", urgency="fyi",
                 options=["Close", "Keep open"], recommend="Close", kind="close?", sender=sender)
            said.append(f"asked to close {th['id']}")
    return ", ".join(said) or "threads: nothing to do"
