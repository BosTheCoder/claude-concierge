"""Every Telegram message the bot sends or receives, and where it came from.

The Bot API has no history. A reply arrives carrying only the id of the message
it answers (and, with the plugin patched, a copy of its text), and that is
useless unless something wrote down at send time which session sent it and
what it was working on. This file is that record.

Why it exists, 2026-09-14: a session Bosire had started by hand sent him an
ethernet-cable shortlist through the bot. He replied to it, "is it not better
to get 40m", and the concierge had no way to see what he was replying to. It
searched the jobs and the recent folders for lengths, found nothing, and had to
ask him. The folder had been there all along.

Two writers append to one file, one JSON object per line:

  telegram.send   every message sent from Python: `notify`, the supervisor,
                  the heartbeat, the reaper, and any session that imports the
                  sender directly (which is what that ethernet session did).
  the plugin      every inbound message, and every `reply` the concierge
                  makes. See patches/telegram-reply-context.patch.

The file sits beside the plugin's own state because that is the one directory
both writers already know. TELEGRAM_STATE_DIR moves it, for both, the same way.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from concierge import config

TRANSCRIPTS = Path.home() / ".claude" / "projects"
DATED = re.compile(r"\b(20\d\d-\d\d-\d\d-[a-z0-9][a-z0-9-]*[a-z0-9])\b")
QUOTE_CHARS = 400


def log_path() -> Path:
    root = os.environ.get("TELEGRAM_STATE_DIR")
    return (Path(root) if root else config.TELEGRAM_ENV.parent) / "messages.jsonl"


def origin(env: dict | None = None, cwd: str | None = None) -> dict:
    """Who is sending, read from the environment Claude Code gives every
    session. Nothing here depends on the sender knowing it should say."""
    env = os.environ if env is None else env
    fields = {
        "job": env.get("CONCIERGE_JOB_ID"),
        "session": env.get("CLAUDE_CODE_SESSION_ID"),
        "rc_session": env.get("CLAUDE_CODE_BRIDGE_SESSION_ID"),
        "cwd": cwd or os.getcwd(),
    }
    return {k: v for k, v in fields.items() if v}


def record(entry: dict, path: Path | None = None) -> None:
    """Append one line. Never raises: a lost log line must not lose a message."""
    try:
        target = path or log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with target.open("a") as fh:
            fh.write(json.dumps({"ts": stamp, **entry}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - deliberately total
        pass


def load(path: Path | None = None) -> list[dict]:
    # ponytail: reads the whole file per lookup; ~100 lines a day, rotate if it
    # ever reaches megabytes.
    target = path or log_path()
    if not target.exists():
        return []
    rows = []
    for line in target.read_text(errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def find(rows: list[dict], message_id: str, chat_id: str | None) -> dict | None:
    """The logged message with that id. Ids are per chat in Telegram, so the
    notifications group can reuse one; the chat narrows it when known."""
    for row in reversed(rows):
        if str(row.get("message_id")) != str(message_id):
            continue
        if chat_id and str(row.get("chat_id")) != str(chat_id):
            continue
        return row
    return None


# --- where a message came from ----------------------------------------------


def transcript_for(session: str | None) -> Path | None:
    if not session:
        return None
    hits = sorted(TRANSCRIPTS.glob(f"*/{session}.jsonl"))
    return hits[0] if hits else None


def folders_written(transcript: Path | None) -> list[str]:
    """Dated task folders the session wrote files into, most recent last.

    The folder a session was working in is the strongest evidence of what a
    message from it was about, and the transcript is the one place every
    session leaves it, told or not.
    """
    if transcript is None:
        return []
    seen: list[str] = []
    with transcript.open(errors="replace") as fh:
        for line in fh:
            if '"file_path"' not in line:
                continue
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except ValueError:
                continue
            for part in content if isinstance(content, list) else []:
                if part.get("type") != "tool_use" or part.get("name") not in (
                    "Write", "Edit", "NotebookEdit"
                ):
                    continue
                match = DATED.search(str((part.get("input") or {}).get("file_path", "")))
                if match:
                    if match.group(1) in seen:
                        seen.remove(match.group(1))
                    seen.append(match.group(1))
    return seen


def folder_path(name: str) -> Path | None:
    for repo in config.SETTINGS.repos:
        for candidate in (repo.path / name, *repo.path.glob(f"*/{name}")):
            if candidate.is_dir():
                return candidate
    return None


def live_sessions() -> dict[str, dict]:
    """Running Claude sessions by uuid. Lazy: /proc and a capture per pane."""
    from concierge import sessions

    return {s["uuid"]: s for s in sessions.snapshot()["sessions"] if s.get("uuid")}


def _quote(text: str | None, limit: int = QUOTE_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _when(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


def describe(row: dict, jobs: dict, live: dict, rows: list[dict]) -> list[str]:
    """Everything known about one logged message, as lines for the concierge."""
    lines = [
        f"message {row.get('message_id')} · {'from him' if row.get('dir') == 'in' else 'sent by the bot'}"
        f" · {_when(row.get('ts'))} UTC"
        + (f" · replying to {row['reply_to']}" if row.get("reply_to") else ""),
        f'  "{_quote(row.get("text"))}"',
    ]

    job_id = row.get("job")
    session = row.get("session")
    folders: list[str] = []
    if row.get("task_folder"):
        folders.append(row["task_folder"])

    if job_id:
        job = jobs.get(job_id) or {}
        lines.append(
            f"from: job [{job_id}] {job.get('title', '(pruned from the registry)')}"
            f" — {job.get('status', 'unknown')}"
        )
        if job.get("task_folder"):
            folders.append(job["task_folder"])
    elif row.get("via") == "plugin":
        lines.append("from: the concierge itself")
    elif session:
        lines.append(f"from: session {session} (not a concierge job) · cwd {row.get('cwd')}")
    elif row.get("dir") != "in":
        lines.append(f"from: unknown sender · cwd {row.get('cwd')}")

    transcript = transcript_for(session)
    folders += list(reversed(folders_written(transcript)))
    folders += [m for m in DATED.findall(row.get("text") or "")]
    folders = list(dict.fromkeys(folders))
    if folders:
        best = folder_path(folders[0])
        lines.append(f"folder: {best or folders[0]}")
        if folders[1:]:
            lines.append(f"  also touched: {', '.join(folders[1:4])}")
    if transcript:
        lines.append(f"transcript: {transcript}")

    if session and not job_id:
        running = live.get(session)
        if running and running.get("tmux"):
            lines.append(
                f"live in tmux ({running.get('tmux')}): route with `concierge send {session} '<msg>'`"
            )
        elif running:
            lines.append(
                "live but not in tmux, so nothing can type into it: spawn a follow-up job with --task-folder"
            )
        else:
            lines.append("no longer running: spawn a follow-up job with --task-folder")

    threaded = [
        f"[{j}] {job.get('title')} — {job.get('status')} ({job.get('task_folder')})"
        for j, job in jobs.items()
        if str(job.get("root_message_id")) == str(row.get("message_id"))
    ]
    if threaded:
        lines.append("jobs started from this message: " + "; ".join(threaded))
    answers = [
        str(r.get("message_id"))
        for r in rows
        if str(r.get("reply_to")) == str(row.get("message_id")) and r is not row
    ]
    if answers:
        lines.append("replied to by: " + ", ".join(answers))
    return lines


def context(
    message_id: str,
    chat_id: str | None = None,
    *,
    jobs: dict | None = None,
    live: dict | None = None,
    path: Path | None = None,
) -> str:
    """`concierge context <id>`: the message, who sent it, and their folder."""
    from concierge import registry

    rows = load(path)
    jobs = registry.load() if jobs is None else jobs
    row = find(rows, message_id, chat_id or registry.last_chat())
    if row is None:
        return (
            f"message {message_id} is not in the log — sent before logging began "
            f"(2026-09-14), or by a path that bypasses it.\n\n" + recent(jobs=jobs, path=path)
        )
    live = live_sessions() if live is None else live
    out = describe(row, jobs, live, rows)
    parent = find(rows, row["reply_to"], row.get("chat_id")) if row.get("reply_to") else None
    if parent is not None:
        out += ["", "which replies to:"] + describe(parent, jobs, live, rows)
    return "\n".join(out)


def recent(
    limit: int = 12, *, jobs: dict | None = None, path: Path | None = None
) -> str:
    """`concierge recent`: the fallback when he did not use Telegram's reply."""
    from concierge import registry

    jobs = registry.load() if jobs is None else jobs
    out = ["recent messages (newest last):"]
    folder_cache: dict[str, str] = {}
    for row in load(path)[-limit:]:
        who = "him" if row.get("dir") == "in" else (
            f"[{row['job']}]" if row.get("job")
            else "concierge" if row.get("via") == "plugin"
            else f"session {str(row.get('session'))[:8]}"
        )
        session = row.get("session")
        if session and not row.get("job") and session not in folder_cache:
            written = folders_written(transcript_for(session))
            folder_cache[session] = written[-1] if written else ""
        folder = row.get("task_folder") or folder_cache.get(session or "", "")
        out.append(
            f"  {row.get('message_id')} {_when(row.get('ts'))[11:]} {who}"
            + (f" [{folder}]" if folder else "")
            + f": {_quote(row.get('text'), 90)}"
        )

    out += ["", "jobs, newest first (finished and reaped included):"]
    for job_id, job in sorted(
        jobs.items(), key=lambda kv: kv[1].get("last_update") or "", reverse=True
    )[:8]:
        out.append(
            f"  [{job_id}] {job.get('title')} — {job.get('status')} · {job.get('task_folder')}"
        )

    out += ["", "task folders, most recently touched first:"]
    out += [f"  {p}" for p in recent_folders()]
    return "\n".join(out)


def recent_folders(limit: int = 8) -> list[Path]:
    found = []
    for repo in config.SETTINGS.repos:
        for index in (*repo.path.glob("*/index.md"), *repo.path.glob("*/*/index.md")):
            if DATED.fullmatch(index.parent.name):
                try:
                    found.append((index.stat().st_mtime, index.parent))
                except OSError:
                    continue
    return [p for _, p in sorted(found, reverse=True)[:limit]]
