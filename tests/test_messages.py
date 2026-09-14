"""The 2026-09-14 incident, end to end.

A session he started by hand (no job id) sent him an ethernet-cable shortlist.
He replied "is it not better to get 40m" and the concierge could not tell what
he meant. Only the edges are faked here: Telegram's HTTP answer and the
session's transcript on disk. notify, telegram.send, the log and the lookup
are the real ones.
"""

import json

from concierge import cli, messages, registry

SESSION = "6c3bbe2b-786d-43c5-8263-96b806e98652"
FOLDER = "2026-09-14-ethernet-cable-to-bedroom"


def test_a_reply_to_a_hand_started_sessions_message_leads_back_to_its_folder(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CONCIERGE_JOB_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setattr(messages, "TRANSCRIPTS", tmp_path / "projects")
    transcript = tmp_path / "projects" / "-home-bos-tasks" / f"{SESSION}.jsonl"
    transcript.parent.mkdir(parents=True)
    write = {"type": "tool_use", "name": "Write",
             "input": {"file_path": f"/home/bos/tasks/{FOLDER}/shortlist.md"}}
    transcript.write_text(json.dumps({"message": {"content": [write]}}) + "\n")

    state = tmp_path / "jobs.json"
    registry.remember_chat("999", state)
    monkeypatch.setattr(cli.telegram, "load_token", lambda: "T")
    monkeypatch.setattr(
        cli.telegram, "_post",
        lambda url, payload: {"ok": True, "result": {"message_id": 1013}},
    )

    # The message says nothing about which folder it came from.
    cli.notify(None, "Buy 30m of round Cat6, two are in your basket.", state_path=state)

    # His reply, as the patched plugin logs it.
    messages.record({"dir": "in", "chat_id": "999", "message_id": 1014,
                     "text": "Is it not better to get 40m", "reply_to": 1013})

    out = messages.context("1014", "999", jobs={}, live={})

    assert FOLDER in out
    assert SESSION in out
