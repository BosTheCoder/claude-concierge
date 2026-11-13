# Threads: the Concierge app

`<base_url>/app` (an installable page on the dashboard). One thread per specific
thing, a key-facts card on top, decisions as buttons, and alerts by urgency.
Spec: https://claude.ai/artifact/5rUz9w4esiwr1D6eddcmLT

## How a session talks to him

```
concierge post "<text>" [--urgency fyi|needs|urgent|critical] [--thread <id|title>] [--file f]
concierge ask "<question>" --option A --option B [--recommend A]
concierge facts "Money=£400" "Who=Drury's"
concierge close --note "<outcome>"
concierge rules                     # his global rules + delegations in force
concierge decided "<q>" "<choice>" --rule "<rule>"   # refused without a delegation
```

A concierge job's thread comes from `CONCIERGE_JOB_ID`. Any other session sets
`CONCIERGE_THREAD_JOB` and `CONCIERGE_THREAD_PANE` (its tmux target) and names its
threads with `--thread`; see 13 Edward's `kelvin.sh`. `notify` still works and lands
in the job's thread; `--status done` closes it.

His messages and taps are typed into the session's pane with `tmuxctl.submit`, as
`[thread N: title] Kelvin says: ...` or `... Decision on "...": Kelvin chose "..."`.
The row records sent → picked up (left the box) → acted on (the job's next post),
or failed with the reason.

## Why decisions can't be answered by accident

A decision is a row in `state/threads.db`, answered only by his tap (`answer`) or by
`decided` under a live delegation. Jobs run with `--disallowedTools AskUserQuestion`:
that menu is what a typed Enter answered on 30 Oct (a £450 quote accepted).

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
