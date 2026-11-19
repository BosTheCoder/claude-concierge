"""Email drafts he edits and sends from the app.

A job makes the Gmail draft as usual, then `concierge draft <draft-id>` puts it in the
thread as a card: From (his Send As aliases), To, Cc, Subject and the body, editable,
and a Send button. The draft stays in Gmail, so the card always shows what Gmail has.

Gmail goes through the toolkit's gdraft (stdlib, reads the workspace MCP's token).
Only the new text is editable: the quoted thread below it (`gmail_quote`) is kept
byte for byte, so a reply still threads and still collapses to "...".
"""

from __future__ import annotations

import base64
import email
import email.policy
import html as htmllib
import importlib.util
import os
import re
from email.utils import formataddr, parseaddr
from html.parser import HTMLParser
from pathlib import Path

GDRAFT = Path(os.environ.get("TOOLKIT_HOME", Path.home() / "projects/personal/toolkit")) / "tools/other/gdraft/gdraft.py"
QUOTE = re.compile(r'<div class="gmail_quote')
PLAIN_QUOTE = re.compile(r"\n\s*On [^\n]*(?:\n[^\n]*)?wrote:\s*\n", re.S)


def _gd():
    spec = importlib.util.spec_from_file_location("gdraft", GDRAFT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except SystemExit as exc:  # gdraft reports Gmail errors by exiting
        raise RuntimeError(str(exc)) from None


def _split(html: str) -> tuple[str, str]:
    m = QUOTE.search(html)
    return (html[:m.start()], html[m.start():]) if m else (html, "")


def _clean(html: str) -> str:
    """What the app renders as editable. His own drafts, but never run a script from mail."""
    html = re.sub(r"<(script|style|iframe|object|embed)\b.*?</\1\s*>", "", html, flags=re.S | re.I)
    return re.sub(r"\s on\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html, flags=re.I)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "h1", "h2", "h3", "tr"):
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


def _parts(gd, raw: bytes):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    h = msg.get_body(preferencelist=("html",))
    p = msg.get_body(preferencelist=("plain",))
    html = h.get_content() if h else "<p>" + htmllib.escape(p.get_content() if p else "").replace("\n", "<br>") + "</p>"
    return msg, html, (p.get_content() if p else to_text(html))


# Where a reply's quoted history starts: Gmail's "On ... wrote:", Outlook's rule then From:/Sent:.
QUOTED = re.compile(r"\n\s*(?:On [^\n]*(?:\n[^\n]*)?wrote:|_{10,}\s*\nFrom:|-{3,} ?Original Message|From: [^\n]+\n(?:Sent|Date): )", re.S)


def thread(gd, token, thread_id: str | None, limit: int = 8) -> list[dict]:
    """The emails the draft replies to, oldest first, drafts left out. Each one's own quoted
    history is cut (it repeats the one before), except the oldest's: that is often his
    original question, which isn't in the thread on its own."""
    if not thread_id:
        return []
    ids = [m["id"] for m in _call(gd.api, token, f"/threads/{thread_id}", format="minimal").get("messages", [])
           if "DRAFT" not in m.get("labelIds", [])][-limit:]
    out = []
    for i, mid in enumerate(ids):
        r = _call(gd.api, token, f"/messages/{mid}", format="raw")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(r["raw"]), policy=email.policy.default)
        b = msg.get_body(preferencelist=("plain", "html"))
        text = (b.get_content() if b.get_content_type() == "text/plain" else to_text(b.get_content())) if b else ""
        text = re.sub(r"\[cid:[^\]]*\]\n?|[\u200b\u200c\u200d\ufeff]", "", text).replace("\r", "")
        if i and (q := QUOTED.search(text)):
            text = text[:q.start()]
        name, addr = parseaddr(str(msg.get("From") or ""))
        out.append({"from": name or addr, "email": addr, "date": str(msg.get("Date") or ""),
                    "text": re.sub(r"\n{3,}", "\n\n", text).strip()[:6000]})
    return out


def load(draft_id: str, *, user: str | None = None) -> dict:
    gd = _gd()
    token = _call(gd.access_token, user or gd.DEFAULT_EMAIL)
    d, raw = _call(gd.load_draft, token, draft_id)
    msg, html, _ = _parts(gd, raw)
    head, _tail = _split(html)
    send_as = _call(gd.api, token, "/settings/sendAs").get("sendAs", [])
    aliases = [{"email": a["sendAsEmail"], "name": a.get("displayName") or ""} for a in send_as]
    default = next((a["sendAsEmail"] for a in send_as if a.get("isDefault")), "")
    return {"id": draft_id, "from": parseaddr(str(msg.get("From") or ""))[1] or default,
            "to": str(msg.get("To") or ""), "cc": str(msg.get("Cc") or ""), "subject": str(msg.get("Subject") or ""),
            "html": _clean(head), "aliases": aliases, "reply": bool(msg.get("In-Reply-To")),
            "attachments": [p.get_filename() or "inline image" for p in gd.parts_to_keep(msg)],
            "thread": _thread_or_none(gd, token, d["message"].get("threadId")),
            "gmail": f"https://mail.google.com/mail/u/0/#drafts?compose={d['message']['id']}"}


def _thread_or_none(gd, token, thread_id):
    try:
        return thread(gd, token, thread_id)
    except RuntimeError:  # the card still works without its history
        return []


def send(draft_id: str, *, sender: str, to: str, cc: str, subject: str, html: str | None,
         user: str | None = None) -> str:
    """Send the draft with his edits. `html` is the new text only (None: unchanged).
    Returns the sent message's id."""
    gd = _gd()
    token = _call(gd.access_token, user or gd.DEFAULT_EMAIL)
    d, raw = _call(gd.load_draft, token, draft_id)
    msg, old_html, old_plain = _parts(gd, raw)
    aliases = {a["sendAsEmail"]: a.get("displayName") or ""
               for a in _call(gd.api, token, "/settings/sendAs").get("sendAs", [])}
    if sender not in aliases:
        raise ValueError(f"{sender} is not one of your Send As addresses")
    if not to.strip():
        raise ValueError("To is empty")
    headers = {"From": formataddr((aliases[sender], sender)), "To": to.strip(), "Cc": cc.strip(),
               "Subject": subject.strip()}
    same = {"From": parseaddr(str(msg.get("From") or ""))[1], "To": str(msg.get("To") or ""),
            "Cc": str(msg.get("Cc") or ""), "Subject": str(msg.get("Subject") or "")}
    changed_headers = any(headers[k] != same[k] for k in ("To", "Cc", "Subject")) or sender != same["From"]
    if html is not None or changed_headers:
        head, tail = _split(old_html)
        new_head = html if html is not None else head
        q = PLAIN_QUOTE.search(old_plain)
        plain = to_text(new_head) + (old_plain[q.start():] if q else "")
        # An empty Cc stays in `headers` on purpose: rebuild() then neither sets it nor carries the old one.
        new_raw = gd.rebuild(raw, body=plain, html=new_head + tail, headers=headers)
        _call(gd.api, token, f"/drafts/{gd.draft_id(draft_id)}", "PUT", {
            "id": draft_id,
            "message": {"raw": base64.urlsafe_b64encode(new_raw).decode(), "threadId": d["message"].get("threadId")}})
    return _call(gd.send_draft, token, draft_id)
