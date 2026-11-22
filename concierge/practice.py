"""Practice: the phone way into his Notion interview question bank (the Practice pane, /claude/practice).

Notion is the only store and the only scheduler. This module reads the bank into a deck the page keeps
offline, and only ever ADDS: attempts (exactly as /drill writes them) and, when he asks, an answer.
It never edits or deletes anything in Notion.

Spec: tasks/2026-11-22-duolingo-style-question-review/spec.md

- An attempt is logged once. The page makes its id; `state/practice.db` maps id -> Notion page. A retry of
  a logged id returns the same page. A crash between the Notion write and the journal is caught by looking
  for a page on that question created since the first try, before writing again.
- Grading on request reuses drill.py's own packet/judge/render (the /drill skill), loaded as a module with
  its Notion calls pointed at ours. drill's save_best is not used: it edits the previous best answer.
- The reminder rides ensure-up (every 5 min, wst \\Bosire\\concierge\\Watchdog): one a day at his time, never
  21:00-08:00 or in the Sabbath, never once today's review is done.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from concierge import config
from concierge.threads import LONDON, sunset

DRILL_PY = Path.home() / "projects/personal/ai-toolkit/plugins/ai-toolkit/skills/drill/drill.py"
DB_FILE = config.REPO / "state" / "practice.db"
CACHE_FILE = config.REPO / "state" / "practice-cache.json"
QUESTIONS = "35d00c22619f81c68641f8e193f279df"
ATTEMPTS = "37822bcc9a704502bf2e92b9ba70f838"
ANSWERS = "8c0c5d7fabae4b2591ccd4c33d632d04"
TYPES = "0252c3e063984e829a6170ee7f90a096"
NOT_WRITTEN = {"Coding", "ML Coding"}  # graded by Outcome + Minutes, and LeetCode stays out of v1
RATINGS = ["Again", "Hard", "Good", "Easy"]
FREEZES, EARN_EVERY = 2, 7  # 2 freezes; one comes back for every 7 days reviewed
REMIND_LATE = timedelta(hours=2)  # PC off at his time: still remind up to 2 h late, never later
LOCK = threading.Lock()  # ponytail: one lock for every write and deck build; one user, a few writes a day


class NotionDown(Exception):
    """Notion (or the token fetch) failed: the page's outbox keeps the write and sends it again."""


# ---- Notion -------------------------------------------------------------------------------------

_token: str | None = None


def token() -> str:
    """NOTION_TOKEN, fetched once into this process's memory; never put in os.environ (jobs inherit that)."""
    global _token
    if not _token:
        ws = shutil.which("with-secrets") or str(Path.home() / ".cache/toolkit/bin/with-secrets")
        out = subprocess.run([ws, "printenv", "NOTION_TOKEN"], capture_output=True, text=True, timeout=180)
        _token = out.stdout.strip() or None
        if not _token:
            raise NotionDown(f"no NOTION_TOKEN from with-secrets (exit {out.returncode})")
    return _token


