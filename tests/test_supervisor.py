import json
from datetime import datetime, timedelta, timezone
import pytest
from concierge import config, registry, supervisor


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


def test_concierge_argv_runs_in_the_configured_permission_mode():
    argv = supervisor.concierge_argv()
    assert argv[argv.index("--permission-mode") + 1] == config.PERMISSION_MODE


def test_concierge_argv_turns_the_telegram_plugin_on_for_itself_only():
    """Disabled at user scope so no other session can steal the bot's poller."""
    argv = supervisor.concierge_argv()
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["enabledPlugins"][config.TELEGRAM_PLUGIN] is True


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
    def __init__(self, alive, window_0_command="claude"):
        self.alive = alive
        self.started = []
        self.windows = ["0"]
        self.window_0_command = window_0_command
        self.killed = []

    def has_session(self, session, **kw):
        return self.alive

    def window_command(self, session, window, **kw):
        if not self.alive or window not in self.windows:
            return None
        return self.window_0_command if window == "0" else "claude"

    def list_windows(self, session, **kw):
        return self.windows

    def new_session(self, session, window, shell_command, **kw):
        self.started.append(shell_command)
        self.alive = True
        self.windows = [window]
        self.window_0_command = "claude"

    def new_window(self, session, window, shell_command, **kw):
        self.started.append(shell_command)
        if window not in self.windows:
            self.windows.append(window)
        if window == "0":
            self.window_0_command = "claude"

    def kill_window(self, session, window, **kw):
        self.killed.append(window)
        if window in self.windows:
            self.windows.remove(window)

    def build_shell_command(self, cwd, argv, env=None):
        return f"cd {cwd} && exec " + " ".join(argv)


class FailingTmux(FakeTmux):
    def new_session(self, session, window, shell_command, **kw):
        raise RuntimeError("tmux new-session failed: no server running")


class DuplicateSessionTmux(FakeTmux):
    def new_session(self, session, window, shell_command, **kw):
        raise RuntimeError("tmux new-session failed: duplicate session: concierge")


def test_concierge_alive_is_false_without_a_session():
    assert supervisor.concierge_alive(FakeTmux(alive=False)) is False


def test_concierge_alive_is_true_when_window_0_runs_claude():
    assert supervisor.concierge_alive(FakeTmux(alive=True)) is True


def test_concierge_alive_is_false_when_window_0_dropped_to_a_shell():
    # A live job keeps the session up; window 0's claude is gone anyway.
    tmux = FakeTmux(alive=True, window_0_command="zsh")
    assert supervisor.concierge_alive(tmux) is False


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


def test_ensure_up_replaces_window_0_when_jobs_keep_the_session_alive(tmp_path):
    # Session up, window 0 dead: new_session would fail with 'duplicate
    # session', so the fix is a new window inside the session we already have.
    tmux = FakeTmux(alive=True, window_0_command="zsh")
    tmux.windows = ["0", "A3"]

    result = supervisor.ensure_up(
        env={}, tmux=tmux, state_path=tmp_path / "jobs.json",
        notifier=lambda m: None,
    )

    assert result == "started"
    assert tmux.killed == ["0"]
    assert "--channels" in tmux.started[0]
    assert "A3" in tmux.windows


def test_ensure_up_treats_a_duplicate_session_race_as_healthy(tmp_path):
    notes = []
    result = supervisor.ensure_up(
        env={}, tmux=DuplicateSessionTmux(alive=False),
        state_path=tmp_path / "jobs.json", notifier=notes.append,
    )
    assert result == "healthy"
    assert notes == []


def test_ensure_up_kills_the_window_of_a_pruned_job(tmp_path):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="done", tmux_window="A3",
                    title="old", chat_id="9")
    old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    jobs = registry.load(state)
    jobs["A3"]["last_update"] = old
    registry.save(jobs, state)

    tmux = FakeTmux(alive=False)
    supervisor.ensure_up(
        env={}, tmux=tmux, state_path=state, notifier=lambda m: None,
    )

    assert "A3" in tmux.killed
    assert registry.load(state) == {}


def test_orphan_message_tells_the_user_how_to_respawn(tmp_path):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="running",
                    tmux_window="A3", title="calibre", chat_id="9")
    notes = []

    supervisor.ensure_up(
        env={}, tmux=FakeTmux(alive=False), state_path=state,
        notifier=notes.append,
    )

    assert "respawn A3" in notes[0]
    assert "Reply to resume it" not in notes[0]


def test_alert_destination_uses_the_most_recent_job_only(tmp_path):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="done", chat_id="111")
    registry.upsert("B7", state, id="B7", status="running", chat_id="222")
    assert supervisor.alert_destination(state) == "222"


def test_alert_destination_falls_back_to_the_last_chat(tmp_path):
    state = tmp_path / "jobs.json"
    registry.remember_chat("999", state)
    assert supervisor.alert_destination(state) == "999"


def test_alert_destination_is_none_on_a_completely_fresh_install(tmp_path):
    assert supervisor.alert_destination(tmp_path / "jobs.json") is None


def test_default_notifier_reaches_the_last_chat_on_an_empty_registry(
    tmp_path, monkeypatch
):
    state = tmp_path / "jobs.json"
    registry.remember_chat("999", state)

    sent = []
    monkeypatch.setattr(
        supervisor.telegram, "send",
        lambda chat_id, text, **kw: sent.append(chat_id),
    )

    supervisor._default_notifier("concierge NOT started", state)

    assert sent == ["999"]


def test_default_notifier_never_broadcasts_to_every_chat(tmp_path, monkeypatch):
    state = tmp_path / "jobs.json"
    registry.upsert("A3", state, id="A3", status="done", chat_id="111")
    registry.upsert("B7", state, id="B7", status="running", chat_id="222")

    sent = []
    monkeypatch.setattr(
        supervisor.telegram, "send",
        lambda chat_id, text, **kw: sent.append(chat_id),
    )

    supervisor._default_notifier("hello", state)

    assert sent == ["222"]


def test_default_notifier_prints_to_stderr_even_with_no_destination(
    tmp_path, capsys
):
    supervisor._default_notifier("concierge failed to start", tmp_path / "jobs.json")
    assert "concierge failed to start" in capsys.readouterr().err
