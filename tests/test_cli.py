from datetime import datetime, timezone
import pytest
import typer
from concierge import cli, registry


def test_format_jobs_lists_one_line_per_active_job():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    jobs = {
        "A3": {
            "id": "A3", "title": "calibre cleanup", "status": "running",
            "opened_at": "2026-08-05T14:00:00+00:00",
        },
        "Z9": {"id": "Z9", "title": "old", "status": "done",
               "opened_at": "2026-08-01T09:00:00+00:00"},
    }
    out = cli.format_jobs(jobs, now)
    assert "[A3] calibre cleanup — running · 1h" in out
    assert "Z9" not in out


def test_format_jobs_says_so_when_there_are_none():
    assert cli.format_jobs({}, datetime.now(timezone.utc)) == "no active jobs"


def test_notify_sends_to_the_chat_recorded_for_that_job(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="running",
                    chat_id="999", root_message_id=4471, title="t")

    sent = []
    monkeypatch.setattr(
        cli.telegram, "send",
        lambda chat_id, text, reply_to=None, **kw: sent.append(
            (chat_id, text, reply_to, kw.get("prefix"))
        ),
    )

    cli.notify("A3", "done", file=None, status="done", state_path=state)

    chat_id, text, reply_to, prefix = sent[0]
    assert chat_id == "999"
    assert prefix == "[A3] "
    assert reply_to == 4471


def test_notify_updates_the_status(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="running", chat_id="9", title="t")
    monkeypatch.setattr(cli.telegram, "send", lambda *a, **k: None)

    cli.notify("A3", "finished", file=None, status="done", state_path=state)

    assert registry.load(state)["A3"]["status"] == "done"


def test_notify_raises_for_an_unknown_job(tmp_path):
    with pytest.raises(KeyError, match="ZZ"):
        cli.notify("ZZ", "hi", file=None, status=None,
                   state_path=tmp_path / "jobs.json")


def test_notify_prefixes_every_chunk_via_the_send_prefix(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="running", chat_id="9", title="t")

    seen = {}
    monkeypatch.setattr(
        cli.telegram, "send",
        lambda chat_id, text, reply_to=None, **kw: seen.update(kw, text=text),
    )

    cli.notify("A3", "done", file=None, status=None, state_path=state)

    assert seen["prefix"] == "[A3] "
    assert seen["text"] == "done"


def test_notify_remembers_the_chat_for_supervisor_alerts(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="running", chat_id="555", title="t")
    monkeypatch.setattr(cli.telegram, "send", lambda *a, **k: None)

    cli.notify("A3", "done", file=None, status=None, state_path=state)

    assert registry.last_chat(state) == "555"


def published(ok=True, branch="main", detail=""):
    """Stand in for the git side. What it did is publish.py's business."""
    return lambda cwd, relpath: cli.publish.Published(ok, branch, detail)


