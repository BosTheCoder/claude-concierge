from concierge import threads, usage


def bars(pct, resets):
    return {"bars": [{"name": "Session", "pct": pct, "resets": resets, "elapsed": 50},
                     {"name": "Week", "pct": 40, "resets": "2026-12-01T08:00:00+00:00", "elapsed": 50}]}


def test_alert_once_per_window_then_again_next_window():
    db = threads.connect()
    count = lambda: db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    sender = lambda *a, **k: None
    assert usage.alert(db, bars(79, "2026-11-27T23:00:00+00:00"), sender=sender) == "usage: under the line"
    usage.alert(db, bars(81, "2026-11-27T22:59:59.7+00:00"), sender=sender)
    assert count() == 1
    usage.alert(db, bars(95, "2026-11-27T23:00:00.4+00:00"), sender=sender)  # same window, jittered
    assert count() == 1
    usage.alert(db, bars(85, "2026-11-28T04:00:00+00:00"), sender=sender)
    assert count() == 2
