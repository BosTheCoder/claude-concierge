from datetime import datetime, timezone
import pytest
from concierge import cli, config, registry


def test_github_link_for_the_tasks_repo():
    link = cli.github_link(
        str(config.TASKS_REPO), "2026-08-05-x", "report.md"
    )
    assert link == (
        "https://github.com/BosTheCoder/tasks/blob/main/2026-08-05-x/report.md"
    )


def test_github_link_for_the_property_repo():
    link = cli.github_link(str(config.NPM_REPO), "2026-08-05-y", "notes.md")
    assert link == (
        "https://github.com/BosTheCoder/nyakundi-property-management"
        "/blob/main/2026-08-05-y/notes.md"
    )


def test_github_link_rejects_an_unknown_repo():
    with pytest.raises(ValueError, match="unknown repo"):
        cli.github_link("/tmp/somewhere", "f", "x.md")


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
            (chat_id, text, reply_to)
        ),
    )

    cli.notify("A3", "done", file=None, status="done", state_path=state)

    chat_id, text, reply_to = sent[0]
    assert chat_id == "999"
    assert text.startswith("[A3] ")
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
