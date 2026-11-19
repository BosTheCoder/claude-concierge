"""Chat messages he edits and sends from the app (WhatsApp, Telegram, anything Beeper bridges).

A job puts its draft in the thread with `concierge message <chat-id> "<text>"`. The card
shows the recent conversation and the draft, editable, with a Send button. Reading and
sending go through the beeper-inbox container, which already has the verified,
duplicate-safe send path (it logs the text before sending, then confirms delivery).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

INBOX = os.environ.get("CONCIERGE_INBOX_API", "http://127.0.0.1:8790")
INBOX_PAGE = "https://bos-desktop.fish-grouper.ts.net/inbox/#to="
SHOWN = 20  # recent messages on the card


def _req(path: str, body: dict | None = None, timeout: float = 30) -> dict:
    req = urllib.request.Request(INBOX + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"inbox {exc.code}: {exc.read().decode()[:300]}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"the inbox app isn't answering ({exc})") from None
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(f"inbox: {out['error']}")
    return out


def load(chat_id: str) -> dict:
    r = _req(f"/api/chat/{urllib.parse.quote(chat_id, safe='')}?meta=1")
    msgs = [{"from": "You" if m.get("is_me") else m.get("sender") or "", "mine": bool(m.get("is_me")),
             "ts": m.get("timestamp_ms"),
             "text": m.get("text") or m.get("caption") or (f"[{m.get('kind')}] {m.get('file_name') or ''}".strip())}
            for m in r.get("messages", [])][-SHOWN:]
    return {"chat_id": chat_id, "title": r.get("title") or chat_id, "network": r.get("network") or "",
            "messages": msgs, "inbox": INBOX_PAGE + urllib.parse.quote(chat_id, safe="")}


def send(chat_id: str, text: str) -> None:
    if not text.strip():
        raise ValueError("the message is empty")
    r = _req("/api/outreach/send", {"text": text, "chat_id": chat_id}, timeout=60)
    if r.get("dry_run"):
        raise RuntimeError("the inbox app is in practice mode (DRY_RUN): nothing was sent")
    if not r.get("sent"):
        raise RuntimeError(f"not sent: {r.get('detail') or 'no reason given'}")
