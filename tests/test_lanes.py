"""Fast lanes riding ensure-up.

These lanes ACT on the outside world — job-tracker's reply lane sends real
messages to real recruiters. So the bar here is higher than the heartbeat's:
nothing a lane does, however badly, may stop the concierge from coming up, and
a lane that isn't installed on this machine must be a non-event.
"""
import subprocess

import pytest

from concierge import lanes

LANE = lanes.Lane(name="test lane", command=("/bin/true",))


class FakeRun:
    def __init__(self, stdout="", returncode=0, raises=None):
        self.stdout, self.returncode, self.raises = stdout, returncode, raises
        self.calls = []

    def __call__(self, cmd, capture_output=None, text=None, timeout=None):
        self.calls.append((cmd, timeout))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


# --- the happy path ---------------------------------------------------------


def test_it_reports_the_lanes_last_line():
    runner = FakeRun(stdout="send-lane: SENT Funding Circle\nsend-lane: sent 1\n")
    assert lanes.run_lane(LANE, runner=runner) == "test lane: send-lane: sent 1"


def test_a_quiet_lane_still_reports():
    assert lanes.run_lane(LANE, runner=FakeRun(stdout="send-lane: sent 0")) == (
        "test lane: send-lane: sent 0"
    )


def test_a_silent_lane_does_not_crash_the_report():
    assert "no output" in lanes.run_lane(LANE, runner=FakeRun(stdout="   \n\n"))


def test_it_passes_the_whole_command_through():
    runner = FakeRun()
    lanes.run_lane(lanes.Lane("l", ("/bin/true", "--dry-run")), runner=runner)
    assert runner.calls[0][0] == ["/bin/true", "--dry-run"]


# --- nothing here may take the concierge down -------------------------------


def test_a_lane_that_times_out_is_reported_not_raised():
    runner = FakeRun(raises=subprocess.TimeoutExpired("cmd", 270))
    assert "timed out" in lanes.run_lane(LANE, runner=runner)


def test_a_lane_that_explodes_is_reported_not_raised():
    runner = FakeRun(raises=OSError("exec format error"))
    assert "exec format error" in lanes.run_lane(LANE, runner=runner)


def test_a_nonzero_exit_is_reported_not_raised():
    runner = FakeRun(stdout="send-lane: exited 1", returncode=1)
    out = lanes.run_lane(LANE, runner=runner)
    assert "exited 1" in out


def test_the_timeout_leaves_room_before_the_next_five_minute_tick():
    # ensure-up fires every 300s. A lane allowed to run longer would stack up
    # behind itself instead of being cut off.
    assert lanes.LANE_TIMEOUT_SECONDS < 300


def test_the_runner_is_given_the_timeout():
    runner = FakeRun()
    lanes.run_lane(LANE, runner=runner)
    assert runner.calls[0][1] == lanes.LANE_TIMEOUT_SECONDS


# --- a missing lane is a non-event ------------------------------------------


def test_an_uninstalled_lane_is_skipped_silently():
    missing = lanes.Lane(name="ghost", command=("/nonexistent/lane.sh",))
    runner = FakeRun()
    assert lanes.run_lane(missing, runner=runner) == "ghost: not installed"
    assert runner.calls == []


# --- run_all ----------------------------------------------------------------


def test_run_all_reports_every_lane_on_one_line():
    two = (lanes.Lane("a", ("/bin/true",)), lanes.Lane("b", ("/bin/true",)))
    out = lanes.run_all(two, runner=FakeRun(stdout="done"))
    assert out == "a: done | b: done"


def test_one_broken_lane_does_not_hide_the_others():
    class FirstOneFails(FakeRun):
        def __call__(self, cmd, **kw):
            self.calls.append((cmd, kw.get("timeout")))
            if len(self.calls) == 1:
                raise OSError("boom")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

    two = (lanes.Lane("bad", ("/bin/true",)), lanes.Lane("good", ("/bin/true",)))
    out = lanes.run_all(two, runner=FirstOneFails())
    assert "boom" in out
    assert "good: ok" in out


# --- the wiring into ensure-up ----------------------------------------------


def test_a_broken_lane_module_does_not_break_ensure_up(monkeypatch):
    from concierge import cli

    monkeypatch.setattr(
        lanes, "run_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gone"))
    )
    assert cli.run_lanes().startswith("lanes-error:")


def test_ensure_up_runs_the_lanes(monkeypatch):
    from concierge import cli, supervisor

    monkeypatch.setattr(supervisor, "ensure_up", lambda: "healthy")
    monkeypatch.setattr(cli, "run_heartbeat", lambda: "not-due")
    monkeypatch.setattr(cli, "run_lanes", lambda: "lanes ran")

    printed = []
    monkeypatch.setattr(cli.typer, "echo", printed.append)
    cli.ensure_up_cmd()
    assert printed == ["healthy", "not-due", "lanes ran"]


# --- the shipped config -----------------------------------------------------


def test_the_shipped_lane_points_at_the_job_tracker_sender():
    assert len(lanes.LANES) == 1
    assert lanes.LANES[0].command[0].endswith("job-tracker/scripts/send-approved.sh")


@pytest.mark.parametrize("lane", lanes.LANES)
def test_every_shipped_lane_is_actually_installed(lane):
    # If this fails the lane silently stops running, which is the failure mode
    # this whole project exists to stop happening quietly.
    assert lane.available, f"{lane.name} missing at {lane.command[0]}"
