# Fast lanes

Work that has to happen within minutes rather than at the next cron boundary.

## The distinction

Some jobs are crawls. There's nothing to subscribe to, and a nightly sweep is
the right shape.

Others are reactions to something a person did, where the whole value is that
the gap is short. A reply approved at 09:05 should not sit until the evening
batch just because sending happens to be bolted onto the same twice-daily run
that did the fetching.

The second kind goes here, on the five-minute `ensure-up` tick.

## Why they ride ensure-up

Same reason as the [heartbeat](heartbeat.md). The watchdog is the most reliably
executed thing on the machine. A dedicated scheduled task per lane is another
thing that can rot silently — which is the failure this whole repo exists to
stop.

## Configuring them

```toml
[[lanes]]
name = "app outbound"
command = ["~/projects/app/scripts/flush.sh"]
```

The first element is the program, and `~` is expanded in it; everything after is
passed through untouched.

With no lanes configured this is a no-op.

## The contract

This module does not know how to send anything. Each lane is an external command
that owns its own logic, its own locking and its own logging. All the concierge
does is invoke it, bound how long it may take, and make absolutely sure it can't
stop the concierge from coming up.

**Your lane should print a summary as its last line**, whether or not it did
anything. That line is what gets reported.

**Your lane should do its own `flock`.** The 270-second timeout here is a
backstop for when that isn't enough, not a substitute for it. It's under 300 so
that a wedged lane is cut off before the next tick rather than stacking up
behind itself.

**A lane whose program doesn't exist is skipped silently.** A missing lane is a
non-event, not an error — the concierge is not the installer for the things it
runs. That makes it safe to declare lanes for projects you haven't checked out
on this machine.

## Nothing a lane does can break the concierge

Lanes act on the outside world, which makes the bar higher here than for the
heartbeat. Timeouts, non-zero exits and outright exceptions all become a string
in the tick's output. One broken lane doesn't hide the others, and
`run_lanes` wraps the lot in a total `except` on top.

```bash
concierge lanes             # run them now
concierge lanes --dry-run   # appends --dry-run to each lane's command
```

`--dry-run` is passed through to your script. Whether it means anything is up to
you.
