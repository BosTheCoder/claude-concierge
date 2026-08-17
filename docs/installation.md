# Installation

Four things to set up: the code, a Telegram bot, the channel plugin, and
something that calls `ensure-up` every five minutes.

## 1. The code

```bash
git clone https://github.com/BosTheCoder/claude-concierge
cd claude-concierge
uv sync
cp concierge.example.toml concierge.toml
$EDITOR concierge.toml
```

At minimum, `concierge.toml` needs one `[[repos]]` entry pointing at a real
directory. See [configuration.md](configuration.md).

```bash
just test          # 238 tests, no network, no tmux
```

`bin/concierge` finds its own checkout, so you can symlink it onto your `PATH`
or call it by absolute path — both work.

## 2. A Telegram bot

Message [@BotFather](https://t.me/botfather), send `/newbot`, and keep the
token. Then start a chat with your new bot and send it anything, so it has a
chat to reply to.

Nothing in this repo stores the token. It lives in
`~/.claude/channels/telegram/.env`, written by the plugin, and is read from
there at send time.

## 3. The channel plugin

The concierge receives messages through Claude Code's Telegram channel plugin,
`telegram@claude-plugins-official`. Install it and configure the token:

```
/plugin install telegram@claude-plugins-official
/telegram:configure <your-bot-token>
```

### The Telegram plugin must stay off at user scope

This is the one step that fails silently, so it's worth understanding rather
than just copying.

Telegram allows **exactly one `getUpdates` consumer per bot token**. The plugin
starts a poller in *every* Claude Code session that loads it, and the new poller
SIGTERMs whoever currently holds `~/.claude/channels/telegram/bot.pid`.

So if the plugin is enabled at user scope, the next ordinary `claude` session
you open at your desk quietly steals the bot from the concierge. Neither session
reports an error. From the phone it looks like the concierge has stopped caring:
messages get the 👀 reaction and no reply, or no reaction at all.

Make sure it is **disabled** in `~/.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "telegram@claude-plugins-official": false
  }
}
```

The concierge turns it on for its own session only, by passing
`--settings '{"enabledPlugins":{"telegram@claude-plugins-official":true}}'` at
startup. That's `CONCIERGE_SETTINGS` in `concierge/config.py`.

If you script your plugin installs, force it back off after installing —
installers tend to enable what they install.

## 4. Start it

```bash
just up          # == concierge ensure-up
tmux attach -t concierge
```

`ensure-up` is idempotent and silent when the concierge is already alive, so
you can run it as often as you like. Send your bot a message; you should get a
👀 reaction and a reply.

## 5. Keep it up

A logon trigger alone covers almost nothing. What actually breaks a
long-running tmux session is reboots, crashes, `wsl --shutdown`, Claude Code
updates, and network outages long enough to time Remote Control out. The
five-minute watchdog is the real guarantee; the logon trigger just makes
startup prompt.

### Windows / WSL

Use Task Scheduler, not systemd inside WSL. After `wsl --shutdown` there is no
WSL left for a systemd timer to run in, so the trigger has to come from the
Windows side.

Two tasks, one at logon and one every five minutes, both running:

```powershell
Start-Process wsl `
  -ArgumentList '-d','Ubuntu-24.04','-u','<you>','--',`
    '/home/<you>/projects/claude-concierge/bin/concierge','ensure-up' `
  -WindowStyle Hidden -WorkingDirectory 'C:\Windows'
```

Two details that matter:

- **Launch through `Start-Process`, don't make `claude` the task action.**
  `Start-Process` returns immediately and the detached tmux session it leaves
  behind is not bound by Task Scheduler's execution time limit. Make the
  session itself the action and it gets killed after an hour.
- **Run the timed task hidden.** Otherwise a console window flashes on the
  desktop every five minutes, forever.

### systemd (native Linux)

```ini
# ~/.config/systemd/user/concierge.service
[Unit]
Description=claude-concierge supervision tick

[Service]
Type=oneshot
ExecStart=%h/projects/claude-concierge/bin/concierge ensure-up
```

```ini
# ~/.config/systemd/user/concierge.timer
[Unit]
Description=Run the concierge tick every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now concierge.timer
loginctl enable-linger "$USER"     # so it runs when you aren't logged in
```

### cron

```cron
*/5 * * * * /home/you/projects/claude-concierge/bin/concierge ensure-up
@reboot     /home/you/projects/claude-concierge/bin/concierge ensure-up
```

## 6. Optional: the Remote Control server

Separate from everything above, and only if you want to start ad-hoc sessions
on this machine from the Claude app. It gets its own pair of tasks running
`bin/concierge rc-server` — deliberately not part of `ensure-up`, so neither
service can take the other down. See [remote-control.md](remote-control.md).

## Environment variables

| | |
|---|---|
| `CONCIERGE_CONFIG` | Path to a config file other than `./concierge.toml` |
| `CONCIERGE_PERMISSION_MODE` | Defaults to `bypassPermissions`; `auto` gives classifier-gated prompts |
| `RC_SERVER_NAME` | Overrides the machine name shown in the Claude app |
| `CONCIERGE_JOB_ID` | Set by the spawner on each job. Don't set it yourself |

## Things that stop it starting

`ensure-up` refuses to start the concierge if any of these are set, and tells
you which, because they break Remote Control or Channels and the resulting
failure is otherwise invisible:

`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`,
`DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_GROWTHBOOK`.

Note that `bin/concierge` deliberately sets its own `PATH`. Scheduled tasks run
no login shell, so `uv`, `claude` and `bun` are otherwise not on it. Everything
the concierge starts inherits that environment — tmux, and through it `claude`
and the plugin's `bun server.ts` — so it's the one place worth fixing. Add
whatever your tools need there.
