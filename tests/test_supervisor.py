from datetime import datetime, timedelta, timezone
import pytest
from concierge import registry, supervisor


def test_blocking_env_finds_the_remote_control_killers():
    found = supervisor.blocking_env({"CLAUDE_CODE_OAUTH_TOKEN": "x", "PATH": "/bin"})
    assert found == ["CLAUDE_CODE_OAUTH_TOKEN"]


def test_blocking_env_ignores_an_empty_value():
    assert supervisor.blocking_env({"DISABLE_TELEMETRY": ""}) == []


def test_blocking_env_clean_environment_returns_nothing():
    assert supervisor.blocking_env({"PATH": "/bin", "HOME": "/home/bosire"}) == []


def test_concierge_argv_enables_the_channel_and_remote_control():
    argv = supervisor.concierge_argv()
    assert argv[argv.index("--channels") + 1] == \
        "plugin:telegram@claude-plugins-official"
    assert argv[argv.index("--remote-control") + 1] == "concierge"
    assert "--dangerously-skip-permissions" not in argv


def test_reconcile_flags_running_jobs_whose_window_is_gone():
    jobs = {
        "A3": {"status": "running", "tmux_window": "A3"},
        "B7": {"status": "running", "tmux_window": "B7"},
        "C1": {"status": "done", "tmux_window": "C1"},
    }
    assert supervisor.reconcile(jobs, windows=["0", "A3"]) == ["B7"]


def test_reconcile_returns_nothing_when_every_window_is_present():
    jobs = {"A3": {"status": "running", "tmux_window": "A3"}}
    assert supervisor.reconcile(jobs, windows=["0", "A3"]) == []


def test_prunable_finds_old_terminal_jobs():
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    old = (now - timedelta(days=9)).isoformat()
    recent = (now - timedelta(days=2)).isoformat()
    jobs = {
        "A1": {"status": "done", "last_update": old},
        "A2": {"status": "done", "last_update": recent},
        "A3": {"status": "running", "last_update": old},
    }
    assert supervisor.prunable(jobs, now) == ["A1"]


class FakeTmux:
    def __init__(self, alive):
        self.alive = alive
        self.started = []
        self.windows = ["0"]

    def has_session(self, session, **kw):
        return self.alive

    def list_windows(self, session, **kw):
        return self.windows

    def new_session(self, session, window, shell_command, **kw):
        self.started.append(shell_command)
        self.alive = True

    def build_shell_command(self, cwd, argv):
        return f"cd {cwd} && exec " + " ".join(argv)


class FailingTmux(FakeTmux):
    def new_session(self, session, window, shell_command, **kw):
        raise RuntimeError("tmux new-session failed: no server running")


def test_ensure_up_refuses_to_start_with_a_blocking_env_var(tmp_path):
    notes = []
    result = supervisor.ensure_up(
        env={"CLAUDE_CODE_OAUTH_TOKEN": "x"},
        tmux=FakeTmux(alive=False),
        state_path=tmp_path / "jobs.json",
        notifier=notes.append,
    )
    assert result == "blocked"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in notes[0]


def test_ensure_up_is_silent_when_the_concierge_is_alive(tmp_path):
    notes = []
    tmux = FakeTmux(alive=True)
    result = supervisor.ensure_up(
        env={}, tmux=tmux, state_path=tmp_path / "jobs.json",
        notifier=notes.append,
    )
    assert result == "healthy"
    assert notes == []
    assert tmux.started == []


def test_ensure_up_starts_the_concierge_when_it_is_gone(tmp_path):
    tmux = FakeTmux(alive=False)
    result = supervisor.ensure_up(
        env={}, tmux=tmux, state_path=tmp_path / "jobs.json",
        notifier=lambda m: None,
    )
    assert result == "started"
    assert "--channels" in tmux.started[0]


def test_ensure_up_reports_jobs_orphaned_by_the_restart(tmp_path):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="running",
                    tmux_window="A3", title="calibre", chat_id="9")
    notes = []

    supervisor.ensure_up(
        env={}, tmux=FakeTmux(alive=False), state_path=state,
        notifier=notes.append,
    )

    assert any("A3" in n for n in notes)
    assert registry.load(state)["A3"]["status"] == "orphaned"


def test_ensure_up_stays_healthy_even_with_a_blocking_env_var_set(tmp_path):
    notes = []
    tmux = FakeTmux(alive=True)
    result = supervisor.ensure_up(
        env={"CLAUDE_CODE_OAUTH_TOKEN": "x"},
        tmux=tmux,
        state_path=tmp_path / "jobs.json",
        notifier=notes.append,
    )
    assert result == "healthy"
    assert notes == []
    assert tmux.started == []


def test_ensure_up_notifies_and_reraises_when_tmux_start_fails(tmp_path):
    notes = []
    with pytest.raises(RuntimeError, match="tmux new-session failed"):
        supervisor.ensure_up(
            env={},
            tmux=FailingTmux(alive=False),
            state_path=tmp_path / "jobs.json",
            notifier=notes.append,
        )
    assert any("failed to start" in n for n in notes)


def test_default_notifier_honours_an_injected_state_path(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="running", chat_id="9")

    sent = []
    monkeypatch.setattr(
        supervisor.telegram, "send",
        lambda chat_id, text, **kw: sent.append(chat_id),
    )

    supervisor._default_notifier("hello", state)

    assert sent == ["9"]
