"""The reaper sends signals, so the rules are tested rather than the plumbing.

`decide` is pure by design — every fact it needs is passed in — so the whole
rule set can be exercised without a tmux server, a /proc or a clock. The tests
that matter here are the ones that assert the reaper does NOT act: a job killed
mid-turn is the expensive failure, and a job that lingers is the cheap one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from concierge import config, reaper, registry
from concierge.settings import Reaper as ReaperSettings

NOW = datetime(2026, 8, 29, 22, 0, tzinfo=timezone.utc)
SETTINGS = ReaperSettings()


def job(**over) -> dict:
    base = {
        "id": "A3",
        "status": "done",
        "title": "a job",
        "chat_id": "123",
        "cwd": "/repo",
        "task_folder": "2026-08-29-thing",
        "opened_at": (NOW - timedelta(hours=5)).isoformat(),
        "last_update": (NOW - timedelta(hours=4)).isoformat(),
        "tmux_window": "A3",
    }
    base.update(over)
    return base


IDLE = 0.82   # measured on a real finished session, 2026-08-29
WORKING = 4.18  # measured on a real session mid-task, same day


def call(row, *, rate=IDLE, command="claude", settings=SETTINGS):
    return reaper.decide(
        row, now=NOW, rate=rate, pane_command=command, settings=settings
    )


@pytest.fixture
def wrote_output(monkeypatch):
    monkeypatch.setattr(reaper, "has_written_output", lambda j: True)


@pytest.fixture
def wrote_nothing(monkeypatch):
    monkeypatch.setattr(reaper, "has_written_output", lambda j: False)


# --- the job that must never die --------------------------------------------


def test_running_job_is_spared_however_old(wrote_output):
    old = job(status="running", last_update=(NOW - timedelta(days=9)).isoformat())
    assert call(old).action == "spare"


def test_finished_job_still_burning_cpu_is_spared(wrote_output):
    # It said `done` then carried on — committing and pushing after notifying
    # is the ordinary case, and the grace period alone would not catch it.
    assert call(job(), rate=WORKING).action == "spare"


def test_an_idle_repl_is_not_mistaken_for_a_working_one(wrote_output):
    """The bug the unit tests could not see, found by measuring a live pane.

    An idle Claude REPL polls and holds an MCP stack open, so it never sits at
    zero. The first cut of this reaper reused the session-reaper's "CPU counter
    unchanged" test, which is never true of these panes — every job would have
    been spared forever while the reaper reported success.
    """
    assert call(job(), rate=IDLE).action == "reap"


def test_finished_job_with_no_previous_sample_is_spared(wrote_output):
    assert call(job(), rate=None).action == "spare"


def test_job_that_wrote_nothing_is_spared(wrote_nothing):
    decision = call(job())
    assert decision.action == "spare"
    assert "task folder" in decision.why


def test_pane_running_something_else_is_spared(wrote_output):
    assert call(job(), command="vim").action == "spare"


# --- the job that should go --------------------------------------------------


def test_finished_idle_job_with_output_is_reaped(wrote_output):
    assert call(job()).action == "reap"


def test_still_inside_the_grace_period_is_spared(wrote_output):
    # Comfortably inside the shipped default, which is 15 minutes and has been
    # shortened before — a fixture pinned just under it breaks on the next cut.
    fresh = job(last_update=(NOW - timedelta(minutes=2)).isoformat())
    assert call(fresh).action == "spare"


def test_grace_period_is_configurable(wrote_output):
    fresh = job(last_update=(NOW - timedelta(minutes=20)).isoformat())
    impatient = ReaperSettings(finished_grace_minutes=5)
    assert call(fresh, settings=impatient).action == "reap"


def test_killed_job_window_goes_with_no_grace(wrote_output):
    row = job(status="killed", last_update=NOW.isoformat())
    assert call(row).action == "reap"


def test_bare_shell_pane_is_reaped(wrote_nothing):
    # The session already exited; the window is an empty terminal, and there is
    # no context left to lose.
    assert call(job(status="running"), command="zsh").action == "spare"
    assert call(job(), command="zsh").action == "reap"


def test_closed_window_is_not_a_decision(wrote_output):
    assert call(job(), command=None).action == "gone"


# --- the job that is waiting on a human -------------------------------------


def test_waiting_job_is_not_reaped_on_the_finished_timer(wrote_output):
    # T7 sat like this for 29 hours legitimately waiting for an answer.
    row = job(status="waiting", last_update=(NOW - timedelta(hours=4)).isoformat())
    assert call(row).action == "spare"


def test_waiting_job_is_nudged_not_reaped_when_it_ages_out(wrote_output):
    row = job(status="waiting", last_update=(NOW - timedelta(hours=29)).isoformat())
    assert call(row).action == "nudge"


def test_waiting_job_is_spared_while_the_nudge_is_still_fresh(wrote_output):
    row = job(
        status="waiting",
        last_update=(NOW - timedelta(hours=29)).isoformat(),
        nudged_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    assert call(row).action == "spare"


def test_waiting_job_closes_only_after_a_nudge_went_unanswered(wrote_output):
    row = job(
        status="waiting",
        last_update=(NOW - timedelta(hours=40)).isoformat(),
        nudged_at=(NOW - timedelta(hours=7)).isoformat(),
    )
    assert call(row).action == "reap"


# --- has_written_output ------------------------------------------------------


def test_output_written_after_the_job_opened_counts(tmp_path):
    folder = tmp_path / "2026-08-29-thing"
    folder.mkdir()
    (folder / "report.md").write_text("findings")
    row = job(cwd=str(tmp_path), opened_at=(NOW - timedelta(days=1)).isoformat())
    assert reaper.has_written_output(row) is True


def test_a_folder_that_predates_the_job_does_not_count(tmp_path):
    folder = tmp_path / "2026-08-29-thing"
    folder.mkdir()
    stale = folder / "index.md"
    stale.write_text("written by the concierge at spawn time")
    long_ago = (NOW - timedelta(days=30)).timestamp()
    import os

    os.utime(stale, (long_ago, long_ago))
    row = job(cwd=str(tmp_path), opened_at=(NOW - timedelta(days=1)).isoformat())
    assert reaper.has_written_output(row) is False


def test_missing_task_folder_is_treated_as_unproven(tmp_path):
    assert reaper.has_written_output(job(cwd=str(tmp_path))) is False
    assert reaper.has_written_output(job(task_folder=None)) is False


# --- cpu_rate ----------------------------------------------------------------


def test_cpu_rate_is_ticks_per_second():
    assert reaper.cpu_rate(1000, 1264, 300) == pytest.approx(0.88)


def test_cpu_rate_has_no_opinion_without_two_samples():
    assert reaper.cpu_rate(None, 1264, 300) is None
    assert reaper.cpu_rate(1000, None, 300) is None
    assert reaper.cpu_rate(1000, 1264, None) is None


def test_a_gap_far_from_the_tick_interval_is_discarded():
    """The machine is off overnight. Averaging a burst of real work across ten
    hours of downtime reads as idle, and would reap a job mid-turn on the first
    tick after wake."""
    assert reaper.cpu_rate(1000, 90000, 40000) is None
    assert reaper.cpu_rate(1000, 1010, 3) is None


def test_a_counter_that_went_backwards_is_discarded():
    # The pane's process was replaced under a recycled pid.
    assert reaper.cpu_rate(90000, 12, 300) is None


# --- the sweep ---------------------------------------------------------------


class FakeTmux:
    def __init__(self, commands: dict[str, str | None]):
        self.commands = commands
        self.killed: list[str] = []

    def window_command(self, session, window):
        return self.commands.get(window)

    def kill_window(self, session, window):
        self.killed.append(window)

    def _run(self, argv):  # pane_pid asks for this
        class Result:
            returncode = 1
            stdout = ""

        return Result()


def test_run_kills_the_window_and_marks_the_row(tmp_path, monkeypatch):
    monkeypatch.setattr(reaper, "has_written_output", lambda j: True)
    state = tmp_path / "jobs.json"
    registry.save({"A3": job(status="killed")}, state)
    tmux = FakeTmux({"A3": "claude"})

    out = reaper.run(
        now=NOW,
        registry_path=state,
        cpu_path=tmp_path / "reaper.json",
        tmux=tmux,
        logger=lambda *a: None,
    )

    assert tmux.killed == ["A3"]
    assert registry.load(state)["A3"]["status"] == "reaped"
    assert out.startswith("reaped:1")


def test_run_nudges_through_the_conversation_not_the_ops_channel(tmp_path):
    state = tmp_path / "jobs.json"
    registry.save(
        {
            "T7": job(
                id="T7",
                status="waiting",
                tmux_window="T7",
                last_update=(NOW - timedelta(hours=29)).isoformat(),
                last_message="which Andrew did you mean?",
            )
        },
        state,
    )
    tmux = FakeTmux({"T7": "claude"})
    sent: list[tuple[str, str]] = []

    reaper.run(
        now=NOW,
        registry_path=state,
        cpu_path=tmp_path / "reaper.json",
        tmux=tmux,
        nudger=lambda j, text: sent.append((j["chat_id"], text)),
        logger=lambda *a: None,
    )

    assert tmux.killed == []
    chat_id, text = sent[0]
    assert chat_id == "123"  # the job's own chat, never the notifications one
    assert "which Andrew did you mean?" in text
    assert registry.load(state)["T7"]["nudged_at"]


def test_a_failed_nudge_does_not_close_the_job(tmp_path):
    state = tmp_path / "jobs.json"
    registry.save(
        {
            "T7": job(
                id="T7",
                status="waiting",
                tmux_window="T7",
                last_update=(NOW - timedelta(hours=29)).isoformat(),
            )
        },
        state,
    )
    tmux = FakeTmux({"T7": "claude"})

    def explode(job_row, text):
        raise RuntimeError("telegram down")

    reaper.run(
        now=NOW,
        registry_path=state,
        cpu_path=tmp_path / "reaper.json",
        tmux=tmux,
        nudger=explode,
        logger=lambda *a: None,
    )

    assert tmux.killed == []
    assert registry.load(state)["T7"].get("nudged_at") is None


def test_disabled_does_nothing_at_all(tmp_path):
    state = tmp_path / "jobs.json"
    registry.save({"A3": job(status="killed")}, state)
    tmux = FakeTmux({"A3": "claude"})
    out = reaper.run(
        now=NOW,
        registry_path=state,
        cpu_path=tmp_path / "reaper.json",
        tmux=tmux,
        settings=ReaperSettings(enabled=False),
    )
    assert out == "disabled"
    assert tmux.killed == []


def test_cpu_samples_carry_across_ticks(tmp_path, monkeypatch):
    """The whole point of the CPU guard: tick one records, tick two decides.

    Driven at the real cadence — 5 minutes apart, the pane accruing the idle
    0.82 ticks/sec measured on a live session — so the arithmetic that decides
    is the arithmetic that runs.
    """
    monkeypatch.setattr(reaper, "has_written_output", lambda j: True)
    monkeypatch.setattr(reaper, "pane_pid", lambda s, w, tmux=None: 4242)

    state = tmp_path / "jobs.json"
    cpu = tmp_path / "reaper.json"
    registry.save({"A3": job()}, state)
    tmux = FakeTmux({"A3": "claude"})

    monkeypatch.setattr(reaper, "cpu_ticks", lambda pid: 900)
    first = reaper.run(
        now=NOW, registry_path=state, cpu_path=cpu, tmux=tmux, logger=lambda *a: None
    )
    assert first == "nothing-to-reap"
    assert json.loads(cpu.read_text())["cpu"] == {"A3": 900}
    assert tmux.killed == []

    monkeypatch.setattr(reaper, "cpu_ticks", lambda pid: 900 + int(0.82 * 300))
    second = reaper.run(
        now=NOW + timedelta(seconds=300),
        registry_path=state,
        cpu_path=cpu,
        tmux=tmux,
        logger=lambda *a: None,
    )
    assert tmux.killed == ["A3"]
    assert second.startswith("reaped:1")


def test_a_job_working_between_ticks_survives_the_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(reaper, "has_written_output", lambda j: True)
    monkeypatch.setattr(reaper, "pane_pid", lambda s, w, tmux=None: 4242)

    state = tmp_path / "jobs.json"
    cpu = tmp_path / "reaper.json"
    registry.save({"A3": job()}, state)
    tmux = FakeTmux({"A3": "claude"})

    monkeypatch.setattr(reaper, "cpu_ticks", lambda pid: 900)
    reaper.run(now=NOW, registry_path=state, cpu_path=cpu, tmux=tmux, logger=lambda *a: None)

    monkeypatch.setattr(reaper, "cpu_ticks", lambda pid: 900 + int(4.18 * 300))
    reaper.run(
        now=NOW + timedelta(seconds=300),
        registry_path=state,
        cpu_path=cpu,
        tmux=tmux,
        logger=lambda *a: None,
    )
    assert tmux.killed == []


# --- the notification split --------------------------------------------------


def test_ops_traffic_goes_to_the_notifications_chat_when_set(tmp_path, monkeypatch):
    from concierge import supervisor

    registry.save({"A3": job(chat_id="8183714282")}, tmp_path / "jobs.json")
    monkeypatch.setattr(config, "NOTIFICATIONS_CHAT_ID", "-1009999")
    assert supervisor.ops_destination(tmp_path / "jobs.json") == "-1009999"


def test_ops_traffic_falls_back_to_the_conversation_when_unset(tmp_path, monkeypatch):
    from concierge import supervisor

    registry.save({"A3": job(chat_id="8183714282")}, tmp_path / "jobs.json")
    monkeypatch.setattr(config, "NOTIFICATIONS_CHAT_ID", "")
    assert supervisor.ops_destination(tmp_path / "jobs.json") == "8183714282"


def test_the_nudge_does_not_reset_the_idle_clock(tmp_path):
    """Caught live: the first real nudge moved `last_update` to now.

    `last_update` is the clock both waiting timers measure from, so recording
    the nudge on the row reset the "waiting 31h with no reply" reading that
    caused it, and the job then read as freshly active.
    """
    state = tmp_path / "jobs.json"
    waited_since = (NOW - timedelta(hours=29)).isoformat()
    registry.save(
        {"T7": job(id="T7", status="waiting", tmux_window="T7", last_update=waited_since)},
        state,
    )

    reaper.run(
        now=NOW,
        registry_path=state,
        cpu_path=tmp_path / "reaper.json",
        tmux=FakeTmux({"T7": "claude"}),
        nudger=lambda j, text: None,
        logger=lambda *a: None,
    )

    row = registry.load(state)["T7"]
    assert row["last_update"] == waited_since
    assert row["nudged_at"]
