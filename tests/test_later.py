"""Scheduled jobs: due ones spawn exactly once, future ones wait, a failure is not retried."""
from datetime import datetime, timezone

from concierge import later


def entry(when, title):
    return {"when": when, "title": title, "brief": "b", "cwd": "/x", "chat_id": "1", "fired": None}


def test_due_entries_spawn_once_and_future_ones_wait(tmp_path):
    path = tmp_path / "later.json"
    later.save([entry("2026-11-12T09:00:00+00:00", "thu"), entry("2026-11-14T20:00:00+00:00", "sat")], path)
    spawned = []

    def spawner(**kw):
        spawned.append(kw["title"])
        return {"id": "Z1"}

    now = datetime(2026, 11, 12, 9, 5, tzinfo=timezone.utc)
    later.run_due(now=now, path=path, spawner=spawner)
    later.run_due(now=now, path=path, spawner=spawner)  # next ensure-up tick

    assert spawned == ["thu"]
    assert later.load(path)[0]["job"] == "Z1"


def test_a_spawn_that_raises_is_not_retried(tmp_path):
    path = tmp_path / "later.json"
    later.save([entry("2026-11-12T09:00:00+00:00", "thu")], path)
    calls = []

    def boom(**kw):
        calls.append(1)
        raise RuntimeError("died")

    now = datetime(2026, 11, 12, 9, 5, tzinfo=timezone.utc)
    later.run_due(now=now, path=path, spawner=boom)
    later.run_due(now=now, path=path, spawner=boom)

    assert calls == [1]
    assert "died" in later.load(path)[0]["error"]


def test_naive_time_is_uk_time():
    assert later.parse_when("2026-07-01T09:00").utcoffset().total_seconds() == 3600
