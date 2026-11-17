from datetime import date, datetime, timedelta, timezone

import pytest

from concierge import cli, config, threads
from concierge.settings import App

SAT_NOON = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)   # inside Sabbath
SAT_NIGHT = datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)  # after Saturday sunset
WED_NOON = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", config.SETTINGS.__class__(
        **{**config.SETTINGS.__dict__, "app": App(base_url="https://box.example.ts.net", alarm_account="me@x.com")}))
    return threads.connect(tmp_path / "t.db")


@pytest.fixture
def clock(monkeypatch):
    t = {"now": WED_NOON}
    monkeypatch.setattr(threads, "now", lambda: t["now"])
    return t


def sent_alarms():
    out = []
    return out, lambda topic, url: out.append((topic, url))


def test_a_decision_is_answered_once_and_only_by_a_tap(db, clock):
    th = threads.find_or_create(db, job="A1", title="Plumber", pane="concierge:A1")
    mid = threads.post(db, th, "Which plumber?", options=["Drury's", "PKUK"], recommend="Drury's",
                       sender=lambda *_: None)
    typed = []
    # Anything he types (or anything typed into the session) is just a message: the decision stays open.
    threads.say(db, th["id"], "Enter", submitter=lambda pane, text: typed.append(text))
    assert threads.detail(db, th["id"])["needs"] == "decision"
    with pytest.raises(ValueError):
        threads.answer(db, mid, "Someone else", submitter=lambda *_: None)
    assert threads.answer(db, mid, "PKUK", submitter=lambda pane, text: typed.append(text)) is None
    assert 'chose "PKUK"' in typed[-1]
    with pytest.raises(ValueError):
        threads.answer(db, mid, "Drury's", submitter=lambda *_: None)


def test_claude_decides_only_inside_a_live_delegation(db, clock):
    th = threads.find_or_create(db, job="A1", title="Plumber", pane="p")
    with pytest.raises(PermissionError):
        threads.decided(db, th, "Visit day?", "Tuesday", rule="any weekday")
    threads.delegate(db, spec="Pick visit days", thread_id=th["id"],
                     until=(WED_NOON + timedelta(hours=1)).isoformat())
    mid = threads.decided(db, th, "Visit day?", "Tuesday", rule="any weekday")
    msg = next(m for m in threads.detail(db, th["id"])["messages"] if m["id"] == mid)
    assert (msg["answered_by"], msg["rule"]) == ("claude", "any weekday")
    clock["now"] = WED_NOON + timedelta(hours=2)
    with pytest.raises(PermissionError):
        threads.decided(db, th, "Visit time?", "9am", rule="any weekday")


def test_sabbath_holds_alerts_and_releases_one_after_sunset(db, clock):
    alarms, sender = sent_alarms()
    th = threads.find_or_create(db, job="A1", title="Glazier", pane="p")
    clock["now"] = SAT_NOON
    threads.post(db, th, "Quote is in", urgency="needs", sender=sender)
    threads.post(db, th, "Leak!", urgency="critical", sender=sender)
    assert len(alarms) == 1                       # only Critical got through
    threads.tick(db, at=SAT_NOON, sender=sender)
    assert len(alarms) == 1                       # still Sabbath: still held
    clock["now"] = SAT_NIGHT
    threads.tick(db, at=SAT_NIGHT, sender=sender)
    assert len(alarms) == 2
    assert alarms[-1][0] == threads.alarm_topic("me@x.com")
    assert alarms[-1][1].startswith("https://box.example.ts.net/api/alarms/")


def test_turned_down_thread_never_rings(db, clock):
    alarms, sender = sent_alarms()
    th = threads.find_or_create(db, job="A1", title="Noise", pane="p")
    threads.set_floor(db, th["id"], "fyi")
    threads.post(db, th, "again", urgency="urgent", sender=sender)
    assert alarms == []


def test_his_message_shows_whether_it_landed(db, clock):
    th = threads.find_or_create(db, job="A1", title="X", pane="p")
    ok = threads.say(db, th["id"], "go ahead", submitter=lambda *_: None)
    bad = threads.say(db, th["id"], "and this", submitter=lambda *_: "its input box already holds unsent text")
    states = {m["id"]: m["state"] for m in threads.detail(db, th["id"])["messages"]}
    assert (states[ok], states[bad]) == ("picked", "failed")
    threads.post(db, th, "done that", sender=lambda *_: None)
    assert {m["id"]: m["state"] for m in threads.detail(db, th["id"])["messages"]}[ok] == "acted"


