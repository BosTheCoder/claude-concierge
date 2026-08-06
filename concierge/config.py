import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STATE_FILE = REPO / "state" / "jobs.json"
# Where the supervisor's own alerts go when the registry is empty — a fresh
# install, or the morning after the 7-day prune cleared it.
LAST_CHAT_FILE = REPO / "state" / "last_chat"
# Poll/alert timestamps for the scheduled-run heartbeat (heartbeat.py).
HEARTBEAT_FILE = REPO / "state" / "heartbeat.json"
PROMPTS_DIR = REPO / "prompts"

TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"

TASKS_REPO = Path.home() / "projects" / "personal" / "tasks"
NPM_REPO = Path.home() / "projects" / "personal" / "nyakundi-property-management"

TMUX_SESSION = "concierge"

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
