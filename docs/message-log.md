# The message log

Telegram's Bot API has no history. A bot can't read back what it sent, and a
reply arrives carrying only the id of the message it answers. With several
senders sharing one bot (jobs, the concierge, sessions Bosire starts himself,
the machine's own alerts), that id means nothing unless something wrote down,
at the moment of sending, what the message said and who sent it.

## What went wrong, 2026-09-14

A session he had started by hand researched an ethernet cable and sent him a
shortlist through the bot. It had no job id, so it couldn't use `notify`, and
called `telegram.send(registry.last_chat(), …)` directly. At 09:14 he replied
to that message: "Is it not better to get 40m just to be safe". Two problems
stacked up:

1. The channel plugin drops `reply_to_message` from the update, so the
   concierge got a bare message with no sign it was a reply.
2. Even with the id, nothing recorded what the bot had sent or which session
   sent it.

The concierge searched its jobs and the recent folders for "40m", found
nothing, and asked him. The folder had been there the whole time.

## How it works now

One append-only file, `~/.claude/channels/telegram/messages.jsonl` (it follows
`TELEGRAM_STATE_DIR`). There are two writers:

| Writer | Logs |
|---|---|
| `telegram.send` | every Python send: `notify`, supervisor, heartbeat, reaper, and any session importing it. Records `CONCIERGE_JOB_ID`, `CLAUDE_CODE_SESSION_ID`, the Remote Control session id and the cwd, all read from the environment, so the sender doesn't have to declare anything. |
| the plugin, patched | every inbound message with its `reply_to` id and quoted text; every `reply` the concierge sends |

The patched plugin also puts `reply_to_message_id` and `reply_to_text` on the
`<channel>` tag.

`concierge context <id>` turns an id into the message, its sender, and the
task folder that sender worked in. For a job, that's the registry row. For a
hand-started session, it's the dated folder the session last wrote files to,
read from its transcript. It also says whether the session is still running
and whether it's in a tmux pane. `concierge send <uuid>` types into that pane
with the same guarantees `send` gives a job.

`concierge recent` is the fallback for a follow-up typed fresh instead of sent
as a reply. It shows recent messages with their senders and folders, recent
jobs, and recently touched task folders.

`notify` with no job id now sends to the last chat, so a hand-started session
no longer has to hand-roll a send.

## The plugin patch

`patches/telegram-reply-context.patch`, re-applied by `ensure-up` whenever the
installed plugin is missing it (`plugin_patch.py`). A fork loaded with
`--plugin-dir` would be simpler to reason about, but it drops off Claude Code's
approved channel allowlist and needs `--dangerously-load-development-channels`.
If an upstream update stops the patch applying, the notifications chat hears
about it once per version. Replies then arrive without context, but everything
else keeps working.

The patched plugin only takes effect when the concierge restarts.

## What it can't do

- Messages sent before 2026-09-14 aren't in the log. For those, the prompt
  falls back to grepping task folders and transcripts.
- A sender that bypasses both writers (`curl` to the Bot API, some other
  program with the token) isn't logged.
- A hand-started session running outside tmux can't be typed into. The
  concierge spawns a follow-up job on its folder instead.
