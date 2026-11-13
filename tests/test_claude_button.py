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
        return {"id": "A7", "rc_url": None}

    monkeypatch.setattr(spawn, "spawn_job", fake_spawn)
    monkeypatch.setattr(dashboard, "finish_start", lambda job_id, cwd: None)
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
    assert (status, body) == (200, {"ok": True, "id": "A7", "rc_url": None})
    (call,) = calls
    assert call["cwd"] == GOOD["cwd"] and call["chat_id"] == "123"
    assert "rerun it" in call["brief"] and "refusals: Bash(cat)" in call["brief"]


@pytest.mark.parametrize("change, headers, status", [
    ({}, {"Sec-Fetch-Site": "cross-site"}, 403),
    ({}, {"Origin": "http://evil.test:8787"}, 403),
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


def test_the_reply_does_not_wait_for_the_chat_url(server, monkeypatch):
    base, _ = server
    release = threading.Event()
    monkeypatch.setattr(dashboard, "finish_start", lambda job_id, cwd: release.wait(10))
    import time
    t0 = time.monotonic()
    try:
        assert post(base + "/api/spawn", GOOD)[0] == 200
        assert time.monotonic() - t0 < 2  # answered while finish_start is still blocked
    finally:
        release.set()


@pytest.fixture
def tmux_fakes(monkeypatch):
    from concierge import tmuxctl
    seen = {"closed": [], "marked": [], "pane": "", "windows": ["A8"]}
    monkeypatch.setattr(tmuxctl, "capture", lambda session, window: seen["pane"])
    monkeypatch.setattr(tmuxctl, "list_windows", lambda session: seen["windows"])
    monkeypatch.setattr(tmuxctl, "kill_window", lambda session, window: seen["closed"].append(window))
    monkeypatch.setattr(registry, "upsert", lambda job_id, path=None, **kw: seen["marked"].append((job_id, kw)))
    return seen


def test_finishing_a_start_records_the_chat_url(tmux_fakes):
    dashboard.finish_start("A8", "~/r", poller=lambda w: "https://claude.ai/code/session_x")
    assert tmux_fakes["marked"] == [("A8", {"rc_url": "https://claude.ai/code/session_x"})]


def test_a_job_stuck_on_the_trust_prompt_is_closed_and_marked_failed(tmux_fakes):
    tmux_fakes["pane"] = "Quick safety check: " + dashboard.TRUST_PROMPT + "?"
    dashboard.finish_start("A8", "~/r", poller=lambda w: None)
    (job_id, fields), = tmux_fakes["marked"]
    assert tmux_fakes["closed"] == ["A8"] and fields["status"] == "failed" and "trust" in fields["error"]


def test_a_window_that_died_on_startup_is_marked_failed(tmux_fakes):
    tmux_fakes["windows"] = []
    dashboard.finish_start("A8", "~/r", poller=lambda w: None)
    assert tmux_fakes["marked"][0][1]["status"] == "failed"


def test_a_slow_start_is_left_running(tmux_fakes):
    dashboard.finish_start("A8", "~/r", poller=lambda w: None)
    assert tmux_fakes["marked"] == [] and tmux_fakes["closed"] == []


def test_the_tray_lists_the_last_days_jobs_newest_first(server, monkeypatch):
    base, _ = server
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    at = lambda h: (now - timedelta(hours=h)).isoformat(timespec="seconds")
    monkeypatch.setattr(registry, "load", lambda path=None: {
        "B1": {"id": "B1", "title": "older", "status": "running", "opened_at": at(2), "brief": "secret"},
        "C2": {"id": "C2", "title": "newer", "status": "running", "opened_at": at(1)},
        "D3": {"id": "D3", "title": "last week", "status": "running", "opened_at": at(30)},
    })
    with urllib.request.urlopen(base + "/claude/api/jobs") as r:
        jobs = json.loads(r.read())["jobs"]
    assert [j["id"] for j in jobs] == ["C2", "B1"] and "brief" not in jobs[1]


def test_a_bad_body_length_gets_an_answer_not_a_hang(server):
    import http.client
    base, calls = server
    conn = http.client.HTTPConnection(base.removeprefix("http://"), timeout=5)
    conn.putrequest("POST", "/api/spawn")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    assert conn.getresponse().status == 400 and calls == []
