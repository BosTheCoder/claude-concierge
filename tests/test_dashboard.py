"""The CPU sampler, which is the thing that answers "is it still working?".

A rate this gets wrong in the low direction puts a Reap button under a session
that is mid-turn. Every test here is a way that could happen.
"""

from concierge import dashboard, sessions


def sampler_with(readings, interval=5.0):
    """Feed a Sampler a fixed series of (time, ticks) for one pid."""
    sampler = dashboard.Sampler(interval=interval)
    for at, ticks in readings:
        sampler._history.setdefault(1, []).append((at, ticks))
    return sampler


def rate(readings, *, now=None):
    sampler = sampler_with(readings)
    return sampler.rates(now=now if now is not None else readings[-1][0]).get(1)


def test_a_steady_load_comes_out_as_ticks_per_second():
    assert rate([(0.0, 0), (25.0, 250)]) == 10.0


def test_a_single_reading_has_no_rate():
    # And `classify` turns a missing rate into `unknown`, which shows no
    # button. That is the intended behaviour for the first seconds of a poll.
    assert rate([(0.0, 0)]) is None


def test_a_window_shorter_than_the_minimum_is_refused():
    # An idle REPL polls in bursts. Over five seconds one burst reads as
    # several ticks per second, which is indistinguishable from working.
    assert rate([(0.0, 0), (5.0, 40)]) is None


def test_the_newest_usable_baseline_is_the_one_used():
    """Measured 2026-09-04: averaging over 150 seconds kept a session that had
    finished a 33-second turn reading as 7.3 ticks/s — 'running' — for two
    minutes after it stopped. The rate has to be recent, not long."""
    busy_then_quiet = [
        (0.0, 0), (10.0, 100), (20.0, 200), (30.0, 300),  # working
        (40.0, 305), (50.0, 310), (60.0, 315),            # stopped
    ]
    assert rate(busy_then_quiet) == 0.5


def test_a_stalled_sampler_reports_nothing():
    # The thread died or the machine slept. Two real readings exist, but the
    # newest is minutes old and says nothing about now.
    assert rate([(0.0, 0), (25.0, 250)], now=1000.0) is None


def test_a_recycled_pid_does_not_produce_a_rate(monkeypatch):
    """The counter going backwards means a different process is wearing this
    pid. Comparing across the two would produce an arbitrary number."""
    sampler = dashboard.Sampler()
    monkeypatch.setattr(sessions, "is_claude", lambda proc: True)
    monkeypatch.setattr(
        sessions, "scan",
        lambda: {1: sessions.Proc(1, 0, ("claude",), 100, 900, 1)},
    )
    sampler.sample(now=0.0)
    monkeypatch.setattr(
        sessions, "scan",
        lambda: {1: sessions.Proc(1, 0, ("claude",), 100, 3, 1)},
    )
    sampler.sample(now=25.0)
    assert sampler.rates(now=25.0) == {}


def test_a_process_that_exits_is_forgotten(monkeypatch):
    sampler = dashboard.Sampler()
    monkeypatch.setattr(sessions, "is_claude", lambda proc: True)
    monkeypatch.setattr(
        sessions, "scan",
        lambda: {1: sessions.Proc(1, 0, ("claude",), 100, 10, 1)},
    )
    sampler.sample(now=0.0)
    monkeypatch.setattr(sessions, "scan", dict)
    sampler.sample(now=5.0)
    assert sampler._history == {}


def test_the_measured_idle_and_working_rates_sit_either_side_of_the_threshold():
    """The numbers the whole thing turns on, kept where a future edit to the
    threshold has to look at them. All measured on this machine 2026-09-04.

    idle REPLs      0.70 - 1.43 ticks/s   (five sessions, several minutes)
    generating      2.85 - 7.32           (a no-tool essay turn)
    tool-using      4.53 - 5.13           (a job reading and writing files)
    """
    for idle in (0.70, 0.80, 0.90, 1.38, 1.43):
        assert idle < sessions.BUSY_TICKS_PER_SECOND
    for working in (2.85, 3.14, 4.53, 7.32):
        assert working > sessions.BUSY_TICKS_PER_SECOND
