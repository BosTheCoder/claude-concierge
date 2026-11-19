"""A chat card: a job drafts a WhatsApp reply, he edits it in the app and sends it. The inbox
app's HTTP API is faked; the real CLI, threads and API route run."""
import pytest
from typer.testing import CliRunner

from concierge import app_api, chatdraft, cli, threads

CHAT = "!abc:beeper.local"


@pytest.fixture
def inbox(monkeypatch):
    state = {"sent": [], "dry_run": False}

    def req(path, body=None, timeout=30):
        if path.startswith("/api/chat/"):
            return {"chat_id": CHAT, "title": "Gideon", "network": "whatsapp", "messages": [
                {"sender": "Gideon", "is_me": False, "text": "When do the keys go north?", "timestamp_ms": 1},
                {"sender": "kbnyakundi", "is_me": True, "text": "", "kind": "image", "file_name": "keys.jpg", "timestamp_ms": 2}]}
        if path == "/api/outreach/send":
            if state["dry_run"]:
                return {"sent": True, "dry_run": True}
            state["sent"].append((body["chat_id"], body["text"]))
            return {"sent": True, "chat_id": body["chat_id"], "dry_run": False}
        raise AssertionError(path)

    monkeypatch.setattr(chatdraft, "_req", req)
    return state


def test_he_edits_a_chat_draft_and_sends_it(inbox, monkeypatch):
    db = threads.connect()
    th = threads.find_or_create(db, job="A1", title="Keys", pane="concierge:A1")
    monkeypatch.setattr(threads, "_alert", lambda *a, **k: None)
    res = CliRunner().invoke(cli.app, ["message", CHAT, "Sending them Friday.", "--thread", str(th["id"])])
    assert res.exit_code == 0, res.output
    mid = db.execute("SELECT id FROM messages WHERE kind='chat'").fetchone()["id"]

    code, card = app_api.get(f"/api/threads/{th['id']}/chat", {"msg": [str(mid)]})
    assert code == 200 and card["draft"] == "Sending them Friday." and card["title"] == "Gideon"
    assert "keys.jpg" in card["messages"][1]["text"] and card["messages"][1]["mine"]

    told = []
    monkeypatch.setattr(app_api, "_bg", lambda fn, *a, **k: fn(db, *a, submitter=lambda p, t: told.append(t), **k))
    # Practice mode in the inbox: refused, and the card stays open.
    inbox["dry_run"] = True
    code, out = app_api.post(f"/api/threads/{th['id']}/sendchat", {"msg": mid, "text": "Sending them Saturday."})
    assert code == 502 and not told
    inbox["dry_run"] = False
    code, out = app_api.post(f"/api/threads/{th['id']}/sendchat", {"msg": mid, "text": "Sending them Saturday."})
    assert code == 200, out
    assert inbox["sent"] == [(CHAT, "Sending them Saturday.")]
    assert "Sending them Saturday." in told[0]  # the job learns what actually went
    # Sent once: a second tap is refused and nothing more goes out.
    assert app_api.post(f"/api/threads/{th['id']}/sendchat", {"msg": mid, "text": "again"})[0] == 400
    assert len(inbox["sent"]) == 1
