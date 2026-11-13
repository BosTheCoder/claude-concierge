"""The tasks page's writes: never clobber an edit made in TickTick, and move dates the way TickTick would."""

import pytest

from concierge import ticktick


def fake_tickTick(task):
    sent = []

    def call(name, args):
        sent.append((name, args))
        return dict(task) if name == "get_task_by_id" else {}
    return sent, call


def test_a_stale_page_never_overwrites_tickTick(monkeypatch):
    sent, call = fake_tickTick({"id": "a", "projectId": "p", "etag": "new", "status": 0, "title": "x"})
    monkeypatch.setattr(ticktick, "call", call)
    with pytest.raises(ticktick.Conflict):
        ticktick.update("a", "old", {"content": "mine"})
    assert [n for n, _ in sent] == ["get_task_by_id"]


def test_moving_a_summer_all_day_task_keeps_it_all_day_on_the_new_day(monkeypatch):
    task = {"id": "a", "projectId": "p", "etag": "e", "status": 0, "title": "x", "isAllDay": True,
            "timeZone": "Europe/London", "dueDate": "2026-06-11T23:00:00.000+0000", "startDate": "2026-06-11T23:00:00.000+0000"}
    sent, call = fake_tickTick(task)
    monkeypatch.setattr(ticktick, "call", call)
    ticktick.update("a", "e", {"day": "2026-11-20", "priority": 5, "status": 2})
    body = next(a["task"] for n, a in sent if n == "update_task")
    assert body["dueDate"] == body["startDate"] == "2026-11-20T00:00:00+00:00" and body["priority"] == 5
    assert ("complete_task", {"project_id": "p", "task_id": "a"}) in sent  # repeating tasks advance
