# claude-concierge

Message Claude Code from your phone. It triages what you asked for, spawns a
real Claude Code session to do the work, and messages you back when it needs a
decision or when it's done.

The chat is a dispatcher, not a worker. Anything substantial becomes its own
session in its own tmux window with its own Remote Control link, so a long job
never blocks the next message and you can watch any of them live by tapping the
link.

```
you  ▸ the calibre epubs have duplicate covers, can you sort it out
bot  ▸ 👀
bot  ▸ two questions: (1) dedupe by ISBN or by title+author? (2) delete the
       losers or move them to a review folder?
you  ▸ isbn, move them
bot  ▸ on it — spawning a job
bot  ▸ [A3] on it ▸ https://claude.ai/code/session_01ABC — tap it to watch
       …
bot  ▸ [A3] done — 27 books, 4 duplicates moved to review/
       https://github.com/you/notes/blob/main/2026-08-05-calibre/report.md
```

## Why it works this way

Three decisions carry most of the design.

**The chat session never does the work.** Channel events queue into one session
in order, so a single long task there would block every other message you send.
The concierge's whole job is to ask the clarifying questions, write the brief,
and hand off.

**A job's identity lives in an environment variable.** `CONCIERGE_JOB_ID` is set
on the process at spawn. A system prompt does not survive context compaction and
neither does the session name; an env var does. That's what lets a job that has
been running for an hour still report to the right chat.

**Jobs never see a chat id.** `concierge notify "done"` looks the destination up
from the job id in the registry. There is no argument a confused model could get
wrong, so a job structurally cannot message the wrong person.

## How it fits together

```
   Telegram
      │
      │  channel plugin (one getUpdates consumer per bot token)
      ▼
┌─────────────────────────────────────────────────────────┐
│ tmux session "concierge"                                │
│                                                         │
│  window 0    the concierge — triage, questions, spawn   │
│  window A3   job: claude --remote-control "[A3] title"  │
│  window K7   job: claude --remote-control "[K7] title"  │
└─────────────────────────────────────────────────────────┘
      │                              ▲
      │ notify (chat looked up       │ ensure-up, every 5 minutes
      │ from the job id)             │ from your scheduler
      ▼                              │
   Telegram                    ┌─────┴──────────────────────┐
                               │ supervisor  is it alive?   │
                               │ rc sweep    still bridged? │
                               │ heartbeat   did cron run?  │
                               │ lanes       urgent work    │
                               └────────────────────────────┘
```

`ensure-up` is the only thing your scheduler needs to call. It is idempotent and
silent when healthy, so running it every five minutes costs nothing and is the
actual guarantee that the concierge comes back after a reboot, a crash, or a
Claude Code update. It carries three passengers, each wrapped so that a failure
in one cannot stop the concierge from starting:

- **[rc sweep](docs/remote-control.md):** sessions drop off Remote Control
  silently. This finds them and reconnects them.
- **[heartbeat](docs/heartbeat.md):** watches scheduled work *elsewhere* and
  tells you when a run didn't happen. A service's own dashboard cannot report
  this, because the dashboard is inside the thing that stopped.
- **[fast lanes](docs/fast-lanes.md):** work that has to happen within minutes
  rather than at the next cron boundary.

All three are optional. With nothing configured they're no-ops.

## Requirements

