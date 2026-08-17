# Configuration

Everything that differs between one installation and the next lives in
`concierge.toml` at the repo root. It is gitignored.
`concierge.example.toml` is the annotated template.

If `concierge.toml` is absent the example is loaded instead, so a fresh clone
still starts and the tests still run. But the example's paths don't exist, so
spawning refuses everything until you write a real one. Point
`CONCIERGE_CONFIG` at a different file to override both.

A missing config file is tolerated. A malformed one is not: broken TOML, a repo
with no `path`, a lane with an empty `command` and a watch with no `url` all
fail loudly and name the offending entry.

There are no secrets in this file. The Telegram bot token is read at runtime
from `~/.claude/channels/telegram/.env`.

---

## `[[repos]]`

The repos a job is allowed to run in. At least one is required.

```toml
[[repos]]
name = "notes"
path = "~/notes"
github = "https://github.com/you/notes"
topics = "anything that isn't the app — research, errands, one-off questions"
default = true
```

| key | required | |
|---|---|---|
| `name` | yes | Label, used in errors |
| `path` | yes | `~` is expanded |
| `github` | no | Used to turn a report file into a link |
| `topics` | no | Prose, pasted into the concierge's system prompt |
| `default` | no | Where the concierge runs and where unclear work goes |

**This is a whitelist, not a hint.** A `cwd` that isn't listed is refused at
spawn time. That check earns its place: tmux forking a shell tells you nothing
about whether the command survived, so a bad path makes `cd` fail, `&&`
short-circuit, and the window close instantly: a job that reports as started
and does nothing.

**`topics` is prose because routing is a judgement call.** It's pasted verbatim
into the concierge's system prompt, so the model routes by meaning. Write it the
way you'd explain the split to a new assistant. A keyword list would only be a
worse version of the same guess.

**`github` is what makes reports readable on a phone.** A job that writes more
than six lines commits the file, pushes, and calls
`concierge notify --file report.md`; the CLI turns that into a blob URL. Without
a `github` entry, the notify still goes out with the file path instead. A link
that can't be built must never cost you the message.

Mark exactly one repo `default = true`. It's where the concierge session itself
runs, where sessions started from the phone open, and where a job goes when the
topic doesn't clearly belong anywhere else. With none marked, the first repo
wins.

---

## `[tmux]`

```toml
[tmux]
session = "concierge"
```

Window 0 of this session is the concierge. Every job gets its own window, named
after its job id.

---

## `[remote_control]`

Optional. Configures `claude remote-control` in **server mode**: the thing that
puts this machine in the Claude app's device list. Not the same as the
concierge's own Remote Control link. See [remote-control.md](remote-control.md).

```toml
[remote_control]
session = "rc"
window = "0"
# name = "my-desktop"      # defaults to the hostname
spawn_mode = "same-dir"
```

`spawn_mode` is `same-dir` (phone-started sessions open in the default repo) or
`worktree` (each gets its own git worktree). Worktree is right for a codebase
and pointless for a notes repo.

---

## `[[lanes]]`

Optional, repeatable. Work that has to happen within minutes rather than at the
next cron boundary. See [fast-lanes.md](fast-lanes.md).

```toml
[[lanes]]
name = "app outbound"
command = ["~/projects/app/scripts/flush.sh", "--quiet"]
```

The first element is the program and gets `~` expanded; the rest are passed
through untouched. A lane whose program doesn't exist on this machine is skipped
silently, so it's safe to declare lanes for projects you haven't checked out
here.

---

## `[[watches]]`

Optional, repeatable. Scheduled work *elsewhere* that should keep happening,
watched from outside. See [heartbeat.md](heartbeat.md).

```toml
[[watches]]
name = "app nightly crawl"
url = "http://localhost:8000/api/runs?kind=source&limit=10"
kind = "source"
max_age_hours = 30
expected = "daily at 04:00"
```

`url` must return `{"data": [run, ...]}` newest-first, each run carrying `kind`,
`ok` and `finished_at`.

Give `max_age_hours` slack for the machine being off. A daily 04:00 job wants
roughly 30h: enough to survive an overnight shutdown that delays the run until
morning, tight enough to still fire within a day of a real stoppage. A machine
switched off for a whole day *should* alert. That's a real gap.

`expected` is quoted back to you in the alert, so write it as you'd want to read
it at 7am.
