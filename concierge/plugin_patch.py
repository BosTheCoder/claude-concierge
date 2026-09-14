"""Keep the Telegram plugin passing reply context through, across its updates.

The channel plugin is Anthropic's (`telegram@claude-plugins-official`). Its
inbound handler drops `reply_to_message`, so a reply reaches the concierge
looking like a fresh message. See docs/message-log.md.

Why a patch and not a fork: a fork loaded with `--plugin-dir` is no longer on
Claude Code's approved channels allowlist, and loading one needs
`--dangerously-load-development-channels`, which an unattended start from a
scheduled task should not depend on. So the official plugin stays, and this
re-applies a small patch whenever the installed copy lacks it. An update lands
in a new versioned cache directory without the patch; the next tick fixes it.

If upstream changes enough that the patch no longer applies, that is said once
in the notifications chat, and replies arrive without context until the patch
is refreshed. Nothing else breaks: the plugin itself is left untouched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from concierge import config

MARKER = "concierge-patch: reply-context"
PATCH = config.REPO / "patches" / "telegram-reply-context.patch"
INSTALLED = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
ALERTED = config.REPO / "state" / "plugin_patch_alerted"


def install_path(installed: Path = INSTALLED) -> Path | None:
    try:
        data = json.loads(installed.read_text())
        rows = data.get("plugins", data)[config.TELEGRAM_PLUGIN]
        return Path(rows[0]["installPath"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def ensure(root: Path | None = None, *, notifier=None, runner=subprocess.run) -> str:
    root = root or install_path()
    server = root / "server.ts" if root else None
    if server is None or not server.exists():
        return "plugin-patch: telegram plugin not found"
    if MARKER in server.read_text():
        return "plugin-patch: ok"

    argv = [
        "patch", "-p1", "--forward", "--batch", "--no-backup-if-mismatch",
        "-d", str(root), "-i", str(PATCH),
    ]
    dry = runner([*argv, "--dry-run"], capture_output=True, text=True)
    if dry.returncode == 0:
        real = runner(argv, capture_output=True, text=True)
        if real.returncode == 0:
            return f"plugin-patch: applied to {root}; live when the concierge next starts"
        dry = real

    # Once per plugin version — this runs every five minutes.
    if not (ALERTED.exists() and ALERTED.read_text().strip() == str(root)):
        if notifier is None:
            from concierge import supervisor

            notifier = supervisor._default_notifier
        notifier(
            f"telegram plugin {root.name} no longer takes the reply-context patch, "
            "so replies reach the concierge without what they reply to. "
            f"Refresh patches/telegram-reply-context.patch. patch said: "
            f"{(dry.stdout + dry.stderr).strip()[:300]}"
        )
        ALERTED.parent.mkdir(parents=True, exist_ok=True)
        ALERTED.write_text(str(root))
    return f"plugin-patch: FAILED on {root}"
