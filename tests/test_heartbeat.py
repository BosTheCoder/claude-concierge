"""The scheduled-run heartbeat.

job-tracker was dead for 13 days and every observability feature it had lived
inside the process that had stopped. These tests are about the one thing that
matters: noticing ABSENCE, from outside, without a new scheduled task that can
rot the same way.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from concierge import heartbeat

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

WATCH = heartbeat.Watch(
    name="job-tracker sourcing",
    url="http://localhost:8000/api/runs?kind=source",
    kind="source",
    max_age_hours=30,
    expected="daily at 04:00",
)


def run(hours_ago: float, *, ok: bool = True, kind: str = "source", summary: str = "swept"):
    ts = (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
    return {"kind": kind, "ok": ok, "started_at": ts, "finished_at": ts, "summary": summary}


def feed(*runs):
    return lambda url: list(runs)


def boom(exc):
    def _fetch(url):
        raise exc
    return _fetch


def text_of(problem):
    assert problem is not None, "expected a problem, got none"
    return problem.text


# --- check: the three ways it goes wrong -------------------------------------


def test_a_recent_successful_run_is_silent():
    assert heartbeat.check(WATCH, NOW, fetch=feed(run(6))) is None


def test_absence_past_the_window_alerts():
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=feed(run(31))))
    assert msg is not None
    assert "no successful run in 31h" in msg
    assert "daily at 04:00" in msg


def test_a_run_just_inside_the_window_is_silent():
    assert heartbeat.check(WATCH, NOW, fetch=feed(run(29.5))) is None


def test_the_actual_outage_would_have_fired():
    # 2026-07-23 to 2026-08-05: the tasks fired on time and died instantly, so
    # no run row was ever written. Absence is the whole signal.
    assert heartbeat.check(WATCH, NOW, fetch=feed(run(13 * 24))) is not None


def test_no_runs_at_all_alerts():
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=feed()))
    assert "no successful run on record" in msg


def test_an_unreachable_api_alerts():
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=boom(OSError("connection refused"))))
    assert "can't reach the API" in msg
    assert "connection refused" in msg


def test_a_failed_newest_run_alerts_even_when_recent():
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=feed(run(1, ok=False, summary="gmail auth died"))))
    assert "last run FAILED" in msg
    assert "gmail auth died" in msg


def test_a_failed_newest_run_beats_an_older_success():
    # The newest word is the true state; an in-window success behind a failure
    # must not buy silence.
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=feed(run(1, ok=False), run(25))))
    assert "last run FAILED" in msg


def test_a_long_failure_summary_is_truncated():
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=feed(run(1, ok=False, summary="x" * 900))))
    assert len(msg) < 400
    assert "…" in msg


def test_other_kinds_are_ignored():
    # A flush today says nothing about whether sourcing ran.
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=feed(run(1, kind="flush"), run(40))))
    assert "no successful run in 40h" in msg


def test_a_run_with_an_unparseable_timestamp_alerts_rather_than_passing():
    bad = {"kind": "source", "ok": True, "finished_at": "not-a-date", "started_at": None}
    msg = text_of(heartbeat.check(WATCH, NOW, fetch=lambda url: [bad]))
    assert "no usable timestamp" in msg


# --- rate limiting: it rides a 5-minute watchdog ----------------------------


def test_first_call_is_due():
    assert heartbeat.is_due({}, NOW)


def test_a_poll_ten_minutes_ago_is_not_due():
    state = {"last_poll": (NOW - timedelta(minutes=10)).isoformat()}
    assert not heartbeat.is_due(state, NOW)


def test_a_poll_two_hours_ago_is_due():
    state = {"last_poll": (NOW - timedelta(hours=2)).isoformat()}
    assert heartbeat.is_due(state, NOW)


def test_a_corrupt_last_poll_does_not_wedge_the_watchdog_forever():
    assert heartbeat.is_due({"last_poll": "garbage"}, NOW)


def test_not_due_does_no_work_at_all(tmp_path):
    state_file = tmp_path / "hb.json"
    state_file.write_text(json.dumps({"last_poll": (NOW - timedelta(minutes=5)).isoformat()}))

    def explode(url):
        raise AssertionError("must not poll when not due")

    out = heartbeat.run_if_due(
        now=NOW, state_path=state_file, watches=(WATCH,), fetch=explode,
        notifier=lambda m: None,
    )
    assert out == "not-due"


# --- run_if_due -------------------------------------------------------------


def test_a_healthy_service_notifies_nobody(tmp_path):
    sent = []
    out = heartbeat.run_if_due(
        now=NOW, state_path=tmp_path / "hb.json", watches=(WATCH,),
        fetch=feed(run(2)), notifier=sent.append,
    )
    assert out == "ok"
    assert sent == []


def test_a_stopped_service_notifies_once(tmp_path):
    sent = []
    out = heartbeat.run_if_due(
        now=NOW, state_path=tmp_path / "hb.json", watches=(WATCH,),
        fetch=feed(run(50)), notifier=sent.append,
    )
    assert out == "alerted:1"
    assert len(sent) == 1
    assert "no successful run in 50h" in sent[0]


def test_the_cooldown_stops_it_nagging_every_five_minutes(tmp_path):
    # ensure-up fires 288 times a day. Without this, so would the alert.
    state_file = tmp_path / "hb.json"
    sent = []
    for minutes in (0, 61, 122, 183):
        heartbeat.run_if_due(
            now=NOW + timedelta(minutes=minutes), state_path=state_file,
            watches=(WATCH,), fetch=feed(run(50)), notifier=sent.append,
        )
    assert len(sent) == 1


def test_it_alerts_again_once_the_cooldown_expires(tmp_path):
    state_file = tmp_path / "hb.json"
    sent = []
    for hours in (0, heartbeat.ALERT_COOLDOWN_HOURS + 1):
        heartbeat.run_if_due(
            now=NOW + timedelta(hours=hours), state_path=state_file,
            watches=(WATCH,), fetch=feed(run(50)), notifier=sent.append,
        )
    assert len(sent) == 2


def test_recovery_clears_the_cooldown_so_the_next_failure_is_not_swallowed(tmp_path):
    state_file = tmp_path / "hb.json"
    sent = []

    heartbeat.run_if_due(
        now=NOW, state_path=state_file, watches=(WATCH,),
        fetch=feed(run(50)), notifier=sent.append,
    )
    # Recovers an hour later...
    heartbeat.run_if_due(
        now=NOW + timedelta(hours=1), state_path=state_file, watches=(WATCH,),
        fetch=feed(run(1)), notifier=sent.append,
    )
    # ...then breaks again, still inside what would have been the cooldown.
    heartbeat.run_if_due(
        now=NOW + timedelta(hours=2), state_path=state_file, watches=(WATCH,),
        fetch=feed(run(50)), notifier=sent.append,
    )
    assert len(sent) == 2


def test_each_watch_has_its_own_cooldown(tmp_path):
    other = heartbeat.Watch(
        name="job-tracker flush", url="u", kind="flush", max_age_hours=30,
        expected="daily at 09:00 and 18:00",
    )
    sent = []
    out = heartbeat.run_if_due(
        now=NOW, state_path=tmp_path / "hb.json", watches=(WATCH, other),
        fetch=lambda url: [run(50), run(50, kind="flush")], notifier=sent.append,
    )
    assert out == "alerted:2"
    assert {m.split(":")[0] for m in sent} == {"job-tracker sourcing", "job-tracker flush"}


def test_state_survives_a_corrupt_file(tmp_path):
    state_file = tmp_path / "hb.json"
    state_file.write_text("{ truncated")
    out = heartbeat.run_if_due(
        now=NOW, state_path=state_file, watches=(WATCH,),
        fetch=feed(run(2)), notifier=lambda m: None,
    )
    assert out == "ok"
    assert json.loads(state_file.read_text())["last_poll"]


# --- two strikes for "can't reach it" ---------------------------------------


def test_a_single_refused_connection_does_not_alert(tmp_path):
    # Containers are not up the instant the machine is, and this box has a
    # known Docker boot race. One refused connection at 07:00 means nothing.
    sent = []
    out = heartbeat.run_if_due(
        now=NOW, state_path=tmp_path / "hb.json", watches=(WATCH,),
        fetch=boom(OSError("refused")), notifier=sent.append,
    )
    assert out == "ok"
    assert sent == []


def test_a_second_refused_connection_an_hour_later_alerts(tmp_path):
    state_file = tmp_path / "hb.json"
    sent = []
    for hours in (0, 1):
        heartbeat.run_if_due(
            now=NOW + timedelta(hours=hours), state_path=state_file,
            watches=(WATCH,), fetch=boom(OSError("refused")), notifier=sent.append,
        )
    assert len(sent) == 1
    assert "can't reach the API" in sent[0]


def test_coming_back_up_between_the_two_strikes_stays_silent(tmp_path):
    # The boot-race case exactly: refused at boot, fine an hour later.
    state_file = tmp_path / "hb.json"
    sent = []
    heartbeat.run_if_due(
        now=NOW, state_path=state_file, watches=(WATCH,),
        fetch=boom(OSError("refused")), notifier=sent.append,
    )
    heartbeat.run_if_due(
        now=NOW + timedelta(hours=1), state_path=state_file, watches=(WATCH,),
        fetch=feed(run(2)), notifier=sent.append,
    )
    heartbeat.run_if_due(
        now=NOW + timedelta(hours=2), state_path=state_file, watches=(WATCH,),
        fetch=boom(OSError("refused")), notifier=sent.append,
    )
    assert sent == []


def test_a_missing_run_alerts_on_the_first_sighting(tmp_path):
    # NOT provisional: a 30-hour hole in the schedule will not heal in an hour,
    # and the whole point is to stop losing days to silence.
    sent = []
    heartbeat.run_if_due(
        now=NOW, state_path=tmp_path / "hb.json", watches=(WATCH,),
        fetch=feed(run(50)), notifier=sent.append,
    )
    assert len(sent) == 1


def test_only_unreachability_is_provisional():
    assert heartbeat.check(WATCH, NOW, fetch=boom(OSError("x"))).provisional
    assert not heartbeat.check(WATCH, NOW, fetch=feed(run(50))).provisional
    assert not heartbeat.check(WATCH, NOW, fetch=feed(run(1, ok=False))).provisional


# --- it must never take the concierge watchdog down -------------------------


def test_a_broken_heartbeat_does_not_break_ensure_up(monkeypatch):
    from concierge import cli

    def explode():
        raise RuntimeError("state dir vanished")

    monkeypatch.setattr(heartbeat, "run_if_due", explode)
    assert cli.run_heartbeat().startswith("heartbeat-error:")


# --- the shipped config -----------------------------------------------------


def test_the_shipped_watches_cover_both_job_tracker_halves():
    # Sourcing dying and flushing dying are different failures with different
    # consequences, and the message has to say which.
    assert {w.kind for w in heartbeat.WATCHES} == {"source", "flush"}


@pytest.mark.parametrize("watch", heartbeat.WATCHES)
def test_every_shipped_watch_tolerates_a_dead_service(watch):
    msg = text_of(heartbeat.check(watch, NOW, fetch=boom(OSError("refused"))))
    assert watch.name in msg
