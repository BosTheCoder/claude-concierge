from pathlib import Path
from concierge import registry, spawn


def test_claude_argv_names_the_session_with_the_job_id():
    argv = spawn.claude_argv("A3", "calibre cleanup", "do the thing", Path("/p/job.md"))
    assert argv[0] == "claude"
    assert "--remote-control" in argv
    assert argv[argv.index("--remote-control") + 1] == "[A3] calibre cleanup"


def test_claude_argv_passes_the_prompt_file_and_brief():
    argv = spawn.claude_argv("A3", "t", "the brief", Path("/p/job.md"))
    assert argv[argv.index("--append-system-prompt-file") + 1] == "/p/job.md"
    assert argv[-1] == "the brief"


def test_claude_argv_never_skips_permissions():
    argv = spawn.claude_argv("A3", "t", "b", Path("/p/job.md"))
    assert "--dangerously-skip-permissions" not in argv


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
    def __init__(self):
        self.windows = []

    def new_window(self, session, window, shell_command, **kw):
        self.windows.append((session, window, shell_command))

    def build_shell_command(self, cwd, argv):
        return f"cd {cwd} && exec " + " ".join(argv)


def test_spawn_job_registers_the_job(tmp_path):
    state = tmp_path / "jobs.json"
    tmux = FakeTmux()

    job = spawn.spawn_job(
        title="calibre cleanup",
        brief="clean the epubs",
        cwd="/home/bosire/projects/personal/tasks",
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
    assert stored["cwd"] == "/home/bosire/projects/personal/tasks"


def test_spawn_job_opens_a_tmux_window_named_after_the_job(tmp_path):
    tmux = FakeTmux()
    job = spawn.spawn_job(
        title="t", brief="b", cwd="/x", chat_id="1",
        state_path=tmp_path / "jobs.json", tmux=tmux,
        poller=lambda w: None,
    )
    assert tmux.windows[0][1] == job["id"]


def test_spawn_job_records_the_job_even_when_the_url_never_appears(tmp_path):
    state = tmp_path / "jobs.json"
    job = spawn.spawn_job(
        title="t", brief="b", cwd="/x", chat_id="1",
        state_path=state, tmux=FakeTmux(), poller=lambda w: None,
    )
    assert registry.load(state)[job["id"]]["rc_url"] is None
    assert registry.load(state)[job["id"]]["status"] == "running"