def test_notify_with_a_file_appends_the_github_link(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd=str(cli.config.REPOS[0].path), task_folder="2026-08-05-thing",
    )

    monkeypatch.setattr(cli.publish, "publish", published())
    sent = []
    monkeypatch.setattr(
        cli.telegram, "send",
        lambda chat_id, text, **kw: sent.append(text),
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert sent[0] == (
        "done\nhttps://github.com/example/notes/blob/main/"
        "2026-08-05-thing/report.md"
    )


def test_the_file_is_pushed_before_the_message_is_sent(tmp_path, monkeypatch):
    """The whole bug. He taps the link the instant it arrives, so the content
    behind it has to be on GitHub already — the `Stop` hook that would commit
    it is async and fires after the turn, which is far too late."""
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd=str(cli.config.REPOS[0].path), task_folder="2026-08-05-thing",
    )

    order = []
    monkeypatch.setattr(
        cli.publish, "publish",
        lambda cwd, relpath: order.append("push")
        or cli.publish.Published(True, "main", ""),
    )
    monkeypatch.setattr(
        cli.telegram, "send", lambda *a, **k: order.append("send")
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert order == ["push", "send"]


def test_a_file_that_would_not_push_is_sent_as_a_path_not_a_dead_link(
    tmp_path, monkeypatch
):
    """A 404 is worse than the path it replaced, so an unpushed file never gets
    a URL — it gets the path and the reason."""
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd=str(cli.config.REPOS[0].path), task_folder="2026-08-05-thing",
    )

    monkeypatch.setattr(
        cli.publish, "publish", published(ok=False, detail="push failed: offline")
    )
    sent = []
    monkeypatch.setattr(
        cli.telegram, "send", lambda chat_id, text, **kw: sent.append(text)
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert "github.com" not in sent[0]
    assert "2026-08-05-thing/report.md" in sent[0]
    assert "push failed: offline" in sent[0]


def test_the_link_points_at_the_branch_that_was_pushed(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd=str(cli.config.REPOS[0].path), task_folder="f",
    )

    monkeypatch.setattr(cli.publish, "publish", published(branch="side"))
    sent = []
    monkeypatch.setattr(
        cli.telegram, "send", lambda chat_id, text, **kw: sent.append(text)
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert "/blob/side/f/report.md" in sent[0]


def test_the_message_still_goes_out_when_the_git_side_blows_up(
    tmp_path, monkeypatch
):
    """Nothing about linking a file may cost him the message itself."""
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd=str(cli.config.REPOS[0].path), task_folder="f",
    )

    def explode(cwd, relpath):
        raise OSError("git is on fire")

    monkeypatch.setattr(cli.publish, "publish", explode)
    sent = []
    monkeypatch.setattr(
        cli.telegram, "send", lambda chat_id, text, **kw: sent.append(text)
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert sent[0].startswith("done\n")
    assert "git is on fire" in sent[0]


def test_notify_still_sends_when_the_repo_has_no_github_link(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd="/somewhere/else", task_folder="2026-08-05-thing",
    )

    monkeypatch.setattr(
        cli.publish, "publish", published(ok=False, detail="no such file")
    )
    sent = []
    monkeypatch.setattr(
        cli.telegram, "send",
        lambda chat_id, text, **kw: sent.append(text),
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert sent[0].startswith("done\n2026-08-05-thing/report.md (no GitHub link:")


def test_notify_with_a_file_but_no_task_folder_never_links_to_none(
    tmp_path, monkeypatch
):
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd=str(cli.config.REPOS[0].path), task_folder=None,
    )

    monkeypatch.setattr(cli.publish, "publish", published())
    sent = []
    monkeypatch.setattr(
        cli.telegram, "send",
        lambda chat_id, text, **kw: sent.append(text),
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert "None" not in sent[0]
    assert "report.md" in sent[0]


def test_resolve_notify_args_takes_the_id_from_the_environment():
    assert cli.resolve_notify_args("all done", None, {"CONCIERGE_JOB_ID": "A3"}) == \
        ("A3", "all done")


def test_resolve_notify_args_prefers_an_explicit_id():
    assert cli.resolve_notify_args("B7", "done", {"CONCIERGE_JOB_ID": "A3"}) == \
        ("B7", "done")


def test_resolve_notify_args_errors_when_the_id_is_nowhere():
    with pytest.raises(typer.BadParameter, match="CONCIERGE_JOB_ID"):
        cli.resolve_notify_args("all done", None, {})


def test_resolve_notify_args_errors_without_any_text():
    with pytest.raises(typer.BadParameter, match="no message text"):
        cli.resolve_notify_args(None, None, {"CONCIERGE_JOB_ID": "A3"})


def test_respawn_starts_a_fresh_job_from_the_stored_brief(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="orphaned", chat_id="9", title="calibre",
        cwd=str(cli.config.REPOS[0].path), task_folder="2026-08-05-thing",
        brief="clean the epubs", root_message_id=4471,
    )

    calls = []

    def fake_spawn(**kw):
        calls.append(kw)
        registry.upsert("B7", kw["state_path"], id="B7", status="running")
        return registry.load(kw["state_path"])["B7"]

    monkeypatch.setattr(cli.spawn_mod, "spawn_job", fake_spawn)

    fresh = cli.respawn("A3", state_path=state)

    assert fresh["id"] == "B7"
    assert calls[0]["brief"].endswith("clean the epubs")
    assert calls[0]["brief"].startswith("You are resuming an interrupted job.")
    assert calls[0]["title"] == "calibre"
    assert calls[0]["cwd"] == str(cli.config.REPOS[0].path)
    assert calls[0]["task_folder"] == "2026-08-05-thing"
    assert calls[0]["chat_id"] == "9"
    assert registry.load(state)["A3"]["status"] == "respawned"


def test_respawn_refuses_a_job_with_no_stored_brief(tmp_path):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="orphaned", chat_id="9",
                    title="t", cwd=str(cli.config.REPOS[0].path))
    with pytest.raises(ValueError, match="no brief or cwd stored"):
        cli.respawn("A3", state_path=state)


def test_respawn_raises_for_an_unknown_job(tmp_path):
    with pytest.raises(KeyError, match="ZZ"):
        cli.respawn("ZZ", state_path=tmp_path / "jobs.json")


def test_format_status_includes_the_remote_control_url():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    job = {
        "id": "A3", "title": "calibre cleanup", "status": "running",
        "opened_at": "2026-08-05T14:00:00+00:00",
        "rc_url": "https://claude.ai/code/session_abc",
    }
    out = cli.format_status(job, now)
    assert "[A3] calibre cleanup — running · 1h" in out
    assert out.splitlines()[-1] == "https://claude.ai/code/session_abc"


def test_format_status_falls_back_when_there_is_no_rc_url():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    job = {
        "id": "A3", "title": "calibre cleanup", "status": "running",
        "opened_at": "2026-08-05T14:00:00+00:00",
        "rc_url": None,
    }
    out = cli.format_status(job, now)
    assert "None" not in out
    assert 'find it as "[A3] calibre cleanup" in claude.ai/code' in out


def test_format_status_reports_a_done_job_rather_than_treating_it_as_missing():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    job = {
        "id": "Z9", "title": "old job", "status": "done",
        "opened_at": "2026-08-01T09:00:00+00:00",
        "rc_url": "https://claude.ai/code/session_old",
    }
    out = cli.format_status(job, now)
    assert "[Z9] old job — done ·" in out
    assert "https://claude.ai/code/session_old" in out


# --- send --------------------------------------------------------------------


class FakeClaudePane:
    """A job's pane, as far as typing into it goes.

    Text sits in the input box until an Enter arrives in a call of its own. An
    Enter in the same `send-keys` call as the text is swallowed into it, which
    is what left E6's message unsent on 2026-09-13. `swallow` makes Claude drop
    that many lone Enters as well, for the retry.
    """

    def __init__(self, box="", swallow=0, starting=0):
        self.box, self.swallow, self.buffer, self.submitted = box, swallow, "", []
        # Captures that show no input box yet; keys typed meanwhile are lost.
        self.starting = starting

    def __call__(self, argv):
        import subprocess

        out = ""
        if argv[1] == "capture-pane" and self.starting:
            self.starting -= 1
        elif argv[1] == "set-buffer":
            self.buffer = argv[-1]
        elif argv[1] == "paste-buffer":
            self.box += "" if self.starting else self.buffer
        elif argv[1] == "send-keys":
            keys = [k for k in argv[4:] if k != "-l"]
            if keys != ["Enter"]:
                self.box += "".join(k for k in keys if k != "Enter")
            elif self.swallow:
                self.swallow -= 1
            elif self.box:
                self.submitted.append(self.box)
                self.box = ""
        elif argv[1] == "capture-pane":
            out = f"❯\xa0{self.box}\n" if self.box else "❯\xa0\x1b[2mold\x1b[0m\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


LONG = "Thursday is quote day, fix the respond loop. " * 12


def _job(tmp_path):
    state = tmp_path / "jobs.json"
    registry.upsert("E6", state, id="E6", status="waiting", chat_id="9",
                    title="t", tmux_window="E6")
    return state


def test_send_submits_a_long_message_even_when_an_enter_is_dropped(tmp_path):
    pane = FakeClaudePane(swallow=1)

    cli.send("E6", LONG, state_path=_job(tmp_path), runner=pane,
             sleeper=lambda s: None)

    assert pane.submitted == [LONG]
    assert pane.box == ""


def test_send_waits_for_a_job_that_is_still_starting(tmp_path):
    pane = FakeClaudePane(starting=3)

    cli.send("E6", LONG, state_path=_job(tmp_path), runner=pane,
             sleeper=lambda s: None)

    assert pane.submitted == [LONG]


def test_send_fails_loudly_when_the_message_never_leaves_the_box(tmp_path):
    pane = FakeClaudePane(swallow=99)

    with pytest.raises(RuntimeError, match="not delivered"):
        cli.send("E6", LONG, state_path=_job(tmp_path), runner=pane,
                 sleeper=lambda s: None)
    assert pane.submitted == []


def test_send_will_not_press_enter_on_text_already_in_the_box(tmp_path):
    pane = FakeClaudePane(box="half-typed reply")

    with pytest.raises(RuntimeError, match="half-typed reply"):
        cli.send("E6", LONG, state_path=_job(tmp_path), runner=pane,
                 sleeper=lambda s: None)
    assert pane.submitted == []
    assert pane.box == "half-typed reply"
