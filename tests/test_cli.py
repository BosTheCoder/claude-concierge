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


def test_notify_with_a_file_appends_the_github_link(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd=str(cli.config.TASKS_REPO), task_folder="2026-08-05-thing",
    )

    sent = []
    monkeypatch.setattr(
        cli.telegram, "send",
        lambda chat_id, text, **kw: sent.append(text),
    )

    cli.notify("A3", "done", file="report.md", status=None, state_path=state)

    assert sent[0] == (
        "done\nhttps://github.com/BosTheCoder/tasks/blob/main/"
        "2026-08-05-thing/report.md"
    )


def test_notify_still_sends_when_the_repo_has_no_github_link(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert(
        "A3", state, id="A3", status="running", chat_id="9", title="t",
        cwd="/somewhere/else", task_folder="2026-08-05-thing",
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
        cwd=str(cli.config.TASKS_REPO), task_folder=None,
    )

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
        cwd=str(cli.config.TASKS_REPO), task_folder="2026-08-05-thing",
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
    assert calls[0]["cwd"] == str(cli.config.TASKS_REPO)
    assert calls[0]["task_folder"] == "2026-08-05-thing"
    assert calls[0]["chat_id"] == "9"
    assert registry.load(state)["A3"]["status"] == "respawned"


def test_respawn_refuses_a_job_with_no_stored_brief(tmp_path):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="orphaned", chat_id="9",
                    title="t", cwd=str(cli.config.TASKS_REPO))
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
