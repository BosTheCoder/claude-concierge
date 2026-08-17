# Architecture

## The shape

One tmux session. Window 0 holds the concierge, a long-lived Claude Code
session with the Telegram channel plugin attached. Every job is another window
in the same session holding its own `claude` session.

```
tmux "concierge"
├── 0    concierge   claude --channels plugin:telegram… --remote-control concierge
├── A3   job         claude --remote-control "[A3] calibre cleanup"
└── K7   job         claude --remote-control "[K7] chase the invoice"
```

State lives in `state/jobs.json`, a flat map of job id to row. It's the only
thing that survives a restart, and it's the reason a job can be respawned from
its brief after a reboot killed it mid-flight.

## Modules

| | |
|---|---|
| `settings.py` | Loads `concierge.toml`; renders the prompt templates |
| `config.py` | Constants, and the settings other modules read |
| `cli.py` | Every command. Jobs and the concierge call this, never the API |
| `spawn.py` | One job: one tmux window, one session, one registry row |
| `registry.py` | `state/jobs.json` — atomic writes, id allocation |
| `supervisor.py` | `ensure-up`: liveness, orphan reconciliation, the prune |
| `telegram.py` | Outbound only. Inbound is the channel plugin's job |
| `tmuxctl.py` | Thin injectable wrapper over the tmux commands used |
| `links.py` | Repo path → GitHub blob URL; age humanising |
| `rc.py` | Sweeps sessions that fell off Remote Control, types `/rc` |
| `rcserver.py` | Keeps `claude remote-control` (server mode) up |
| `heartbeat.py` | Watches scheduled work elsewhere for absence |
| `lanes.py` | Runs minute-latency external commands on the tick |

`prompts/concierge.md` and `prompts/job.md` are the two system prompts. They're
templates: `{{REPO_ROUTING}}` and `{{CONCIERGE_BIN}}` are substituted from
`concierge.toml` at spawn time and written to `state/*.rendered.md`. That's why
the committed prompts name nobody's repos.

## Design decisions

### The concierge never does the work

Channel events queue into one session in order, so a long task in the concierge
blocks every other message you send. Its system prompt is explicit: trivial
lookups answered inline in under six lines, everything else spawned. Six lines
because it's a phone: no tables, no code blocks, no headings.

### A job's identity is an environment variable

`CONCIERGE_JOB_ID` is set as an assignment prefix on the `exec` that starts the
session, so the process itself carries it. This matters because a system prompt
does not survive context compaction and neither does a session name. An hour
into a job, the env var is still the only thing that reliably answers "who am
I".

### A job cannot message the wrong chat

`concierge notify "done"` takes no chat id. The CLI looks the destination up
from the job id in the registry. There is no argument for a confused model to
get wrong.

### Ids are never recycled

Job ids are a letter and a digit, drawn from an alphabet with no I, L or O,
because those misread as 1 and 0 on a phone. Finished ids are **not** returned
to the pool when the job ends, because nothing kills a job's tmux window then:
`claude '<brief>'` leaves an interactive REPL behind, and tmux allows duplicate
window names. A recycled id would capture the wrong pane, overwrite the wrong
row, and let `/kill` destroy a live session. The 7-day prune is what frees them,
and it kills the window as it goes.

### "Session exists" is not liveness

`tmux has-session` is not enough, because every job is a window in the same
session: a live job keeps the session up long after the concierge's own process
has been OOM-killed. So the check reads window 0's `pane_current_command` and
looks for `claude`. Without that, `ensure-up` reports healthy while messages
pile up unread.

### ensure-up must be unkillable

The five-minute watchdog is the only real guarantee that anything comes back
after a reboot. So its three passengers (the rc sweep, the heartbeat, the fast
lanes) are each wrapped in a total `except` that turns a failure into a string.
A bug in the heartbeat must not be able to take the concierge down; keeping the
concierge alive is the job that matters.

`ensure-up` is also idempotent and silent when healthy. It never touches a
running session, so it can't interrupt a live turn.

### Spawning verifies, and says so when it fails

`spawn` watches the new pane for the Remote Control URL for up to ~20 seconds.
No URL is not automatically fatal, since the session is named `[A3] <title>` and
is still findable at claude.ai/code. But no URL *and* no window means it died on
the first line, and that raises rather than registering a job that will never
run.

### Alerts go to one chat, never a broadcast

The destination is the most recently touched job's chat, with an active job
winning a same-second tie, falling back to `state/last_chat`, which is the only
thing that exists on a fresh registry, or the morning after the prune emptied
it. If there's nowhere to send, the message still goes to stderr; a hidden
detached run with no destination would otherwise swallow the one alert that
matters most.

## Testing

238 tests, no network, no real tmux. An autouse fixture makes any unstubbed call
into `tmuxctl._run` an immediate failure. Not hypothetical caution: adding the
rc sweep to `ensure-up` once opened a path into the real `tmux send-keys`, and
running the suite typed `/rc` into a live concierge and left a dialog open on it.

The suite runs against `tests/fixture.toml` rather than your `concierge.toml`,
so it asserts on the code rather than on whatever the machine happens to be set
up for.

```bash
just test
```
