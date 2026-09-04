# The session dashboard

A page listing every Claude process on the machine, what state each one is in,
and a button to close the idle ones.

```
http://127.0.0.1:8787/        on the box
http://<tailnet-ip>:8787/     on the phone
```

## Why it exists

[reaping.md](reaping.md) covers jobs the concierge started. It reads
`state/jobs.json`, so a session opened by hand in a terminal is invisible to
it and always will be — but its ~350 MB is not, and neither are the MCP servers
beside it, which roughly double that.

The case that prompted this is Bosire on his phone with no terminal open,
wanting to know what is holding the box's memory and to get some of it back.
Measured 2026-09-04: four hand-started sessions and two finished jobs, 5.8 GB
of Claude across the process trees, 4.0 GB free on a 14 GB VM.

## How it decides whether a session is running

This is the part that matters. Killing something mid-turn loses work; leaving
something idle costs a page refresh. So the rules are ordered busy-first —
**everything that can say "running" is checked before anything that can say
"idle"**, and a session nothing can measure is `unknown` and gets no button.

Four sources, in the order `sessions.classify` consults them:

| Source | Says "running" when | Covers |
|---|---|---|
| the job registry | `status: running` | concierge jobs only |
| the tmux pane | it is drawing a live turn timer (`✽ Twisting… (3m 52s · …)`) rather than a finished one (`✻ Cooked for 11m 33s · done 9:20 AM`) | anything in tmux |
| the CPU rate | above 2.0 ticks/s | everything |
| the transcript | written to in the last 45 seconds | anything with a transcript |

A `waiting` job — blocked on a human, holding the question it asked — gets a
state of its own and is never offered, the same reasoning as in
[reaping.md](reaping.md).

### The CPU numbers

The threshold is `reaper.BUSY_TICKS_PER_SECOND`, 2.0, and it is the same number
for the same reason. Re-measured on this machine 2026-09-04:

```
idle REPLs        0.70 - 1.43 ticks/s   five sessions, several minutes each
generating        2.85 - 7.32           a no-tool essay turn
tool-using        4.53 - 5.13           a job reading and writing files
```

An idle Claude REPL is never at zero — it polls and holds an MCP stack open —
which is why "the counter did not move" is useless here.

A rate needs two readings separated by time, so a background thread samples
every 5 seconds and requests read the answer it has already computed. The rate
is taken over the **newest** window of at least 20 seconds, not the longest
available: averaging over 150 seconds kept a session that had just finished a
33-second turn reading at 7.3 ticks/s for two minutes after it stopped.

Measured end to end on a session driven through a real turn: **~7 seconds** to
notice it started, **~25 seconds** to notice it stopped.

## What may be closed

- **Never**: the concierge (window 0), and the Remote Control server. Both are
  drawn in red and carry no button. The concierge is recognised by two
  independent marks — `--remote-control concierge`, and the rendered system
  prompt only it is started with — because a false negative there costs the
  concierge itself.
- **Never**: anything `running`, `waiting` or `unknown`.
- **Otherwise**: `idle` and `finished` rows.

The UI's judgement is not trusted at the point of the kill. `sessions.reap`
re-derives the row from scratch and also checks the process's start time
against what the page was looking at, so a page left open on a phone overnight
cannot kill a recycled pid.

A registered job goes out the way the reaper closes one — kill the tmux window,
mark the row `reaped` — so `respawn <id>` still works. Anything else gets a
SIGTERM, then a SIGKILL five seconds later if it is still there.

## Reaching it from the phone

WSL2 here is in NAT mode. A listener inside WSL is reachable on the Windows
host's `127.0.0.1` and on nothing else — measured 2026-09-04: Windows could
reach `127.0.0.1:8787` and could not reach `<tailnet-ip>:8787`. Binding
`0.0.0.0` does not change that, and nothing about the Python server can.

Tailscale runs on the Windows side. Docker Desktop also runs on the Windows
side, so a *published* container port can bind the tailnet address. So:

```
phone → <tailnet-ip>:8787   (bound by Docker Desktop, on Windows)
      → socat container
      → host.docker.internal:8787   (the Windows host)
      → WSL localhostForwarding
      → the server
```

Four hops, all of them already on the machine for other reasons. `netsh
portproxy` is the obvious alternative and is worse: it needs an Administrator
shell and breaks every time WSL's IP changes on reboot.

`ensure_up` recreates the container whenever it is missing, and it carries
`--restart unless-stopped` so Docker Desktop brings it back after a reboot.

**It is bound to the tailnet address specifically, never `0.0.0.0`.** The bind
address is the access control — there is no authentication, exactly as with the
vault browser on 6080/6081 — and on a café network this page would be an
unauthenticated kill switch. If the page ever needs to leave the tailnet, add
auth first.

## Running it

Normally there is nothing to do: `ensure-up` starts it into the `dash` window
of the concierge tmux session, checks every 5 minutes that the port still
answers, and replaces the window if it does not. That window lives in the
`concierge` session, which the standalone `session-reaper` already spares.

By hand:

```bash
bin/concierge dash                  # foreground, plus the tailnet proxy
bin/concierge dash --no-expose      # localhost only
bin/concierge dash --port 9000
```

`CONCIERGE_DASHBOARD_PORT` overrides the port everywhere.

## Reading a row

- **Size** is the RSS of the session plus every process it owns — its MCP
  servers, which are usually more than the session itself. Ownership is the
  *nearest* Claude ancestor, so a session started from the phone owns its own
  subtree rather than having it counted against the Remote Control server.
- RSS double-counts pages shared between a parent and its children, so a tree
  total is an upper bound. Measured: closing a 1181 MB row returned 544 MB of
  `MemAvailable`. The footer says so.
- **Title** comes from the job registry, then the first thing a human said in
  the transcript, then the pane. The session's id is found from the scratch
  directory it holds open, or from the id printed in its own pane footer, or —
  only when exactly one transcript in that directory opened when the process
  did — by matching start times. Never a guess between two candidates.
- Short-lived `--no-session-persistence` calls (claude-mem summarising, and
  friends) are counted in the totals and the footer but are not rows. They are
  200 MB each while they last and nobody's conversation.
