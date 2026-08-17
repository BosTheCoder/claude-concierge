"""Did the scheduled work actually happen?

This exists because of a specific, boring disaster. A scheduled service had a
`runs` table, a "last automated run" stats card, a runs page with live log
tails, and a per-run summary email — and it was stone dead for thirteen days
without producing a single signal, because every one of those lives INSIDE the
thing that stopped running. The failure mode was never "a run went wrong". It
was "no run happened", and nothing watched for absence.

This watches for absence, from outside.

It rides `ensure-up` rather than taking a scheduled task of its own. The
watchdog calls `ensure-up` every 5 minutes and is the most reliably executed
thing on the machine — it survives reboots, crashes, and Claude Code updates.
A separate task for the watchdog could rot silently in exactly the way it
exists to detect, and then who watches the watcher. Riding a proven executor
closes that loop.

Cheap by construction: `ensure-up` fires 288 times a day, so the poll is rate
limited to hourly and each alert has a cooldown, making a real outage nag
twice a day instead of 288 times.

Watches are declared in `concierge.toml`; with none declared this is a no-op.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from concierge import config

HTTP_TIMEOUT = 10

# ensure-up runs every 5 minutes; there is no point asking a daily job whether
# it ran 288 times a day.
POLL_INTERVAL_HOURS = 1

# How long a given watch stays quiet after it alerts. Long enough that a
# multi-day outage nags rather than floods, short enough to stay visible.
ALERT_COOLDOWN_HOURS = 12


@dataclass(frozen=True)
class Problem:
    """What's wrong, and whether it's worth believing on first sight.

    `provisional` problems have to be seen twice, an hour apart, before they
    alert. Only "can't reach the API" is provisional: containers are not up
    the instant the machine is, and this box has a known Docker boot race, so
    a single refused connection at 07:00 says nothing. A missing run does not
    need confirming — a 30-hour hole in the schedule will not heal in an hour.
    """

    text: str
    provisional: bool = False


@dataclass(frozen=True)
class Watch:
    """One scheduled thing that is supposed to keep happening.

    `url` must return `{"data": [run, ...]}` newest-first, each run carrying
    `kind`, `ok` and `finished_at`. Any service that grows a runs endpoint of
    that shape can be watched by adding five lines to `concierge.toml`.

    Pick `max_age_hours` with slack for the machine being off. A daily 04:00
    job wants roughly 30h: enough to survive an overnight shutdown that delays
    the run until morning, tight enough to still fire within a day of a
    genuine stoppage. A machine switched off for a whole day SHOULD alert —
    that is a real gap.
    """

    name: str
    url: str
    kind: str
    max_age_hours: float
    expected: str


WATCHES: tuple[Watch, ...] = tuple(
    Watch(
        name=spec.name,
        url=spec.url,
        kind=spec.kind,
        max_age_hours=spec.max_age_hours,
        expected=spec.expected,
    )
    for spec in config.SETTINGS.watches
)


# --- state -----------------------------------------------------------------


def _path(state_path: Path | None) -> Path:
    return state_path or config.HEARTBEAT_FILE


def load_state(state_path: Path | None = None) -> dict:
    p = _path(state_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        # A truncated state file must not wedge the watchdog permanently.
        return {}


def save_state(state: dict, state_path: Path | None = None) -> None:
    p = _path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_due(state: dict, now: datetime) -> bool:
    last = _parse(state.get("last_poll"))
    return last is None or now - last >= timedelta(hours=POLL_INTERVAL_HOURS)


def in_cooldown(state: dict, name: str, now: datetime) -> bool:
    last = _parse((state.get("alerts") or {}).get(name))
    return last is not None and now - last < timedelta(hours=ALERT_COOLDOWN_HOURS)


# --- checking ---------------------------------------------------------------


def fetch_runs(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read()).get("data") or []


def _age_hours(ts: str | None, now: datetime) -> float | None:
    when = _parse(ts)
    return None if when is None else (now - when).total_seconds() / 3600


def _stamp(ts: str | None) -> str:
    when = _parse(ts)
    return when.strftime("%a %d %b %H:%MZ") if when else "unknown"


def check(watch: Watch, now: datetime, fetch=fetch_runs) -> Problem | None:
    """One watch. Returns the problem, or None when all is well.

    Three distinct failures, each worth different words: the service is
    unreachable, it ran and failed, or it silently never ran at all. The last
    one is the one that cost 13 days.
    """
    try:
        runs = fetch(watch.url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Problem(
            f"{watch.name}: can't reach the API ({exc}). "
            f"The app may be down — expected {watch.expected}.",
            provisional=True,
        )

    of_kind = [r for r in runs if r.get("kind") == watch.kind]

    # A run that reports its own failure. Say so even if an older successful
    # run is still inside the window: the newest word is the true state.
    if of_kind and not of_kind[0].get("ok", True):
        newest = of_kind[0]
        summary = (newest.get("summary") or "").strip().replace("\n", " ")
        if len(summary) > 200:
            summary = summary[:200] + "…"
        return Problem(
            f"{watch.name}: last run FAILED "
            f"({_stamp(newest.get('finished_at') or newest.get('started_at'))})"
            + (f" — {summary}" if summary else "")
        )

    ok_runs = [r for r in of_kind if r.get("ok", True)]
    if not ok_runs:
        return Problem(
            f"{watch.name}: no successful run on record at all. "
            f"Expected {watch.expected}."
        )

    newest = ok_runs[0]
    age = _age_hours(newest.get("finished_at") or newest.get("started_at"), now)
    if age is None:
        return Problem(
            f"{watch.name}: newest run has no usable timestamp — can't tell if it's current."
        )
    if age > watch.max_age_hours:
        return Problem(
            f"{watch.name}: no successful run in {age:.0f}h "
            f"(last was {_stamp(newest.get('finished_at') or newest.get('started_at'))}). "
            f"Expected {watch.expected}."
        )
    return None


# --- the entry point --------------------------------------------------------


def _default_notifier(message: str, state_path: Path | None = None) -> None:
    """Reuses the supervisor's destination rule — one chat, never a broadcast,
    and it degrades to stderr when there is nowhere to send."""
    from concierge import supervisor

    supervisor._default_notifier(message, state_path)


def run_if_due(
    *,
    now: datetime | None = None,
    state_path: Path | None = None,
    registry_path: Path | None = None,
    watches: tuple[Watch, ...] = WATCHES,
    fetch=fetch_runs,
    notifier=None,
) -> str:
    """Poll every watch if the interval has elapsed, alert on what's wrong.

    Silent and near-free on the 287 calls a day that aren't due. Returns a
    one-word outcome for the CLI to echo.
    """
    now = now or datetime.now(timezone.utc)
    notifier = notifier or (lambda msg: _default_notifier(msg, registry_path))

    state = load_state(state_path)
    if not is_due(state, now):
        return "not-due"

    state["last_poll"] = now.isoformat(timespec="seconds")
    alerts = state.setdefault("alerts", {})
    unconfirmed = state.setdefault("unconfirmed", {})

    fired = 0
    for watch in watches:
        problem = check(watch, now, fetch=fetch)

        if problem is None:
            # Recovered: drop the cooldown so the next failure alerts at once
            # instead of being swallowed by a stale timestamp, and forget any
            # half-seen provisional problem.
            alerts.pop(watch.name, None)
            unconfirmed.pop(watch.name, None)
            continue

        if problem.provisional and watch.name not in unconfirmed:
            # First sighting. Wait for the next poll to confirm it's real.
            unconfirmed[watch.name] = now.isoformat(timespec="seconds")
            continue
        unconfirmed.pop(watch.name, None)

        if in_cooldown(state, watch.name, now):
            continue
        notifier(problem.text)
        alerts[watch.name] = now.isoformat(timespec="seconds")
        fired += 1

    save_state(state, state_path)
    return "ok" if not fired else f"alerted:{fired}"
