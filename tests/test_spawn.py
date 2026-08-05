from pathlib import Path
import pytest
from concierge import config, registry, spawn

TASKS = str(config.TASKS_REPO)
NPM = str(config.NPM_REPO)


def test_claude_argv_names_the_session_with_the_job_id():
    argv = spawn.claude_argv("A3", "calibre cleanup", "do the thing", Path("/p/job.md"))
    assert argv[0] == "claude"
    assert "--remote-control" in argv
    assert argv[argv.index("--remote-control") + 1] == "[A3] calibre cleanup"


def test_claude_argv_passes_the_prompt_file_and_brief():
    argv = spawn.claude_argv("A3", "t", "the brief", Path("/p/job.md"))
    assert argv[argv.index("--append-system-prompt-file") + 1] == "/p/job.md"
    assert argv[-1].endswith("the brief")


def test_claude_argv_states_the_job_id_at_the_top_of_the_brief():
    argv = spawn.claude_argv("A3", "t", "the brief", Path("/p/job.md"))
    assert argv[-1] == "Your job id is A3.\n\nthe brief"


def test_claude_argv_runs_in_the_configured_permission_mode():
    argv = spawn.claude_argv("A3", "t", "b", Path("/p/job.md"))
    assert argv[argv.index("--permission-mode") + 1] == config.PERMISSION_MODE


def test_claude_argv_uses_the_mode_flag_not_the_dangerous_one():
    """--permission-mode is per-session and reversible; the other flag is neither."""
    argv = spawn.claude_argv("A3", "t", "b", Path("/p/job.md"))
    assert "--dangerously-skip-permissions" not in argv


def test_session_id_comes_from_the_last_path_segment():
    assert spawn.session_id_from_url("https://claude.ai/code/session_01ABC") == \
        "session_01ABC"


def test_session_id_is_none_without_a_url():
    assert spawn.session_id_from_url(None) is None


def test_resolve_cwd_accepts_both_permitted_repos():
    assert spawn.resolve_cwd(TASKS) == config.TASKS_REPO.resolve()
    assert spawn.resolve_cwd(NPM) == config.NPM_REPO.resolve()


def test_resolve_cwd_expands_a_tilde_path():
    assert spawn.resolve_cwd("~/projects/personal/tasks") == config.TASKS_REPO.resolve()


def test_resolve_cwd_rejects_anything_else():
    with pytest.raises(ValueError, match="not a permitted repo"):
        spawn.resolve_cwd("/x")


def test_poll_for_rc_url_returns_as_soon_as_it_appears():
    frames = ["starting", "starting", "url: https://claude.ai/code/session_x"]
    slept = []
    url = spawn.poll_for_rc_url(
        "A3",
        attempts=5,
        capture=lambda w: frames.pop(0),
        sleeper=slept.append,
    )
    assert url == "https://claude.ai/code/session_x"
    assert len(slept) == 2


def test_poll_for_rc_url_gives_up_and_returns_none():
    url = spawn.poll_for_rc_url(
        "A3", attempts=3, capture=lambda w: "nothing", sleeper=lambda s: None
    )
    assert url is None


class FakeTmux:
    def __init__(self, window_survives=True):
        self.windows = []
        self.window_survives = window_survives

    def new_window(self, session, window, shell_command, **kw):
        self.windows.append((session, window, shell_command))

    def list_windows(self, session, **kw):
        if not self.window_survives:
            return ["0"]
        return ["0"] + [w for _, w, _ in self.windows]

    def build_shell_command(self, cwd, argv, env=None):
        prefix = "".join(f"{k}={v} " for k, v in (env or {}).items())
        return f"cd {cwd} && {prefix}exec " + " ".join(argv)


class ExplodingTmux(FakeTmux):
    def new_window(self, session, window, shell_command, **kw):
        raise RuntimeError("tmux new-window failed: bad session name")


def test_spawn_job_registers_the_job(tmp_path):
    state = tmp_path / "jobs.json"
    tmux = FakeTmux()

    job = spawn.spawn_job(
        title="calibre cleanup",
        brief="clean the epubs",
        cwd=TASKS,
        chat_id="999",
        root_message_id=4471,
        state_path=state,
        tmux=tmux,
        poller=lambda w: "https://claude.ai/code/session_x",
    )

    stored = registry.load(state)[job["id"]]
    assert stored["status"] == "running"
    assert stored["chat_id"] == "999"
    assert stored["root_message_id"] == 4471
    assert stored["rc_url"] == "https://claude.ai/code/session_x"
    assert stored["cwd"] == TASKS


