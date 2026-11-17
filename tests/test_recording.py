"""A screen recording from the phone becomes a job that can read it.

Real HTTP server and real ffmpeg; the transcriber (a paid API) and spawn_job are faked,
and recordings land in a temp dir.
"""

import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from concierge import dashboard, recording, registry, spawn


@pytest.fixture
def server(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(recording, "ROOT", tmp_path / "rec")
    fake = tmp_path / "transcribe"
    fake.write_text("#!/bin/sh\necho '[00:01] Why is this button greyed out?'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(recording, "TRANSCRIBE", fake)
    monkeypatch.setattr(spawn, "spawn_job", lambda **kw: calls.append(kw) or {"id": "A9"})
    monkeypatch.setattr(dashboard, "finish_start", lambda job_id, cwd: None)
    monkeypatch.setattr(registry, "last_chat", lambda path=None: "123")
    monkeypatch.setattr(recording, "start_in_background", lambda folder, note, **kw: recording.start(folder, note, **kw))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.handler_for(dashboard.Sampler()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", calls, tmp_path
    httpd.shutdown()


def upload(url, data, headers=None):
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "video/mp4", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_an_upload_starts_a_job_with_frames_transcript_and_note(server, tmp_path):
    base, calls, _ = server
    video = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=270x584:rate=10:duration=3",
                    "-f", "lavfi", "-i", "sine=duration=3", "-shortest", str(video)], check=True)

    status, body = upload(base + "/claude/api/recording?note=fix%20this", video.read_bytes())

    assert status == 200 and body["ok"]
    (call,) = calls
    folder = recording.ROOT / body["folder"]
    assert len(list((folder / "frames").glob("f_*.jpg"))) == 3
    assert str(folder / "frames") in call["brief"] and "greyed out" in call["brief"] and "fix this" in call["brief"]
    assert "Prep problems" not in call["brief"]
    assert call["title"].startswith("Screen rec: fix this")


def test_a_page_cannot_post_a_recording(server):
    base, calls, _ = server
    status, _ = upload(base + "/api/recording", b"x" * 10, {"Origin": "https://evil.example"})
    assert status == 403 and not calls


def test_a_failed_prep_step_still_starts_the_job_and_says_why(server):
    base, calls, _ = server
    status, _ = upload(base + "/api/recording", b"not a video")
    assert status == 200
    (call,) = calls
    assert "Prep problems" in call["brief"] and "frames:" in call["brief"]
