from datetime import date, datetime

import pytest

from concierge import practice
from concierge.threads import LONDON

Q = "3f900c22619f8121a328e2945e26a336"


@pytest.fixture
def db(tmp_path):
    return practice.connect(tmp_path / "p.db")


def test_an_attempt_is_logged_once_however_often_it_arrives(db, monkeypatch):
    made = []
    create = lambda: made.append(1) or {"url": f"https://notion/{len(made)}"}
    monkeypatch.setattr(practice, "query", lambda *a: pytest.fail("no lookup needed"))
    first = practice.once(db, "abc12345", "attempt", Q, create)
    again = practice.once(db, "abc12345", "attempt", Q, create)  # the outbox resending after a lost reply
    assert made == [1] and first == again


def test_a_write_that_died_before_its_journal_entry_is_found_not_repeated(db, monkeypatch):
    def crash():
        raise practice.NotionDown("reply lost")

    with pytest.raises(practice.NotionDown):
        practice.once(db, "abc12345", "attempt", Q, crash)  # it may still have landed in Notion
    monkeypatch.setattr(practice, "query", lambda db_id, flt: [{"url": "https://notion/landed"}])
    url = practice.once(db, "abc12345", "attempt", Q, lambda: pytest.fail("would duplicate"))
    assert url == "https://notion/landed"
    monkeypatch.setattr(practice, "query", lambda db_id, flt: [])
    assert practice.once(db, "zzz12345", "attempt", Q, lambda: {"url": "u"}) == "u"


def test_a_retry_with_nothing_in_notion_writes_it(db, monkeypatch):
    with pytest.raises(practice.NotionDown):
        practice.once(db, "abc12345", "attempt", Q, lambda: (_ for _ in ()).throw(practice.NotionDown("x")))
    monkeypatch.setattr(practice, "query", lambda db_id, flt: [])
    assert practice.once(db, "abc12345", "attempt", Q, lambda: {"url": "made"}) == "made"


def row(id, due=False, answered=True, attempts=1, interval=3, nr="2026-11-20", topic="A", difficulty=3):
    return {"id": id, "due": due, "answered": answered, "attempts": attempts, "interval": interval,
            "next_review": nr, "topic": topic, "difficulty": difficulty, "practice_order": None, "created": id}


def test_review_queue_order_then_new():
    rows = [row("new-b", attempts=0, topic="B"), row("not-due"), row("due-weak-late", due=True, interval=1, nr="2026-11-21"),
            row("due-strong", due=True, interval=9, nr="2026-11-01"), row("unanswered", answered=False, attempts=2, interval=5, nr="2026-11-25"),
            row("due-weak-early", due=True, interval=1, nr="2026-11-02"), row("new-a-easy", attempts=0, topic="A", difficulty=0),
            row("new-a-hard", attempts=0, topic="A", difficulty=2)]
    assert [r["id"] for r in practice.order(rows)] == [
        "unanswered", "due-weak-early", "due-weak-late", "due-strong", "new-a-easy", "new-a-hard", "new-b"]


def counts(*days, n=1):
    return {date.fromisoformat(d): n for d in days}


# 2026-11-16 is a Monday; 11-21 Saturday, 11-22 Sunday.
def test_saturday_never_breaks_the_streak():
    s = practice.streak(counts("2026-11-19", "2026-11-20", "2026-11-22"), date(2026, 11, 22))
    assert s["days"] == 3 and s["freezes"] == 2 and s["done"]


def test_on_saturday_the_streak_is_already_done():
    assert practice.streak(counts("2026-11-20"), date(2026, 11, 21))["done"]


def test_a_missed_day_is_repaired_by_two_the_next_day():
    c = {**counts("2026-11-16", "2026-11-18"), date(2026, 11, 18): 2}
    s = practice.streak(c, date(2026, 11, 18))
    assert s["days"] == 3 and s["freezes"] == 2 and not s["repair"]


def test_yesterday_missed_stays_repairable_today_without_spending_a_freeze():
    s = practice.streak({**counts("2026-11-16"), date(2026, 11, 18): 1}, date(2026, 11, 18))
    assert s["repair"] and s["freezes"] == 2 and s["days"] == 2


def test_freezes_cover_misses_then_it_starts_again_with_both_back():
    s = practice.streak(counts("2026-11-09", "2026-11-11", "2026-11-13", "2026-11-17"), date(2026, 11, 17))
    # 10th: freeze 1, 12th: freeze 2, 14th Sat, 15th+16th missed: the 15th breaks it
    assert s["days"] == 1 and s["freezes"] == 2


def test_a_freeze_comes_back_after_a_week_of_reviews():
    c = counts("2026-11-02", *[f"2026-11-{d:02}" for d in range(4, 12)])
    assert practice.streak(c, date(2026, 11, 11))["freezes"] == 2  # spent on the 3rd, earned back on the 10th


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=LONDON)


@pytest.mark.parametrize("now,remind,sent,done,want", [
    ("2026-11-18 09:00", "09:00", "", False, True),
    ("2026-11-18 08:59", "09:00", "", False, False),           # not yet
    ("2026-11-18 11:30", "09:00", "", False, False),           # over 2 h late: skip the day
    ("2026-11-18 09:05", "09:00", "2026-11-18", False, False),  # one a day
    ("2026-11-18 09:05", "09:00", "", True, False),            # already practised
    ("2026-11-18 09:05", "", "", False, False),                # reminder off
    ("2026-11-18 21:00", "20:30", "", False, False),           # quiet hours
    ("2026-11-20 16:30", "16:15", "", False, False),           # Friday after sunset (~15:55)
    ("2026-11-20 15:00", "15:00", "", False, True),            # Friday before sunset
    ("2026-11-21 15:00", "15:00", "", False, False),           # Saturday before sunset
])
def test_reminder_window(now, remind, sent, done, want):
    assert practice.should_remind(at(now), remind, sent, done) is want


def test_remind_sends_once_and_never_when_done(db):
    db.execute("INSERT INTO kv VALUES ('remind', '09:00')")
    sent = []
    out = practice.remind(at("2026-11-18 09:05"), db=db, sender=sent.append, reviewed=lambda: True)
    assert sent == [] and "already" in out
    practice.remind(at("2026-11-18 09:05"), db=db, sender=sent.append, reviewed=lambda: False)
    practice.remind(at("2026-11-18 09:10"), db=db, sender=sent.append, reviewed=lambda: False)
    assert len(sent) == 1
