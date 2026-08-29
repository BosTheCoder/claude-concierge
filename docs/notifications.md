# Two channels, not one

Everything the system says arrives in one Telegram DM: a job answering a
question, a job handing over a finished report, "the nightly sync has not run in
30 hours", "concierge failed to start", and a Healthchecks alert for every
scheduled task that missed its window.

Bosire's description, 2026-08-29:

> a lot of them, it's like spam. It feels like spam, like "oh, team session,
> blahdy blahdy blah" or "job didn't run" and I feel like, no, it's too much.
> Like maybe I need a separate channel for that. […] at work, I might have a
> main channel for one thing and then other channels, let's say it was like
> Slack, for like notifications and stuff like that.

That is the standard answer and it is the right one. The split is **routing,
never filtering** — nothing is dropped, muted or rate-limited to make the main
chat quieter. Everything still gets sent; the machine's own noise just stops
sitting in the same thread as the work.

## What goes where

**Conversation** — the DM. Anything about Bosire's actual work.

| Source | Example |
|---|---|
| `cli.notify` from a job | `[A3] done — 27 books, 4 duplicates moved` |
| `cli.notify` from a job asking something | `[A3] dedupe by ISBN or by title+author?` |
| the concierge session's own replies | `on it — spawning a job` |
| the reaper's nudge for a blocked job | `[T7] still waiting on you since…` |

The reaper's nudge is deliberately in this list. It is a job re-asking a
question Bosire has not answered, which is exactly what the split exists to keep
visible.

**Notifications** — the second chat. Anything the machine says about itself,
unprompted.

| Source | Example |
|---|---|
| `heartbeat` | `job-tracker sourcing: no successful run in 34h` |
| `supervisor` | `concierge failed to start: …` |
| `supervisor` | `concierge NOT started — these env vars break Remote Control…` |
| `supervisor` | `[R0] was mid-flight when the machine restarted` |
| Healthchecks | `🔴 bootstrap-sync is DOWN` |

## How it is wired

Inside the concierge, every one of those ops messages already funnelled through
a single function — `supervisor._default_notifier`, which `heartbeat.py`
delegates straight into. So the routing is one decision in one place:

```python
def ops_destination(state_path=None):
    return config.NOTIFICATIONS_CHAT_ID or alert_destination(state_path)
```

Unset, that is byte-for-byte the old behaviour. Everything falls back to the
conversation chat, which is what a fresh install and this machine both do until
the group exists.

Healthchecks is outside the concierge and is re-pointed separately — see below.

## Setting it up

The bot cannot create a group or invite itself, so these four steps are
Bosire's:

1. **Telegram → New Group.** Call it something like `Bos ops`. Add the same bot
   the concierge uses as a member.
2. **Send one message in the group** — anything. Telegram will not report a chat
   that has never been spoken in.
3. **Read the id:**

   ```bash
   TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/.claude/channels/telegram/.env | cut -d= -f2 | tr -d "\"'")
   curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" \
     | python3 -c 'import json,sys; [print(u.get("message",{}).get("chat")) for u in json.load(sys.stdin)["result"]]'
   ```

   A group id is negative. Keep the minus sign.

4. **Put it in `concierge.toml`:**

   ```toml
   [telegram]
   notifications_chat_id = "-1001234567890"
   ```

   The next `ensure-up` tick picks it up; nothing needs restarting.

Then, for the Healthchecks alerts — the `job didn't run` messages, which are the
larger share of the noise:

```bash
cd ~/projects/personal/healthchecks
docker compose -f compose.yml -f compose.local.yml exec -T app \
    python - --token "$TELEGRAM_BOT_TOKEN" --chat-id "-1001234567890" \
    < scripts/telegram_channel.py
```

That rewrites the one outbound webhook every check shares. No inbound traffic is
involved, so nothing about the bot's `getUpdates` consumer changes.

## The group is outbound-only, and that is fine

The Telegram channel plugin gates inbound messages on `access.json`, which the
notifications group is not in. So the bot will post into the group and ignore
anything said there.

That is the right default for a notifications channel, and it costs nothing:
every command still works in the main DM. `[R0] was mid-flight…` appearing in
the ops group does not strand the `respawn R0` reply — typing that in the
conversation works exactly as it did.

Granting the group inbound access is a separate, deliberate step
(`/telegram:access`) and is Bosire's to take if he ever wants it.