def test_notify_lands_in_the_jobs_thread_and_done_closes_it(db, clock):
    job = {"title": "Chase invoice", "tmux_window": "K7"}
    cli.to_thread("K7", job, "Sent the chaser", None, db=db)
    cli.to_thread("K7", job, "Paid", "done", db=db)
    rows = threads.home(db, closed=True)
    assert [(r["title"], r["status"]) for r in rows] == [("Chase invoice", "closed")]
    assert [m["text"] for m in threads.detail(db, rows[0]["id"])["messages"]] == ["Sent the chaser", "Paid"]


def test_sunset_is_about_right_for_london():
    s = threads.sunset(date(2026, 11, 13))
    assert (s.hour, 5 <= s.minute <= 25) == (16, True)   # ~16:13 in mid-November
    assert threads.sunset(date(2026, 6, 21)).hour == 21   # ~21:21 midsummer


def test_the_app_api_refuses_a_bad_answer_and_serves_alarms(db, clock, monkeypatch, tmp_path):
    from concierge import app_api

    th = threads.find_or_create(db, job="A1", title="Plumber", pane="p")
    alarms, sender = sent_alarms()
    mid = threads.post(db, th, "Which?", urgency="urgent", options=["A", "B"], sender=sender)
    monkeypatch.setattr(threads, "DB_FILE", tmp_path / "t.db")  # the API opens its own connection
    ran = []
    monkeypatch.setattr(app_api, "_bg", lambda fn, *a, **k: ran.append((fn.__name__, a)))
    assert app_api.post(f"/api/threads/{th['id']}/answer", {"msg": mid, "choice": "A"})[0] == 200
    assert ran == [("answer", (mid, "A"))]
    threads.answer(db, mid, "A", submitter=lambda *_: None)
    assert app_api.post(f"/api/threads/{th['id']}/answer", {"msg": mid, "choice": "B"})[0] == 400
    status, alarm = app_api.get("/api/alarms/" + alarms[0][1].rsplit("/", 1)[1], {})
    assert (status, alarm["level"], alarm["open"]) == (200, "urgent", f"https://box.example.ts.net/app#t={th['id']}")
    assert app_api.get("/api/alarms/nope", {})[0] == 404


def test_the_bottom_bar_layout_round_trips_and_refuses_junk():
    from concierge import app_api
    assert app_api.get("/api/settings/appbar", {}) == (200, {})
    assert app_api.post("/api/settings/appbar", {"order": ["review", "tasks", "review"], "main": 2})[0] == 200
    assert app_api.get("/api/settings/appbar", {}) == (200, {"order": ["review", "tasks"], "main": 2})
    for bad in ({"order": "tasks", "main": 2}, {"order": ["tasks"], "main": 9}, {"order": [], "main": 2}):
        assert app_api.post("/api/settings/appbar", bad)[0] == 400
    assert app_api.get("/api/settings/appbar", {})[1]["main"] == 2


def test_thread_opens_on_summary_with_the_same_fact_rows_everywhere(db, clock):
    th = threads.find_or_create(db, job="A1", title="Damp", pane="concierge:A1")
    db.execute("UPDATE threads SET facts=? WHERE id=?", ('{"D05": "fan", "Money": "£400"}', th["id"]))
    with pytest.raises(ValueError, match="Waiting on"):
        threads.set_facts(db, th["id"], {"Shortlist": "x"})
    threads.set_facts(db, th["id"], {"Due": "31 Dec", "Next": "pick a firm"})
    threads.set_summary(db, th["id"], "Three prices in; waiting on two more.")
    d = threads.detail(db, th["id"])
    assert d["summary"] == "Three prices in; waiting on two more."
    assert list(d["facts"]) == ["Next", "Due", "Money"]  # FACT_KEYS order, whatever order they were set in
    assert d["other_facts"] == {"D05": "fan"}