def test_spawn_job_puts_the_job_id_in_the_windows_environment(tmp_path):
    tmux = FakeTmux()
    job = spawn.spawn_job(
        title="t", brief="b", cwd=TASKS, chat_id="1",
        state_path=tmp_path / "jobs.json", tmux=tmux,
        poller=lambda w: None,
    )
    assert f"CONCIERGE_JOB_ID={job['id']} exec" in tmux.windows[0][2]


def test_spawn_job_stores_the_brief_and_session_id_for_a_respawn(tmp_path):
    state = tmp_path / "jobs.json"
    job = spawn.spawn_job(
        title="t", brief="the original brief", cwd=TASKS, chat_id="1",
        state_path=state, tmux=FakeTmux(),
        poller=lambda w: "https://claude.ai/code/session_abc",
    )
    stored = registry.load(state)[job["id"]]
    assert stored["brief"] == "the original brief"
    assert stored["claude_session_id"] == "session_abc"


def test_spawn_job_stores_no_session_id_without_a_url(tmp_path):
    state = tmp_path / "jobs.json"
    job = spawn.spawn_job(
        title="t", brief="b", cwd=TASKS, chat_id="1",
        state_path=state, tmux=FakeTmux(), poller=lambda w: None,
    )
    assert registry.load(state)[job["id"]]["claude_session_id"] is None


def test_spawn_job_remembers_the_chat_for_supervisor_alerts(tmp_path):
    state = tmp_path / "jobs.json"
    spawn.spawn_job(
        title="t", brief="b", cwd=TASKS, chat_id="777",
        state_path=state, tmux=FakeTmux(), poller=lambda w: None,
    )
    assert registry.last_chat(state) == "777"


def test_spawn_job_opens_a_tmux_window_named_after_the_job(tmp_path):
    tmux = FakeTmux()
    job = spawn.spawn_job(
        title="t", brief="b", cwd=TASKS, chat_id="1",
        state_path=tmp_path / "jobs.json", tmux=tmux,
        poller=lambda w: None,
    )
    assert tmux.windows[0][1] == job["id"]


def test_spawn_job_records_the_job_even_when_the_url_never_appears(tmp_path):
    state = tmp_path / "jobs.json"
    job = spawn.spawn_job(
        title="t", brief="b", cwd=TASKS, chat_id="1",
        state_path=state, tmux=FakeTmux(), poller=lambda w: None,
    )
    assert registry.load(state)[job["id"]]["rc_url"] is None
    assert registry.load(state)[job["id"]]["status"] == "running"


def test_spawn_job_refuses_a_cwd_outside_the_two_repos(tmp_path):
    state = tmp_path / "jobs.json"
    tmux = FakeTmux()
    with pytest.raises(ValueError, match="not a permitted repo"):
        spawn.spawn_job(
            title="t", brief="b", cwd="/x", chat_id="1",
            state_path=state, tmux=tmux, poller=lambda w: None,
        )
    assert tmux.windows == []
    assert registry.load(state) == {}


def test_spawn_job_fails_loudly_when_the_window_died_on_startup(tmp_path):
    state = tmp_path / "jobs.json"
    with pytest.raises(RuntimeError, match="died immediately"):
        spawn.spawn_job(
            title="t", brief="b", cwd=TASKS, chat_id="1",
            state_path=state, tmux=FakeTmux(window_survives=False),
            poller=lambda w: None,
        )
    stored = list(registry.load(state).values())[0]
    assert stored["status"] == "failed"


def test_spawn_job_leaves_no_registry_entry_when_tmux_raises(tmp_path):
    state = tmp_path / "jobs.json"
    with pytest.raises(RuntimeError, match="tmux new-window failed"):
        spawn.spawn_job(
            title="t", brief="b", cwd=TASKS, chat_id="1",
            state_path=state, tmux=ExplodingTmux(), poller=lambda w: None,
        )
    assert registry.load(state) == {}
