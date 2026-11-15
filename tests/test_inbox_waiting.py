import json

import pytest

from concierge import inbox_waiting

THREAD, REPLY = "msg_THREAD1", "msg_REPLY1"


def line(**entry):
    return json.dumps(entry) + "\n"


def sent_chat(extra=""):
    """A transcript that sent into THREAD through the Inbox tool, as Claude Code writes it."""
    return (line(cwd="/repo", message={"content": [
                {"type": "tool_use", "id": "t1", "name": "mcp__plannotator__plannotator_inbox",
                 "input": {"action": "send_message"}}]})
            + line(message={"content": [{"type": "tool_result", "tool_use_id": "t1",
                                         "content": f"Sent to the Plannotator Inbox (thread {THREAD}, …)"}]})
            + extra)


@pytest.fixture
def chats(tmp_path):
    root = tmp_path / "projects"
    (root / "proj").mkdir(parents=True)
    return root


def fake_plannotator(project_root, last_author="person"):
    def run(*args):
        if args[:2] == ("inbox", "projects"):
            return {"projects": [{"name": "tasks", "root": str(project_root)}]}
        return {"threads": [{"thread_id": THREAD, "subject": "Spec", "thread_name": "Calendar spec",
                             "last_author": last_author, "last_person_message_id": REPLY,
                             "last_person_at": "2026-11-15T15:41:41Z"}]}
    return run


def test_a_reply_waiting_for_a_closed_chat_is_listed_and_resumable(chats, tmp_path):
    (chats / "proj" / "sess-closed.jsonl").write_text(sent_chat())
    # Another chat only quoting the thread id (as a screen capture would) does not own it.
    (chats / "proj" / "sess-bystander.jsonl").write_text(
        line(cwd="/other", message={"content": [{"type": "tool_result", "tool_use_id": "x", "content": THREAD}]}))

    found = inbox_waiting.waiting(run=fake_plannotator(tmp_path), root=chats, live=set)
    assert [(w["session_id"], w["cwd"], w["subject"]) for w in found] == [("sess-closed", "/repo", "Calendar spec")]

    opened = []

    class Tmux:
        list_windows = staticmethod(lambda s: [])
        build_shell_command = staticmethod(lambda cwd, argv: (cwd, argv))
        new_window = staticmethod(lambda s, w, cmd: opened.append(cmd))

    inbox_waiting.resume("sess-closed", found=found, tmux=Tmux, state_path=tmp_path / "jobs.json")
    cwd, argv = opened[0]
    assert cwd == "/repo" and argv[-2:] == ["--resume", "sess-closed"]
    with pytest.raises(ValueError):
        inbox_waiting.resume("sess-bystander", found=found, tmux=Tmux, state_path=tmp_path / "jobs.json")


def test_nothing_is_listed_when_the_chat_already_has_the_reply_or_is_running(chats, tmp_path):
    (chats / "proj" / "sess-a.jsonl").write_text(sent_chat())
    run = fake_plannotator(tmp_path)
    assert inbox_waiting.waiting(run=run, root=chats, live=lambda: {"sess-a"}) == []

    (chats / "proj" / "sess-a.jsonl").write_text(sent_chat(line(message={"content": f"Plannotator Inbox: x ({REPLY})"})))
    assert inbox_waiting.waiting(run=run, root=chats, live=set) == []

    assert inbox_waiting.waiting(run=fake_plannotator(tmp_path, last_author="agent"), root=chats, live=set) == []


def test_a_plannotator_update_that_changes_its_output_is_reported_not_hidden(tmp_path):
    def changed(*args):
        if args[:2] == ("inbox", "projects"):
            return {"projects": [{"name": "tasks", "root": str(tmp_path)}]}
        return {"items": []}
    with pytest.raises(inbox_waiting.PlannotatorChanged):
        inbox_waiting.waiting(run=changed, root=tmp_path, live=set)