def test_an_overtaken_decision_leaves_his_list_and_a_question_names_the_decision(db, clock):
    th = threads.find_or_create(db, job="A1", title="Damp", pane="concierge:A1")
    first = threads.post(db, th, "Ask Benson for Sunday?", options=["Yes", "Wait"], sender=lambda *_: None)
    second = threads.post(db, th, "Ask Benson for Monday instead?", options=["Yes", "Wait"], sender=lambda *_: None)
    threads.withdraw(db, first, "replaced by decision 2")
    assert [r["id"] for r in threads.open_decisions(db, th["id"])] == [second]
    with pytest.raises(ValueError):
        threads.answer(db, first, "Yes")  # withdrawn is final: a late tap can't revive it
    typed = []
    threads.say(db, th["id"], "Explain", about=second, submitter=lambda pane, text: typed.append(text))
    assert f"decision {second}" in typed[0] and "Monday" in typed[0]


def test_a_decision_is_revised_in_place_and_a_tap_on_a_dropped_option_is_refused(db, clock):
    th = threads.find_or_create(db, job="A1", title="Damp", pane="concierge:A1")
    mid = threads.post(db, th, "Ask Benson for Sunday?", options=["Sunday", "Wait"], recommend="Sunday",
                       context="RN can look Sun 1-2pm", why="soonest free visit", sender=lambda *_: None)
    threads.revise(db, mid, text="Ask Benson for Monday?", options=["Monday", "Wait"], recommend="Monday")
    d = threads.open_decisions(db, th["id"])[0]
    assert (d["text"], d["recommend"], d["context"], d["why"]) == (
        "Ask Benson for Monday?", "Monday", "RN can look Sun 1-2pm", "soonest free visit")
    with pytest.raises(ValueError):
        threads.answer(db, mid, "Sunday", submitter=lambda *_: None)


def test_he_can_take_back_an_answer_before_and_after_it_reaches_the_job(db, clock, monkeypatch):
    th = threads.find_or_create(db, job="A1", title="Plumber", pane="p")
    mid = threads.post(db, th, "Which plumber?", options=["Drury's", "PKUK"], sender=lambda *_: None)
    typed = []
    sub = lambda pane, text: typed.append(text)  # noqa: E731
    # Undo inside the window: the job never hears of it, and the question is open again.
    monkeypatch.setattr(threads.time, "sleep", lambda s: threads.undo(db, mid, submitter=sub))
    assert threads.answer(db, mid, "PKUK", submitter=sub, grace=10) is not None
    assert typed == [] and threads.detail(db, th["id"])["needs"] == "decision"
    # Undo after it went: reopened, and the job is told to reverse what it did.
    monkeypatch.setattr(threads.time, "sleep", lambda s: None)
    assert threads.answer(db, mid, "Drury's", submitter=sub, grace=10) is None
    threads.undo(db, mid, submitter=sub)
    assert len(typed) == 2 and "took back" in typed[1] and '"Drury\'s"' in typed[1]
    assert threads.answer(db, mid, "PKUK", submitter=sub) is None  # he can answer again


def test_an_answer_held_when_the_dashboard_died_still_goes(db, clock, monkeypatch):
    th = threads.find_or_create(db, job="A1", title="Plumber", pane="p")
    mid = threads.post(db, th, "Which plumber?", options=["Drury's", "PKUK"], sender=lambda *_: None)
    db.execute("UPDATE messages SET answer='PKUK', answered_by='me', answered_at=?, state='held' WHERE id=?",
               (threads._iso(WED_NOON), mid))
    typed = []
    monkeypatch.setattr(threads.tmuxctl, "submit", lambda pane, text: typed.append(text))
    threads.tick(db, at=WED_NOON + timedelta(minutes=5), sender=lambda *_: None)
    assert len(typed) == 1 and 'chose "PKUK"' in typed[0]


def test_undoing_close_reopens_the_thread_and_the_question(db, clock):
    th = threads.find_or_create(db, job="A1", title="Plumber", pane="p")
    clock["now"] = WED_NOON + timedelta(days=4)
    threads.tick(db, sender=lambda *_: None)
    ask = threads.open_decisions(db, th["id"]) or db.execute(
        "SELECT * FROM messages WHERE kind='close?' AND answer IS NULL").fetchall()
    mid = ask[0]["id"]
    threads.answer(db, mid, "Close")
    assert threads.detail(db, th["id"])["status"] == "closed"
    threads.undo(db, mid, submitter=lambda *_: pytest.fail("nothing goes to the job"))
    assert threads.detail(db, th["id"])["status"] == "open"
    assert db.execute("SELECT answer FROM messages WHERE id=?", (mid,)).fetchone()[0] is None
