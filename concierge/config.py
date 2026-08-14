import json
import os
import socket
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STATE_FILE = REPO / "state" / "jobs.json"
# Where the supervisor's own alerts go when the registry is empty — a fresh
# install, or the morning after the 7-day prune cleared it.
LAST_CHAT_FILE = REPO / "state" / "last_chat"
# Poll/alert timestamps for the scheduled-run heartbeat (heartbeat.py).
HEARTBEAT_FILE = REPO / "state" / "heartbeat.json"
# Consecutive-strike counter for the Remote Control server (rcserver.py).
RC_SERVER_FILE = REPO / "state" / "rcserver.json"
PROMPTS_DIR = REPO / "prompts"

TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"

TASKS_REPO = Path.home() / "projects" / "personal" / "tasks"
NPM_REPO = Path.home() / "projects" / "personal" / "nyakundi-property-management"

TMUX_SESSION = "concierge"

# --- the Remote Control server (rcserver.py) --------------------------------
# A different thing from the concierge session's `--remote-control concierge`.
# That flag publishes one already-running session; this is `claude
# remote-control` in server mode, which is what puts the machine itself in the
# Claude app's device list so a session can be *started* from the phone.
RC_SERVER_TMUX_SESSION = "rc"
RC_SERVER_WINDOW = "0"
# What the machine is called in the app. The CLI would derive the same thing
# from the hostname; naming it here means the tests can assert on it.
RC_SERVER_NAME = os.environ.get("RC_SERVER_NAME", socket.gethostname())
# same-dir, not worktree: on-demand sessions all open in TASKS_REPO. Worktree
# mode would be wrong here even though tasks is a git repo — worktrees of a
# notes repo are pointless, and the obvious alternative root
# (~/projects/personal) is not its own repo at all: bootstrap made $HOME the
# home-wsl repo, so a worktree there would fork the whole home directory.
RC_SERVER_SPAWN_MODE = "same-dir"

# Both sessions run in bypass. The design assumed the permission relay would
# carry each prompt to the phone; in practice one ordinary job produced enough
# prompts to make the chat unusable, and a prompt nobody answers is a hung job.
# `auto` (classifier-gated, prompts only on genuinely risky calls) is one env
# var away: CONCIERGE_PERMISSION_MODE=auto.
PERMISSION_MODE = os.environ.get("CONCIERGE_PERMISSION_MODE", "bypassPermissions")

# Telegram allows exactly one getUpdates consumer per bot token, and the channel
# plugin starts a poller in *every* session that loads it — so any ordinary
# `claude` session would SIGTERM the concierge's poller and silently take over
# the bot. The plugin is therefore disabled at user scope (see
# ~/.claude/setup-plugins.sh) and switched on here, for this session only.
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
