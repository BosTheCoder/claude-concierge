import json
import os
from pathlib import Path

from concierge import settings as settings_mod

SETTINGS = settings_mod.current()

REPO = settings_mod.REPO_ROOT

STATE_FILE = REPO / "state" / "jobs.json"
# Where the supervisor's own alerts go when the registry is empty — a fresh
# install, or the morning after the 7-day prune cleared it.
LAST_CHAT_FILE = REPO / "state" / "last_chat"
# Poll/alert timestamps for the scheduled-run heartbeat (heartbeat.py).
HEARTBEAT_FILE = REPO / "state" / "heartbeat.json"
# Consecutive-strike counter for the Remote Control server (rcserver.py).
RC_SERVER_FILE = REPO / "state" / "rcserver.json"
# Copy of the Remote Control server's pane output. The server exits on its own
# every few hours; tmux destroys a single-window session when its process dies,
# so the window closes and takes the reason with it. This is the only place that
# survives the exit.
RC_SERVER_LOG = REPO / "state" / "rcserver.log"
PROMPTS_DIR = settings_mod.PROMPTS_DIR

TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"

# Which repos jobs may run in, and where the concierge itself sits. Both come
# from concierge.toml — see settings.py.
REPOS = SETTINGS.repos

TMUX_SESSION = SETTINGS.tmux_session

# --- the Remote Control server (rcserver.py) --------------------------------
# A different thing from the concierge session's `--remote-control concierge`.
# That flag publishes one already-running session; this is `claude
# remote-control` in server mode, which is what puts the machine itself in the
# Claude app's device list so a session can be *started* from the phone.
RC_SERVER_TMUX_SESSION = SETTINGS.remote_control.session
RC_SERVER_WINDOW = SETTINGS.remote_control.window
RC_SERVER_NAME = os.environ.get("RC_SERVER_NAME") or SETTINGS.remote_control.name
# On-demand sessions from the phone all open in the default repo. Worktree mode
# is the alternative, and is the wrong shape when that repo is a notes or task
# repo rather than a codebase — a worktree of a notes repo buys nothing, and
# the enclosing directory is usually not its own repo at all.
RC_SERVER_SPAWN_MODE = SETTINGS.remote_control.spawn_mode

# Both sessions run in bypass. The design assumed the permission relay would
# carry each prompt to the phone; in practice one ordinary job produced enough
# prompts to make the chat unusable, and a prompt nobody answers is a hung job.
# `auto` (classifier-gated, prompts only on genuinely risky calls) is one env
# var away: CONCIERGE_PERMISSION_MODE=auto.
PERMISSION_MODE = os.environ.get("CONCIERGE_PERMISSION_MODE", "bypassPermissions")

# Telegram allows exactly one getUpdates consumer per bot token, and the channel
# plugin starts a poller in *every* session that loads it — so any ordinary
# `claude` session would SIGTERM the concierge's poller and silently take over
# the bot. The plugin must therefore be disabled at user scope and switched on
# here, for this session only. See docs/installation.md.
TELEGRAM_PLUGIN = "telegram@claude-plugins-official"
CONCIERGE_SETTINGS = json.dumps(
    {"enabledPlugins": {TELEGRAM_PLUGIN: True}}, separators=(",", ":")
)

ACTIVE_STATUSES = frozenset({"running", "waiting"})

# No I, L or O — they misread as 1 and 0 on a phone.
ID_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"
ID_DIGITS = "0123456789"

BLOCKING_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
)
