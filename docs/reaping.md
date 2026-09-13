# Closing finished jobs

`claude '<brief>'` does not exit when its turn ends. It drops to an interactive
REPL and holds its whole MCP stack open there — measured at ~350 MB for the
pane process and 700-900 MB for the tree. Nothing closed that window.

On 2026-08-29 four finished jobs had been sitting like that, the oldest for 30
hours. Closing them by hand took Claude processes from 6.8 GB to 3.5 GB and free
memory from 1.0 GB to 2.6 GB on a 13 GB machine.

`concierge/reaper.py` closes them. It rides `ensure-up`, so it runs every five
minutes on the same tick as the Remote Control sweep, the heartbeat and the fast
lanes, wrapped in the same total `try/except`: a bug in the reaper must never
stop the concierge coming up, and this is the one of the four that sends
signals.

## Why not the session-reaper

`win-scheduled-tasks/scripts/session-reaper.py` already sweeps leaked Claude
sessions every 6 hours. It did not miss these four — it **spared** them, by
name, on every run since 17 August:

```python
PROTECTED_TMUX = {"concierge", "rc"}
```

That protection has to stay. The session-reaper cannot read `state/jobs.json`,
so it cannot tell a finished job's window from window 0, which is the concierge
itself — and loosening the rule would put "may kill window 0" into a script that
runs unattended. Only the registry knows which window is which, so the decision
belongs here, and the session-reaper keeps doing the different job it was
written for.

The concierge already had the shape of this: `supervisor.prunable` kills the
window of any job finished more than 7 days ago, with the comment *"a finished
job leaves its REPL open"*. This is the same idea at a useful timescale, plus
the guards that a 7-day delay made unnecessary. The 7-day prune stays as the
backstop for everything the reaper declines to touch.

## The rules

Keyed on the registry `status`, which the job itself sets by passing
`--status` to `notify`.

| status | what happens |
|---|---|
| `running` | never touched, at any age |
| `done`, `failed` | closed once the grace period has passed and the pane is idle |
| `waiting` | **not** on the same timer — nudged, then closed if still unanswered |
| `killed`, `orphaned`, `respawned`, `reaped` | window closed at once, no grace, no message |

### Why the grace period is not zero

A follow-up from Telegram reaches a running job by being typed into its own tmux
window (`concierge send A3`). Close the window the instant a job
reports and that follow-up lands nowhere.

15 minutes is the default. It was 90 until 2026-09-04, and the reason it came
down is that the trade is not symmetric. Memory is the binding constraint on a
14 GB WSL VM — a handful of finished REPLs takes the box under 1 GB free — while
the cost of closing one early is one command: the registry keeps the brief and
the task folder, so `respawn <id>` starts the work again from what was written
down.

What 15 minutes gives up is the leisurely follow-up. Reply within the quarter
hour and the message still lands in the job's own window; reply an hour later
and it lands nowhere, and you respawn instead. Bosire took that trade
explicitly. Lengthen it if you follow up more often than you run out of RAM.

### Why `waiting` is separate

T7 sat blocked for 29 hours legitimately waiting for Bosire to answer a
question. Reaping that on the finished timer silently loses the question.

So a `waiting` job gets `waiting_nudge_hours` (24h) before anything happens, then
**re-sends its question** — quoting the text of its last message, which `notify`
records on the row for exactly this — and only closes `waiting_grace_hours` (6h)
after that if it is still unanswered. The nudge goes to the job's own
conversation, never to the notifications channel: a job asking Bosire something
is the one class of traffic the channel split exists to protect.

If the nudge fails to send, the job is not closed.

## The guards

Each spares the job and writes the reason to `state/reaper.log`.

1. **The pane must be running `claude`.** A pane that has fallen back to a bare
   shell is an empty terminal and is closed freely. A pane running anything else
   is someone else's and is left alone.

2. **The pane must be idle, measured as a rate.** This is where the obvious
   implementation is wrong. The session-reaper's rule B tests that the CPU
   counter is *unchanged* between runs; applied to these panes that is never
   true, and reusing it made the reaper a silent no-op that reported success.
   Measured 2026-08-29 over 88 seconds:

   | pane | state | ticks/sec |
   |---|---|---|
   | M1 | idle, finished | 0.84 |
   | T7 | idle, waiting | 0.82 |
   | Q4 | idle, finished | 0.80 |
   | F8 | working | 4.18 |

   An idle Claude REPL polls and holds an MCP stack open, so it never reaches
   zero — but the gap to a working session is a clean 5x. The threshold is
   **2.0 ticks/sec**, 2.4x idle and half of working. A sample is discarded
   entirely if the gap between ticks is outside 30-1800s (the machine was
   asleep, and averaging a burst of work across ten hours reads as idle) or if
   the counter went backwards (recycled pid).

   This costs one tick of lag: the first sweep after a restart records a
   baseline and reaps nothing.

3. **The work must exist on disk.** The job's task folder must contain a file
   written at or after the job opened. A job whose output exists only in its own
   context is never closed here; it is left to the 7-day prune, by which time
   someone will have looked. A job with no `task_folder` recorded cannot be
   checked and is treated the same way.

## Configuration

```toml
[reaper]
enabled = true
finished_grace_minutes = 15
waiting_nudge_hours = 24
waiting_grace_hours = 6
```

## Looking at it

```bash
bin/concierge reap --dry-run   # what the rules say about the registry right now
bin/concierge reap             # force a sweep
cat state/reaper.log           # what it closed, and what it spared and why
```

The log is written only on a tick that decided something, so it stays readable
despite `ensure-up` firing 288 times a day. A job is logged as spared only once
it is old enough to be a candidate — otherwise every finished job would produce
a line every five minutes for its whole grace period.

## Verified

2026-08-29, driven by the real `\Bosire\concierge\Watchdog` scheduled task, not
by hand:

```
21:30:02 | reaped=0 nudged=1
  NUDGE T7: waiting on a human 31.0h with no reply
21:35:02 | reaped=1 nudged=0
  REAP M1: done 1.4h ago, idle at 0.87 ticks/s, output on disk
  spare Q4: no usable CPU sample yet
  spare T7: nudged 0.1h ago, closing at 6h
21:40:02 | reaped=1 nudged=0
  REAP Q4: done 0.1h ago, idle at 0.91 ticks/s, output on disk
  spare T7: nudged 0.2h ago, closing at 6h
```

Q4 was a throwaway job spawned for the test: it wrote a file to its task folder,
went idle at its REPL, was spared on the tick that had no baseline, and closed on
the next. M1 was a real finished job from earlier that evening. T7 was blocked on
a human throughout and was never closed. F8 was running throughout and was never
touched. Free memory went from 2,643 MB to 4,356 MB.
