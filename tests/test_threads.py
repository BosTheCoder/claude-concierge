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
