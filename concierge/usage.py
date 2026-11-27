"""His Claude plan usage: the 5-hour session and the week, as /usage shows them, so he can pace himself.

Asks the same endpoint Claude Code's /usage reads, with the login Claude Code keeps fresh in
~/.claude/.credentials.json. The home list polls every 10s, so the answer is cached for a minute.
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CREDS = Path.home() / ".claude" / ".credentials.json"
URL = "https://api.anthropic.com/api/oauth/usage"
WINDOWS = {"five_hour": ("Session", timedelta(hours=5)), "seven_day": ("Week", timedelta(days=7))}
_cache: tuple[float, dict | None] = (0.0, None)


def shape(raw: dict, now: datetime) -> dict:
    """The two bars: percent used, when it resets, and how much of the window has gone,
    so 48% used with 70% of the week gone reads as room to spare."""
    bars = []
    for key, (name, span) in WINDOWS.items():
        w = raw.get(key)
        if not w or w.get("utilization") is None:
            continue
        resets = datetime.fromisoformat(w["resets_at"])
        gone = 1 - (resets - now) / span
        bars.append({"name": name, "pct": round(w["utilization"]), "resets": w["resets_at"],
                     "elapsed": max(0, min(100, round(gone * 100)))})
    return {"bars": bars}


def get() -> dict | None:
    global _cache
    if time.time() - _cache[0] < 60:
        return _cache[1]
    try:
        token = json.loads(CREDS.read_text())["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {token}",
                                                   "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code"})
        out = shape(json.loads(urllib.request.urlopen(req, timeout=5).read()), datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - expired login, offline: the card says so instead of vanishing
        out = {"bars": [], "error": f"{type(exc).__name__}: {exc}"[:200]}
    _cache = (time.time(), out)
    return out


if __name__ == "__main__":
    now = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    s = shape({"five_hour": {"utilization": 7.0, "resets_at": "2026-11-27T23:00:00+00:00"},
               "seven_day": {"utilization": 48.4, "resets_at": "2026-12-01T06:00:00+00:00"},
               "seven_day_opus": None}, now)
    assert [b["name"] for b in s["bars"]] == ["Session", "Week"], s
    assert s["bars"][0]["elapsed"] == 0 and s["bars"][1] == {**s["bars"][1], "pct": 48, "elapsed": 50}, s
    print(json.dumps(get(), indent=1))
