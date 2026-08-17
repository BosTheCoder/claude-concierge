# Heartbeat

Watches scheduled work *elsewhere* and tells you when a run didn't happen.

## Why it's not part of the thing it watches

This exists because of a specific, boring disaster. A scheduled service had a
`runs` table, a "last automated run" stats card, a runs page with live log
tails, and a per-run summary email. It was stone dead for thirteen days and
produced not one signal, because every one of those lives *inside* the thing
that stopped running.

The failure mode was never "a run went wrong". It was "no run happened", and
nothing watched for absence. A service structurally cannot report its own
silence.

So this watches from outside, and it rides `ensure-up` rather than taking a
schedule of its own. The watchdog calls `ensure-up` every five minutes and is
the most reliably executed thing on the machine — it survives reboots, crashes
and Claude Code updates. A separate timer for the watchdog could rot silently in
exactly the way it exists to detect, and then who watches the watcher. Riding a
proven executor closes that loop.

## Configuring it

```toml
[[watches]]
name = "app nightly crawl"
url = "http://localhost:8000/api/runs?kind=source&limit=10"
kind = "source"
max_age_hours = 30
expected = "daily at 04:00"
```

`url` must return `{"data": [run, ...]}` newest-first, each run carrying `kind`,
`ok` and `finished_at`. Any service that grows a runs endpoint of that shape can
be watched by adding five lines.

With no watches configured this is a no-op.

## What it reports

Three distinct failures, each worth different words:

- **The service is unreachable.** "can't reach the API — the app may be down."
- **It ran and failed.** Reported even if an older successful run is still
  inside the window, because the newest word is the true state.
- **It silently never ran.** "no successful run in 47h (last was Tue 05 Aug
  04:02Z). Expected daily at 04:00." This is the one that cost thirteen days.

## Not nagging

`ensure-up` fires 288 times a day, so:

- polls are **rate limited to hourly** — there's no point asking a daily job
  whether it ran 288 times a day
- each alert has a **12-hour cooldown**, so a multi-day outage nags twice a day
  instead of 288 times
- recovery **clears the cooldown**, so the next failure alerts immediately
  instead of being swallowed by a stale timestamp

## Two strikes for an unreachable service

"Can't reach the API" is treated as *provisional*: it must be seen twice, an
hour apart, before it alerts. Containers aren't up the instant the machine is,
so a single refused connection at 07:00 says nothing.

A missing run gets no such grace. A 30-hour hole in the schedule will not heal
in an hour.

## It cannot take the concierge down

`run_heartbeat` wraps the whole thing in a total `except` and returns the error
as a string. Keeping the concierge alive is the job that matters, and a broken
heartbeat is not worth failing that over.

```bash
concierge heartbeat --force     # poll now, ignoring the hourly interval
```
