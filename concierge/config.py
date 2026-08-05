from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

STATE_FILE = REPO / "state" / "jobs.json"
PROMPTS_DIR = REPO / "prompts"

TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"

TASKS_REPO = Path.home() / "projects" / "personal" / "tasks"
NPM_REPO = Path.home() / "projects" / "personal" / "nyakundi-property-management"

TMUX_SESSION = "concierge"

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
