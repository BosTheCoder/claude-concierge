"""Fast lanes: work that has to happen within minutes, not at a cron boundary.

Some jobs are crawls — nothing to subscribe to, a nightly sweep is the right
shape. Others are reactions to something a person did, where the whole value is
that the gap is short. A reply lane is the second kind: a message approved at
09:05 should not wait until the evening batch to go out because sending happens
to be bolted onto the same twice-daily run that did the fetching.

Those lanes live here, and they ride `ensure-up` for the same reason the
heartbeat does: the watchdog calls it every five minutes and is the most
reliably executed thing on the machine. A dedicated scheduled task per fast
lane is another thing that can rot silently, which is the failure this repo
exists to stop.

This module does not know how to send anything. Each lane is an external
command that owns its own logic, its own locking and its own logging; all this
does is invoke it, bound how long it may take, and make absolutely sure it
cannot stop the concierge from coming up. Lanes are declared in
`concierge.toml`; with none declared this is a no-op.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from concierge import config

# Hard ceiling per lane. `ensure-up` runs every 5 minutes and its real job is
# keeping the concierge alive, so a wedged lane gets cut off well before the
# next tick rather than stacking up behind it. Lanes are expected to do their
# own `flock`; this is the backstop for when that isn't enough.
LANE_TIMEOUT_SECONDS = 270


@dataclass(frozen=True)
class Lane:
    """One fast lane: a command to run and a name to report it under."""

    name: str
    command: tuple[str, ...]

    @property
    def available(self) -> bool:
        """False when the lane's project isn't checked out on this machine. A
        missing lane is a non-event, not an error — the concierge is not the
        installer for the things it runs."""
        return Path(self.command[0]).exists()


LANES: tuple[Lane, ...] = tuple(
    Lane(name=spec.name, command=spec.command) for spec in config.SETTINGS.lanes
)


def run_lane(lane: Lane, runner=subprocess.run) -> str:
    """Run one lane and return a one-line outcome. Never raises."""
    if not lane.available:
        return f"{lane.name}: not installed"
    try:
        result = runner(
            list(lane.command),
            capture_output=True,
            text=True,
            timeout=LANE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"{lane.name}: timed out after {LANE_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return f"{lane.name}: {exc}"

    # The lane's own last line is its summary; the wrapper prints one whether
    # it sent anything or not.
    tail = [ln for ln in (result.stdout or "").strip().splitlines() if ln.strip()]
    if result.returncode != 0:
        return f"{lane.name}: exited {result.returncode} — {(tail or ['no output'])[-1]}"
    return f"{lane.name}: {(tail or ['no output'])[-1]}"


def run_all(lanes: tuple[Lane, ...] = LANES, runner=subprocess.run) -> str:
    """Every fast lane, one line each. Called from `ensure-up`."""
    return " | ".join(run_lane(lane, runner=runner) for lane in lanes)