- [Claude Code](https://claude.com/claude-code) with an active subscription
- `tmux`
- [`uv`](https://docs.astral.sh/uv/) and Python 3.14+
- A Telegram bot token from [@BotFather](https://t.me/botfather)
- Something that runs a command every five minutes: Task Scheduler, systemd
  timers, cron

Developed on WSL2; anything Linux-shaped with tmux should work. macOS is
untested.

## Install

```bash
git clone https://github.com/BosTheCoder/claude-concierge
cd claude-concierge
uv sync
cp concierge.example.toml concierge.toml
$EDITOR concierge.toml          # at minimum, one [[repos]] entry
```

Then follow **[docs/installation.md](docs/installation.md)** for the Telegram
bot, the channel plugin, and the scheduled tasks. Read the plugin section
before you start it. There's one trap that fails completely silently, covered
below.

Check it:

```bash
just test
just up          # start the concierge; safe to run repeatedly
just jobs
```

## Configuration

Everything installation-specific lives in `concierge.toml`, which is gitignored.
`concierge.example.toml` is the annotated template. The essential part is the
list of repos a job may run in:

```toml
[[repos]]
name = "notes"
path = "~/notes"
github = "https://github.com/you/notes"
topics = "anything that isn't the app — research, errands, one-off questions"
default = true

[[repos]]
name = "app"
path = "~/projects/app"
github = "https://github.com/you/app"
topics = "the web app — bugs, deploys, the database, anything about its users"
```

This is a whitelist, not a hint: a `cwd` that isn't listed is refused at spawn
time. `topics` is prose because it's pasted into the concierge's system prompt,
so write it the way you'd explain the split to a new assistant.

Full reference: **[docs/configuration.md](docs/configuration.md)**.

## Commands

| | |
|---|---|
| `concierge jobs` | active jobs, one line each |
| `concierge status A3` | one job and its Remote Control link |
| `concierge spawn <title> <brief> <cwd> <chat>` | start a job (the concierge calls this) |
| `concierge notify <text> [--file f] [--status s]` | report back (a job calls this) |
| `concierge respawn A3` | restart a job the reboot interrupted, from its stored brief |
| `concierge kill A3` | close a job's window |
| `concierge ensure-up` | the whole supervision tick — what your scheduler runs |
| `concierge sessions` | which sessions have dropped off Remote Control |
| `concierge rc` | put them back |
| `concierge rc-server` | keep this machine in the Claude app's device list |

From chat, `/jobs`, `/status A3`, `/kill A3`, `respawn A3` and `/sessions` do
the same things.

## The trap that costs an afternoon

**Telegram allows exactly one `getUpdates` consumer per bot token**, and the
channel plugin starts a poller in *every* Claude Code session that loads it. If
you enable the plugin at user scope, the next ordinary `claude` session you open
at your desk will SIGTERM the concierge's poller and take the bot over, with no
error in either session. Messages get the 👀 reaction and no reply, or nothing
at all.

So the plugin must stay **disabled** at user scope. The concierge switches it on
for its own session only, with `--settings '{"enabledPlugins":{...}}'`. See
[docs/installation.md](docs/installation.md#the-telegram-plugin-must-stay-off-at-user-scope).

## Permissions

Both the concierge and its jobs run with `--permission-mode bypassPermissions`.
This is a deliberate trade and you should decide about it yourself.

The original design relayed each permission prompt to the phone. In practice one
ordinary job produced more prompts than a chat window can carry, and a prompt
nobody answers is a hung job. Bypass is what makes the thing usable from a
phone; it also means a job can do anything you can do.

Two things reduce the blast radius. Jobs may only run in the repos listed in
`concierge.toml`. And the job system prompt tells them to stop and ask, rather
than act, for anything outward or irreversible the brief didn't call for:
sending mail, writes to your accounts, force pushes, deletions.

If that's not a trade you want, `CONCIERGE_PERMISSION_MODE=auto` steps back to
classifier-gated prompts, which only interrupt on genuinely risky calls.

## Docs

- [Installation](docs/installation.md) — bot, plugin, scheduled tasks
- [Configuration](docs/configuration.md) — every key in `concierge.toml`
- [Architecture](docs/architecture.md) — what each module does and why
- [Remote Control](docs/remote-control.md) — the three things called "remote control"
- [Heartbeat](docs/heartbeat.md) — watching scheduled work from outside
- [Fast lanes](docs/fast-lanes.md) — minute-latency work on the supervision tick

## Status

This runs a single-user setup daily and has since August 2026. It is not
packaged, versioned or supported, and it assumes one person with one bot on one
machine. Issues and pull requests are welcome; treat it as something to read and
adapt rather than something to depend on.

## Licence

MIT. See [LICENSE](LICENSE).
