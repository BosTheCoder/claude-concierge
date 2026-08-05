"""Outbound Telegram. Reuses the token the channel plugin already stores."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from concierge import config

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4096


def load_token(env_path: Path | None = None) -> str:
    p = env_path or config.TELEGRAM_ENV
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError(
        f"no TELEGRAM_BOT_TOKEN in {p} — run /telegram:configure <token> in Claude Code"
    )


def chunk(text: str, limit: int = LIMIT) -> list[str]:
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    parts.append(rest)
    return parts


def _urlopen(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _post(url: str, payload: dict, *, opener=None, sleeper=None) -> dict:
    """Telegram answers HTTP 200 with {"ok": false} on a rejected send."""
    opener = opener or _urlopen
    sleeper = sleeper or time.sleep

    try:
        data = opener(url, payload)
    except OSError:  # urllib.error.URLError and friends are all OSError
        sleeper(2)
        data = opener(url, payload)

    if not data.get("ok"):
        raise RuntimeError(
            f"telegram sendMessage failed: {data.get('description') or data}"
        )
    return data


def send(
    chat_id: str,
    text: str,
    reply_to: int | None = None,
    *,
    prefix: str = "",
    token: str | None = None,
    poster=None,
) -> list[dict]:
    token = token or load_token()
    poster = poster or _post
    url = API.format(token=token)

    results = []
    # Every chunk carries the prefix, so a split message stays identifiable.
    for i, part in enumerate(chunk(text, LIMIT - len(prefix))):
        payload: dict = {"chat_id": chat_id, "text": prefix + part}
        # Thread only the first chunk, matching the plugin's own 'first' mode.
        if reply_to is not None and i == 0:
            payload["reply_parameters"] = {"message_id": reply_to}
        results.append(poster(url, payload))
    return results
