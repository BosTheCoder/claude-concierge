import json
import pytest
from concierge import registry


@pytest.fixture
def state(tmp_path):
    return tmp_path / "jobs.json"


def test_load_missing_file_returns_empty(state):
    assert registry.load(state) == {}


def test_save_then_load_roundtrips(state):
    registry.save({"A3": {"title": "x", "status": "running"}}, state)
    assert registry.load(state)["A3"]["title"] == "x"


def test_save_is_atomic_leaving_no_temp_files(state):
    registry.save({"A3": {"status": "done"}}, state)
    leftovers = [p.name for p in state.parent.iterdir() if p.name != "jobs.json"]
    assert leftovers == []


def test_allocate_id_avoids_active_jobs(state):
    jobs = {"A0": {"status": "running"}, "A1": {"status": "waiting"}}
    assert registry.allocate_id(jobs) not in {"A0", "A1"}


def test_allocate_id_may_reuse_a_finished_id(state):
    # 240 ids exist; only finished ones are reusable, so with every id but one
    # taken by an active job the allocator must return that one.
    jobs = {}
    for letter in registry.config.ID_LETTERS:
        for digit in registry.config.ID_DIGITS:
            jobs[f"{letter}{digit}"] = {"status": "running"}
    jobs["M7"]["status"] = "done"
    assert registry.allocate_id(jobs) == "M7"


def test_allocate_id_raises_when_all_ids_active(state):
    jobs = {
        f"{letter}{digit}": {"status": "running"}
        for letter in registry.config.ID_LETTERS
        for digit in registry.config.ID_DIGITS
    }
    with pytest.raises(RuntimeError, match="no free job id"):
        registry.allocate_id(jobs)


def test_upsert_creates_and_merges(state):
    registry.upsert("B7", state, title="chase BG", status="running")
    registry.upsert("B7", state, status="done")
    job = registry.load(state)["B7"]
    assert job["title"] == "chase BG"
    assert job["status"] == "done"


def test_upsert_stamps_last_update(state):
    registry.upsert("B7", state, status="running")
    assert "last_update" in registry.load(state)["B7"]


def test_active_filters_terminal_statuses(state):
    jobs = {
        "A1": {"status": "running"},
        "A2": {"status": "waiting"},
        "A3": {"status": "done"},
        "A4": {"status": "orphaned"},
    }
    assert set(registry.active(jobs)) == {"A1", "A2"}
