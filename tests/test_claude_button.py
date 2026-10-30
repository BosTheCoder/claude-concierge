"""The Claude button's endpoint: a page POSTs what it shows, a job starts with it.

Driven through the real HTTP server, because the route has to answer both bare
(the dashboard's own port) and under /claude (Tailscale Serve, which strips it).
spawn_job is faked: a test must never start a real session.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from concierge import dashboard, registry, spawn


@pytest.fixture
def server(monkeypatch):
    calls = []

    def fake_spawn(**kw):
        calls.append(kw)
        if kw["cwd"] == "/nope":
            raise ValueError("cwd not a permitted repo: /nope")
        return {"id": "A7", "rc_url": "https://claude.ai/code/session_x"}

    monkeypatch.setattr(spawn, "spawn_job", fake_spawn)
    monkeypatch.setattr(registry, "last_chat", lambda path=None: "123")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.handler_for(dashboard.Sampler()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", calls
    httpd.shutdown()


def post(url, body, headers=None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


GOOD = {"title": "Fix the refused tools", "cwd": "~/projects/personal/inbox-brief",
        "context": "refusals: Bash(cat)", "note": "rerun it", "page": "https://x/brief/"}


@pytest.mark.parametrize("path", ["/api/spawn", "/claude/api/spawn"])
def test_a_tap_starts_a_job_carrying_the_note_and_the_context(server, path):
    base, calls = server
    status, body = post(base + path, GOOD)
    assert (status, body) == (200, {"ok": True, "id": "A7", "rc_url": "https://claude.ai/code/session_x"})
    (call,) = calls
    assert call["cwd"] == GOOD["cwd"] and call["chat_id"] == "123"
    assert "rerun it" in call["brief"] and "refusals: Bash(cat)" in call["brief"]


@pytest.mark.parametrize("change, headers, status", [
    ({}, {"Sec-Fetch-Site": "cross-site"}, 403),
    ({}, {"Content-Type": "text/plain"}, 400),
    ({"cwd": "/nope"}, {}, 400),
    ({"title": ""}, {}, 400),
    ({"note": ["not", "text"]}, {}, 400),
    ({"context": "x" * 30_001}, {}, 400),
])
def test_bad_requests_are_refused_with_a_reason(server, change, headers, status):
    base, _ = server
    code, body = post(base + "/claude/api/spawn", {**GOOD, **change}, headers)
    assert code == status and body["ok"] is False and body["error"]


def test_no_job_starts_without_a_chat_to_report_to(server, monkeypatch):
    base, calls = server
    monkeypatch.setattr(registry, "last_chat", lambda path=None: None)
    code, _ = post(base + "/api/spawn", GOOD)
    assert code == 503 and calls == []


def test_the_widget_is_served_under_the_prefix(server):
    base, _ = server
    with urllib.request.urlopen(base + "/claude/claude.js") as r:
        assert r.status == 200 and b"claudeAsk" in r.read()
