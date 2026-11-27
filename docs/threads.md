# Threads: the Concierge app

`<base_url>/app` (an installable page on the dashboard). One thread per specific
thing. Opening one shows the job's summary, any open decision as buttons, and a
key-facts card with the same rows everywhere (Next, Waiting on, Due, Money, Who,
Decided, Link); the message history is folded below. Alerts by urgency.
Spec: https://claude.ai/artifact/5rUz9w4esiwr1D6eddcmLT

## How a session talks to him

```
concierge post "<text>" [--urgency fyi|needs|urgent|critical] [--thread <id|title>] [--file f]
concierge ask "<question>" --option A --option B [--recommend A]
concierge summary "<what this is, where it stands, what's next>"
concierge facts "Money=£400" "Who=Drury's"   # rows: threads.FACT_KEYS
concierge close --note "<outcome>"
concierge rules                     # his global rules + delegations in force
concierge decided "<q>" "<choice>" --rule "<rule>"   # refused without a delegation
```

`post` and `ask` take `--image <path>`, repeated per photo: each is pushed and linked, and the app
draws any GitHub link to an image as a swipeable strip, full screen on tap. The repos are private,
so `/api/img` serves the file from the configured repo's checkout (image files inside it only).

A concierge job's thread comes from `CONCIERGE_JOB_ID`. Any other session sets
`CONCIERGE_THREAD_JOB` and `CONCIERGE_THREAD_PANE` (its tmux target) and names its
threads with `--thread`; see 13 Edward's `kelvin.sh`. `notify` still works and lands
in the job's thread; `--status done` closes it.

His messages and taps are typed into the session's pane with `tmuxctl.submit`, as
`[thread N: title] Kelvin says: ...` or `... Decision on "...": Kelvin chose "..."`.
The row records sent → picked up (left the box) → acted on (the job's next post),
or failed with the reason.

## Skills from the app

Typing `/` at the start of a message (or a New thread) lists the skills a job can run
(`/api/skills`, from `concierge/skills.py`: enabled plugins, live folder for a directory
marketplace, plus ~/.claude/skills), filtered as he types. His message reaches the job inside
`[thread N] Kelvin says: ...`, so Claude Code never sees it as a slash command; `skills.hint`
adds a line telling the job to run that skill with the Skill tool. Skills marked
`disable-model-invocation` are left out, since the Skill tool can't run them.

## Why decisions can't be answered by accident

A decision is a row in `state/threads.db`, answered only by his tap (`answer`) or by
`decided` under a live delegation. Jobs run with `--disallowedTools AskUserQuestion`:
that menu is what a typed Enter answered on 30 Oct (a £450 quote accepted).

## Undo

Every answered decision has an Undo. His tap waits `UNDO_SECONDS` (10s) before it is
typed to the job: Undo in that window just reopens the question. After it, Undo reopens
it and tells the job he took it back, to reverse what it can and wait for a new answer.
An answer whose worker died in the window is sent by the next threads tick.
Claude's own delegated decisions keep their Undo as before.
An answer tapped with no signal sits in sw.js's outbox: Undo (or Take back on the
outbox pill) deletes it there, so it never leaves the phone. Undo on a "Close it?"
answer reopens the question, and the thread if it closed it.

## Off his list: asked about, or Not now

A decision he asks about (Ask about this, Explain) leaves his list while the job
answers, so it doesn't sit there stale; the job is told to `revise N` if it still
needs his call, which puts it back with an alert. Not now sets it aside the same way
and tells the job to carry on without it. Either way the row is `answered_by='parked'`,
not answerable until it's back; Bring back in the app reopens it and tells the job.
Email and chat cards and "Close it?" never park. If the question never reached the
job, the decision stays on his list.

## Alerts

`raise_alarm` stores the alarm and posts its URL (`/api/alarms/<id>`) to ntfy.sh on
`glance-` + sha256("glance-alarm:" + account)[:20]. Glance fetches the JSON over the
tailnet and shows it: Needs me a notification, Urgent loud through Do Not Disturb,
Critical a full-screen page. Nothing but the URL leaves the tailnet, and an id glance
can't fetch never fires. `mirror_to_telegram` also copies every alert to Telegram.

A thread turned down never alerts above FYI. Modes (Settings) hold back what they
don't let through; `ensure-up`'s threads tick releases it as one alert when the mode
ends, and asks about threads quiet for 3 days. If the dashboard has to be started two
ticks running, the notifications chat on Telegram is told the app is down.