def api(method: str, path: str, body: dict | None = None) -> dict:
    for attempt in range(4):
        time.sleep(0.35)  # Notion's 3 requests a second
        req = urllib.request.Request(
            f"https://api.notion.com/v1/{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token()}", "Notion-Version": "2022-06-28",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 409, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise NotionDown(f"{method} {path}: {e.code} {e.read().decode()[:300]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise NotionDown(f"{method} {path}: {e}") from None


_drill = None


def drill():
    """drill.py as a module, its Notion calls routed through api() above."""
    global _drill
    if _drill is None:
        spec = importlib.util.spec_from_file_location("drill", DRILL_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.api = lambda method, path, body=None: api(method, path, body)
        _drill = mod
    return _drill


def query(db: str, flt: dict | None = None) -> list[dict]:
    return drill().query(db, flt)


# ---- the schedule, as Notion computes it --------------------------------------------------------

def day_number(d: date) -> int:
    """Notion's day number for a Date: floor((timestamp + 12h) / 1 day) = days since 1970-01-01."""
    return (d - date(1970, 1, 1)).days


def interval(rank, fail_day, first_day, ease_sum, successes, gap_setting, full_run) -> int:
    """The Questions 'Interval' formula, line for line (Notion's round() rounds halves up)."""
    r = rank or 0
    lg, last = r % 10, r // 10
    anchor = fail_day if (fail_day or 0) > 0 else (first_day or 0)
    gap = max(0, last - anchor)
    ease = max(1.3, 2.5 + (ease_sum or 0))
    s = max(1, successes or 0)
    base = 2 if lg == 2 else 7 if lg == 4 else 3
    cap = base * ease ** (s - 1)
    g = (gap_setting or 1) * (4 if full_run else 1)
    if r <= 0:
        return 0
    if lg == 1:
        return 1
    x = gap * 1.2 if lg == 2 else gap * ease * 1.3 if lg == 4 else gap * ease
    return math.floor(g * min(cap, max(base, x)) + 0.5)


def back_in(s: dict, rating: str, on: date) -> int:
    """Days until the question comes back if rated `rating` on `on`: what Notion will show once synced."""
    t, digit = day_number(on), RATINGS.index(rating) + 1
    first = min(s["first_day"] or 999999, t)
    return interval(max(s["rank"] or 0, t * 10 + digit), max(s["fail_day"] or 0, t if digit == 1 else 0), first,
                    (s["ease_sum"] or 0) + {1: -0.2, 2: -0.15, 3: 0, 4: 0.15}[digit],
                    (s["successes"] or 0) + (digit > 1), s["gap"], s["full_run"])


# ---- the deck -----------------------------------------------------------------------------------

def order(rows: list[dict]) -> list[dict]:
    """Review Queue first, in its own order (unanswered, then weakest = shortest Interval, then most
    overdue), then New (answered, never attempted) as the New tabs group it: topic or project, easy first."""
    queue = [r for r in rows if r["due"] or not r["answered"]]
    new = [r for r in rows if r not in queue and not r["attempts"]]
    queue.sort(key=lambda r: (r["answered"], r["interval"], r["next_review"] or "9999"))
    new.sort(key=lambda r: (r["topic"], r["difficulty"], r["practice_order"] is None, r["practice_order"] or 0,
                            r["created"]))
    return queue + new


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _body(cache: dict, page: dict, part: str | None = None) -> str:
    """A page's text (only the part under the '## <part>' heading, if given), cached on last_edited_time."""
    key = page["id"] + (f"#{part}" if part else "")
    hit = cache.get(key)
    if hit and hit["edited"] == page["last_edited_time"]:
        return hit["text"]
    d = drill()
    kids = [b for b in d.children(page["id"]) if b["type"] != "child_database"]
    if part:
        heads = [i for i, b in enumerate(kids) if b["type"].startswith("heading_")]
        at = next((i for i in heads if d.block_text(kids[i]).strip() == part), None)
        kids = [] if at is None else kids[at + 1:next((i for i in heads if i > at), len(kids))]
    text = d.to_md(kids)
    cache[key] = {"edited": page["last_edited_time"], "text": text}
    return text


def build_deck(today: date | None = None) -> dict:
    d = drill()
    v = d.value
    today = today or datetime.now(LONDON).date()
    types = {r["id"]: d.title(r) for r in query(TYPES)}
    gaps = {r["id"]: v(r["properties"]["Review gap"]) for r in query(TYPES)}
    written = {i for i, n in types.items() if n not in NOT_WRITTEN}
    rows = query(QUESTIONS, {"and": [{"property": "Skip", "checkbox": {"equals": False}},
                                     {"property": "Format", "select": {"does_not_equal": "Full run"}}]})
    rows = [r for r in rows if set(v(r["properties"]["Interview Type"])) & written
            and "Leetcode" not in [o["name"] for o in r["properties"]["Source"]["multi_select"]]]
    answers: dict[str, list[dict]] = {}
    for a in query(ANSWERS, {"property": "Old", "checkbox": {"equals": False}}):
        for q in v(a["properties"]["Question"]):
            answers.setdefault(q, []).append(a)
    names = _names
    cache = _load_cache()
    out = []
    for r in rows:
        p = r["properties"]
        tid = next(i for i in v(p["Interview Type"]) if i in written)
        group_ids = v(p["Projects"]) if types[tid] == "CV Defence" else v(p["Topics"])
        for i in group_ids:
            if i not in names:
                names[i] = d.title(api("GET", f"pages/{i}"))
        sched = {"rank": v(p["Last rank"]), "fail_day": v(p["Last fail day"]), "first_day": v(p["First day"]),
                 "ease_sum": v(p["Ease sum"]), "successes": v(p["Successes"]), "gap": gaps.get(tid) or 1,
                 "full_run": False}
        rank = sched["rank"] or 0
        ans = sorted(answers.get(r["id"], []), key=lambda a: a["created_time"])
        diff = v(p["Difficulty"]) or []
        out.append({
            "id": r["id"], "url": r["url"], "title": d.title(r), "type": types[tid],
            "topic": ", ".join(names[i] for i in group_ids) or types[tid],
            "difficulty": {"easy": 0, "medium": 1, "hard": 2}.get(diff[0] if diff else None, 3),
            "practice_order": v(p["Practice Order"]), "created": r["created_time"],
            "due": bool(v(p["Due"])), "answered": bool(v(p["Answered"])), "attempts": v(p["Attempt count"]) or 0,
            "interval": v(p["Interval"]) or 0, "next_review": (v(p["Next Review"]) or "")[:10] or None,
            "last": RATINGS[rank % 10 - 1] if rank else None,
            "context": _body(cache, r, "Question"),
            "answers": [{"title": d.title(a), "text": _body(cache, a)} for a in ans],
            "back_in": {k: back_in(sched, k, today) for k in RATINGS},
            "gradable": types[tid] in d.RUBRICS,
        })
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE_FILE)
    counts = review_counts()
    drop = ("difficulty", "practice_order", "created")
    return {"built": datetime.now(LONDON).isoformat(timespec="seconds"), "day": today.isoformat(),
            "types": sorted({q["type"] for q in out}),
            "questions": [{k: x for k, x in q.items() if k not in drop} for q in order(out)],
            "today": counts.get(today, 0),
            "streak": {k: streak({**counts, today: k}, today) for k in (0, 1, 2)}}


_names: dict[str, str] = {}  # topic and project titles; they rarely change
_deck: dict | None = None


def deck(max_age: float = 60) -> dict:
    global _deck
    with LOCK:
        if _deck is None or time.time() - _deck["_at"] > max_age:
            _deck = {**build_deck(), "_at": time.time()}
        return {k: v for k, v in _deck.items() if k != "_at"}


# ---- streak -------------------------------------------------------------------------------------

def review_counts() -> dict[date, int]:
    since = (datetime.now(LONDON).date() - timedelta(days=400)).isoformat()
    counts: dict[date, int] = {}
    for a in query(ATTEMPTS, {"property": "Date", "date": {"on_or_after": since}}):
        day = date.fromisoformat(drill().value(a["properties"]["Date"])[:10])
        counts[day] = counts.get(day, 0) + 1
    return counts


def streak(counts: dict[date, int], today: date) -> dict:
    """Days in a row with a review. Saturday (the Sabbath) never breaks it and needs nothing. A missed day is
    repaired by 2 reviews on the next streak day; failing that a freeze covers it; failing that it starts again,
    with both freezes back.
    A freeze comes back for every EARN_EVERY days reviewed, up to FREEZES."""
    days, freezes, run, repair = 0, FREEZES, 0, False
    nxt = lambda d: d + timedelta(days=2 if d.weekday() == 4 else 1)  # Friday's next streak day is Sunday
    day = min((d for d, n in counts.items() if n), default=today)
    while day <= today:
        n = counts.get(day, 0)
        if n:
            days, run = days + 1, run + 1
            if run >= EARN_EVERY and freezes < FREEZES:
                freezes, run = freezes + 1, 0
        elif day.weekday() == 5 or day == today:
            pass
        else:
            after = nxt(day)
            if after > today or (after == today and counts.get(today, 0) < 2):
                repair = True  # still repairable today (or tomorrow, after a Saturday)
            elif counts.get(after, 0) >= 2:
                days += 1
            elif freezes:
                freezes -= 1
            else:
                days, run, freezes = 0, 0, FREEZES  # a fresh start comes with both freezes
        day += timedelta(days=1)
    return {"days": days, "freezes": freezes, "done": bool(counts.get(today)) or today.weekday() == 5,
            "repair": repair}


# ---- writes (exactly once) ----------------------------------------------------------------------

def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=10, isolation_level=None, check_same_thread=False)
    db.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, kind TEXT, question TEXT, tried TEXT, url TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    return db


def once(db, cid: str, kind: str, question: str, create) -> str:
    """Run create() at most once per client id; return the Notion page url. create() -> page dict."""
    with LOCK:
        row = db.execute("SELECT tried, url FROM sent WHERE id=?", (cid,)).fetchone()
        if row and row[1]:
            return row[1]
        if row:  # tried before and never recorded: it may have landed in Notion
            parent = ATTEMPTS if kind == "attempt" else ANSWERS
            since = (datetime.fromisoformat(row[0]) - timedelta(minutes=2)).isoformat()
            found = query(parent, {"and": [{"property": "Question", "relation": {"contains": question}},
                                           {"timestamp": "created_time", "created_time": {"on_or_after": since}}]})
            page = found[0] if found else create()
        else:
            db.execute("INSERT INTO sent (id, kind, question, tried) VALUES (?,?,?,?)",
                       (cid, kind, question, datetime.now(LONDON).isoformat()))
            page = create()
        db.execute("UPDATE sent SET url=? WHERE id=?", (page["url"], cid))
        return page["url"]


def log_attempt(db, cid: str, question: str, rating: str, day: date, answer: str = "", judged=None) -> str:
    """The attempt /drill writes: '#N - Rating', Date, Rating, Question; his answer under '## Answer'."""
    d = drill()

    def create():
        q = api("GET", f"pages/{question}")
        count = d.value(q["properties"]["Attempt count"]) or 0
        body = d.md_to_blocks("## Answer\n" + answer) if answer.strip() else []
        body += d.render(judged) if judged else []
        page = api("POST", "pages", {"parent": {"database_id": ATTEMPTS}, "children": body[:100], "properties": {
            "Attempt": {"title": d.rich(f"#{count + 1} - {rating}")}, "Rating": {"select": {"name": rating}},
            "Question": {"relation": [{"id": question}]}, "Date": {"date": {"start": day.isoformat()}}}})
        d.append(page["id"], body[100:])
        return page

    return once(db, cid, "attempt", question, create)


def save_answer(db, cid: str, question: str, text: str) -> str:
    d = drill()

    def create():
        blocks = d.md_to_blocks(text)
        page = api("POST", "pages", {"parent": {"database_id": ANSWERS}, "children": blocks[:100], "properties": {
            "Answer": {"title": d.rich("My answer")}, "Question": {"relation": [{"id": question}]}}})
        d.append(page["id"], blocks[100:])
        return page

    return once(db, cid, "answer", question, create)


def grade(question: str, answer: str) -> dict:
    """drill's grader on one typed answer: {rating, feedback, judged}. Slow (one claude -p call)."""
    d = drill()
    q = api("GET", f"pages/{question}")
    rnd = d.round_of(q)
    if rnd not in d.RUBRICS:
        raise ValueError(f"/drill has no rubric for {rnd} yet")
    try:
        j = d.judge([{"round": rnd, "mode": "full", "packet": d.packet(q)["text"], "answer": answer}])[0]
    except SystemExit as e:  # drill exits on a failed grader call
        raise NotionDown(str(e)) from None
    if isinstance(j, str):
        raise NotionDown(j)
    return {"rating": j["rating"], "feedback": d.feedback(j), "judged": j}


# ---- requests from the page ---------------------------------------------------------------------

def _day(body: dict) -> date:
    at = body.get("at")
    return datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(LONDON).date() if at else \
        datetime.now(LONDON).date()


def post(route: str, body: dict, *, db=None) -> tuple[int, dict]:
    db = db or connect()
    cid, question = str(body.get("id") or ""), str(body.get("question") or "").replace("-", "")
    try:
        if route == "/api/practice/settings":
            remind = body.get("remind") or ""
            if remind and not (len(remind) == 5 and "08:00" <= remind <= "20:59"):
                return 400, {"ok": False, "error": "reminder must be between 08:00 and 20:59"}
            db.execute("INSERT OR REPLACE INTO kv VALUES ('remind', ?)", (remind,))
            return 200, {"ok": True, "remind": remind}
        if not (8 <= len(cid) <= 64 and len(question) == 32):
            return 400, {"ok": False, "error": "need an id and a question"}
        if route == "/api/practice/attempt":
            if body.get("rating") not in RATINGS:
                return 400, {"ok": False, "error": "rating must be Again, Hard, Good or Easy"}
            url = log_attempt(db, cid, question, body["rating"], _day(body), str(body.get("answer") or ""))
            return 200, {"ok": True, "url": url}
        if route == "/api/practice/answer":
            if not str(body.get("text") or "").strip():
                return 400, {"ok": False, "error": "nothing to save"}
            return 200, {"ok": True, "url": save_answer(db, cid, question, str(body["text"]))}
        if route == "/api/practice/grade":
            answer = str(body.get("answer") or "").strip()
            if not answer:
                return 400, {"ok": False, "error": "type an answer first"}
            done = db.execute("SELECT url FROM sent WHERE id=?", (cid,)).fetchone()
            if done and done[0]:
                return 200, {"ok": True, "url": done[0], "repeat": True}
            g = grade(question, answer)
            url = log_attempt(db, cid, question, g["rating"], _day(body), answer, g["judged"])
            return 200, {"ok": True, "url": url, "rating": g["rating"], "feedback": g["feedback"]}
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    except NotionDown as e:
        return 503, {"ok": False, "error": str(e)}
    return 404, {"ok": False, "error": "no such route"}


def get(route: str, *, db=None) -> tuple[int, dict]:
    try:
        if route == "/api/practice/deck":
            return 200, deck()
        if route == "/api/practice/settings":
            row = (db or connect()).execute("SELECT value FROM kv WHERE key='remind'").fetchone()
            return 200, {"remind": row[0] if row else ""}
    except NotionDown as e:
        return 503, {"ok": False, "error": str(e)}
    return 404, {"ok": False, "error": "no such route"}


# ---- the daily reminder -------------------------------------------------------------------------

def sabbath(t: datetime) -> bool:
    t = t.astimezone(LONDON)
    return (t.weekday() == 4 and t >= sunset(t.date())) or (t.weekday() == 5 and t < sunset(t.date()))


def should_remind(now: datetime, remind: str, sent_on: str, done: bool) -> bool:
    now = now.astimezone(LONDON)
    if not remind or done or sent_on == now.date().isoformat():
        return False
    if not 8 <= now.hour < 21 or sabbath(now):
        return False
    at = now.replace(hour=int(remind[:2]), minute=int(remind[3:]), second=0, microsecond=0)
    return at <= now < at + REMIND_LATE


def remind(now: datetime | None = None, *, db=None, sender=None, reviewed=None) -> str:
    """One tick of ensure-up. Asks Notion whether today is done only when everything else says send."""
    db = db or connect()
    now = now or datetime.now(LONDON)
    kv = dict(db.execute("SELECT key, value FROM kv").fetchall())
    if not should_remind(now, kv.get("remind", ""), kv.get("reminded", ""), False):
        return "practice: no reminder due"
    today = now.astimezone(LONDON).date()
    if (reviewed or (lambda: streak(review_counts(), today)["done"]))():
        return "practice: already reviewed today"
    db.execute("INSERT OR REPLACE INTO kv VALUES ('reminded', ?)", (today.isoformat(),))  # one a day, even if send fails
    if sender is None:
        from concierge import registry, telegram

        chat = registry.last_chat()
        if not chat:
            return "practice: no chat to remind"
        sender = lambda text: telegram.send(chat, text)
    sender(f"Today's 5 questions are ready when you are, about 5 minutes: {config.SETTINGS.app.base_url}/practice")
    return "practice: reminder sent"
