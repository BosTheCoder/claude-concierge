import json
from datetime import datetime, timezone

from concierge import rc

NOW = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
# Old enough to be past MIN_AGE_SECONDS in every fixture below.
STARTED = int((NOW.timestamp() - 3600) * 1000)

# Captured from the live concierge panes on 2026-08-11 with `capture-pane -pe`.
# An empty box still draws the last message as a dim SGR-2 ghost, which is the
# one thing that makes this parser necessary.
EMPTY_BOX = "\x1b[39m❯\xa0\x1b[2m/status E5\x1b[0m"
UNSENT_TEXT = (
    "\x1b[39m❯\xa0The user says: please post those reviews. "
    "Go ahead and post them."
)
QUEUED_THEN_EMPTY = (
    "\x1b[38;5;239m\x1b[48;5;237m❯\x1b[39m \x1b[38;5;231m/compact\x1b[39m\n"
    "\x1b[39m❯\xa0\x1b[2mtell AK the reviews are up\x1b[0m"
)


# --- the parser everything else rests on ------------------------------------


def test_dim_ghost_of_the_last_message_is_not_pending_text():
    assert rc.input_box_content(EMPTY_BOX) is None


def test_real_unsent_text_is_reported():
    assert rc.input_box_content(UNSENT_TEXT) == (
        "The user says: please post those reviews. Go ahead and post them."
    )


def test_a_queued_message_above_an_empty_box_does_not_count_as_pending():
    # Two prompt markers on screen: the queued /compact and the real box.
    assert rc.input_box_content(QUEUED_THEN_EMPTY) is None


def test_no_prompt_on_screen_reads_as_nothing_pending():
    assert rc.input_box_content("just some scrollback\nand more") is None


# --- which sessions are actually down ---------------------------------------


def write_session(directory, pid, **overrides):
    data = {
        "pid": pid,
        "procStart": str(pid * 100),
        "startedAt": STARTED,
        "name": f"session-{pid}",
        "cwd": "/home/bosire/projects/personal/tasks",
        "tmux": f"concierge:@0.%{pid}",
        "entrypoint": "cli",
        "bridgeSessionId": None,
    }
    data.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.json").write_text(json.dumps(data))
    return data


def write_subagent(directory, pid, pane):
    """A claude-mem observer: SDK-spawned, sharing its parent's pane, and with
    no bridgeSessionId key at all. Shape copied from a real row on 2026-08-11.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "procStart": str(pid * 100),
                "startedAt": STARTED,
                "name": f"observer-sessions-{pid}",
                "cwd": "/home/bosire/.claude-mem/observer-sessions",
                "entrypoint": "sdk-cli",
                "tmux": pane,
            }
        )
    )


def proc_start_for(*pids):
    """Only these pids exist, each with the procStart write_session recorded."""
    return lambda pid: str(pid * 100) if pid in pids else None


def test_a_null_bridge_on_a_live_process_is_a_disconnection(tmp_path):
    write_session(tmp_path, 100)
    (session,) = rc.load_sessions(tmp_path)
    assert session.connected is False
    assert rc.is_running(session, proc_start_for(100)) is True


def test_a_session_with_a_bridge_id_is_connected(tmp_path):
    write_session(tmp_path, 100, bridgeSessionId="session_01ABC")
    (session,) = rc.load_sessions(tmp_path)
    assert session.connected is True


def test_a_registry_row_whose_process_is_gone_is_not_live(tmp_path):
    write_session(tmp_path, 100)
    (session,) = rc.load_sessions(tmp_path)
    assert rc.is_running(session, proc_start_for()) is False


def test_a_recycled_pid_is_not_mistaken_for_the_old_session(tmp_path):
    write_session(tmp_path, 100)
    (session,) = rc.load_sessions(tmp_path)
    # pid 100 exists, but it started at a different time — different process.
    assert rc.is_running(session, lambda pid: "999999") is False


def test_unparseable_registry_rows_do_not_hide_the_rest(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "broken.json").write_text("{truncated")
    write_session(tmp_path, 100)
    assert [s.pid for s in rc.load_sessions(tmp_path)] == [100]


def test_pane_is_taken_from_the_tmux_field(tmp_path):
    write_session(tmp_path, 100, tmux="concierge:@3.%7")
    (session,) = rc.load_sessions(tmp_path)
    assert session.pane == "%7"


def test_a_pane_in_another_tmux_session_is_still_addressable(tmp_path):
    """Ad-hoc windows drop off the bridge exactly as concierge ones do."""
    write_session(tmp_path, 100, tmux="work:@0.%4")
    (session,) = rc.load_sessions(tmp_path)
    assert session.pane == "%4"
    assert session.tmux_session == "work"


def test_a_session_not_running_under_tmux_has_no_pane(tmp_path):
    write_session(tmp_path, 100, tmux=None)
    (session,) = rc.load_sessions(tmp_path)
    assert session.pane is None


# --- the sweep ---------------------------------------------------------------


class FakeTmux:
    def __init__(self, panes=None, commands=None):
        self.panes = panes or {}
        self.commands = commands or {}
        self.sent = []

    def pane_command(self, pane, **kw):
        return self.commands.get(pane, "claude")

    def capture_pane_escaped(self, pane, **kw):
        return self.panes.get(pane, EMPTY_BOX)

    def send_keys(self, pane, *keys, **kw):
        self.sent.append((pane, keys))
        return True


def run_sweep(tmp_path, tmux, *, pids, reconnect_on_wait=(), now=NOW, **kw):
    """Run a sweep whose wait reconnects the pids we say it should."""
    notes = []
    sessions = tmp_path / "sessions"

    def sleeper(_seconds):
        for pid in reconnect_on_wait:
            path = sessions / f"{pid}.json"
            data = json.loads(path.read_text())
            data["bridgeSessionId"] = f"session_{pid}"
            path.write_text(json.dumps(data))

    summary = rc.sweep(
        sessions_dir=sessions,
        tmux=tmux,
        sleeper=sleeper,
        proc_start=proc_start_for(*pids),
        notifier=notes.append,
        state_path=tmp_path / "rc.json",
        now=now,
        **kw,
    )
    return summary, notes


def test_sweep_reconnects_a_dead_pane_and_says_nothing(tmp_path):
    write_session(tmp_path / "sessions", 100)
    tmux = FakeTmux()

    summary, notes = run_sweep(
        tmp_path, tmux, pids=(100,), reconnect_on_wait=(100,)
    )

    assert tmux.sent == [("%100", ("/rc", "Enter"))]
    assert "reconnected" in summary
    assert notes == []


def test_sweep_never_presses_enter_on_a_box_holding_unsent_text(tmp_path):
    write_session(tmp_path / "sessions", 100)
    tmux = FakeTmux(panes={"%100": UNSENT_TEXT})

    summary, notes = run_sweep(tmp_path, tmux, pids=(100,))

    assert tmux.sent == []
    assert "unsent text" in summary
    assert "post those reviews" in notes[0]


def test_a_subagent_sharing_the_pane_is_not_a_disconnection(tmp_path):
    """The bug that typed /rc into a healthy window 0 on 2026-08-11."""
    sessions = tmp_path / "sessions"
    write_session(sessions, 100, bridgeSessionId="session_live", tmux="concierge:@0.%0")
    write_subagent(sessions, 200, "concierge:@0.%0")
    tmux = FakeTmux()

    summary, notes = run_sweep(tmp_path, tmux, pids=(100, 200))

    assert tmux.sent == []
    assert summary == "rc: all connected"
    assert notes == []


def test_a_subagent_on_a_genuinely_dead_pane_is_still_not_typed_into_twice(tmp_path):
    """Only the real session earns a /rc — one pane, one keystroke."""
    sessions = tmp_path / "sessions"
    write_session(sessions, 100, tmux="concierge:@0.%0")
    write_subagent(sessions, 200, "concierge:@0.%0")
    tmux = FakeTmux()

    run_sweep(tmp_path, tmux, pids=(100, 200), reconnect_on_wait=(100,))

    assert tmux.sent == [("%0", ("/rc", "Enter"))]


def test_sweep_reconnects_an_adhoc_window_outside_the_concierge_session(tmp_path):
    write_session(tmp_path / "sessions", 100, tmux="work:@1.%9")
    tmux = FakeTmux()

    summary, notes = run_sweep(
        tmp_path, tmux, pids=(100,), reconnect_on_wait=(100,)
    )

    assert tmux.sent == [("%9", ("/rc", "Enter"))]
    assert "reconnected" in summary
    assert notes == []


def test_a_pane_that_handed_its_terminal_to_a_shell_is_not_typed_into(tmp_path):
    """/rc there runs as a shell command. Seen on a real `status: shell` row."""
    write_session(tmp_path / "sessions", 100)
    tmux = FakeTmux(commands={"%100": "zsh"})

    summary, notes = run_sweep(tmp_path, tmux, pids=(100,))

    assert tmux.sent == []
    assert "pane not at the REPL" in summary
    assert "zsh" in notes[0]


def test_sweep_leaves_connected_sessions_alone(tmp_path):
    write_session(tmp_path / "sessions", 100, bridgeSessionId="session_live")
    tmux = FakeTmux()

    summary, notes = run_sweep(tmp_path, tmux, pids=(100,))

    assert tmux.sent == []
    assert summary == "rc: all connected"
    assert notes == []


def test_sweep_ignores_a_session_that_is_still_starting_up(tmp_path):
    just_started = int((NOW.timestamp() - 5) * 1000)
    write_session(tmp_path / "sessions", 100, startedAt=just_started)
    tmux = FakeTmux()

    summary, notes = run_sweep(tmp_path, tmux, pids=(100,))

    assert tmux.sent == []
    assert summary == "rc: all connected"


def test_sweep_reports_when_rc_does_not_bring_the_bridge_back(tmp_path):
    write_session(tmp_path / "sessions", 100)
    tmux = FakeTmux()

    summary, notes = run_sweep(tmp_path, tmux, pids=(100,))

    assert tmux.sent == [("%100", ("/rc", "Enter"))]
    assert "still down" in summary
    assert "needs restarting" in notes[0]


def test_sweep_cannot_type_into_a_session_outside_tmux_so_it_asks(tmp_path):
    write_session(tmp_path / "sessions", 100, tmux=None, name="tasks-86")
    tmux = FakeTmux()

    summary, notes = run_sweep(tmp_path, tmux, pids=(100,))

    assert tmux.sent == []
    assert "no tmux pane" in summary
    assert "tasks-86" in notes[0] and "/rc" in notes[0]


def test_an_unfixable_session_is_mentioned_once_and_then_left_alone(tmp_path):
    """It nags hourly otherwise, about the one thing the sweep cannot fix."""
    write_session(tmp_path / "sessions", 100, tmux=None)

    _, first = run_sweep(tmp_path, FakeTmux(), pids=(100,))
    later = NOW.replace(hour=NOW.hour + 6)
    _, second = run_sweep(tmp_path, FakeTmux(), pids=(100,), now=later)

    assert len(first) == 1
    assert second == []


# --- the report he can ask for from the phone -------------------------------


def test_report_says_all_clear_when_every_session_is_on_the_bridge(tmp_path):
    write_session(tmp_path, 100, bridgeSessionId="session_a")
    write_session(tmp_path, 200, bridgeSessionId="session_b")
    assert rc.report(sessions_dir=tmp_path, proc_start=proc_start_for(100, 200)) == (
        "2 sessions, all connected"
    )


def test_report_names_what_is_down_and_whether_it_is_reachable(tmp_path):
    write_session(tmp_path, 100, bridgeSessionId="session_a")
    write_session(tmp_path, 200, tmux="work:@0.%2", name="tasks-93")
    write_session(tmp_path, 300, tmux=None, name="npm-39")

    line = rc.report(sessions_dir=tmp_path, proc_start=proc_start_for(100, 200, 300))

    assert "3 sessions, 1 connected" in line
    assert "tasks-93 (tmux work)" in line
    assert "npm-39 (not in tmux — run /rc there)" in line


def test_report_ignores_subagents_sharing_a_healthy_pane(tmp_path):
    write_session(tmp_path, 100, bridgeSessionId="session_a", tmux="concierge:@0.%0")
    write_subagent(tmp_path, 200, "concierge:@0.%0")
    assert rc.report(sessions_dir=tmp_path, proc_start=proc_start_for(100, 200)) == (
        "1 session, all connected"
    )


def test_a_broken_sweep_does_not_stop_the_concierge_coming_up(monkeypatch):
    from concierge import cli

    monkeypatch.setattr(
        rc, "sweep", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gone"))
    )
    assert cli.run_rc().startswith("rc-error:")


def test_a_repeat_failure_inside_the_cooldown_stays_quiet(tmp_path):
    write_session(tmp_path / "sessions", 100)

    _, first = run_sweep(tmp_path, FakeTmux(), pids=(100,))
    _, second = run_sweep(tmp_path, FakeTmux(), pids=(100,))

    assert len(first) == 1
    assert second == []


def test_a_recovery_clears_the_cooldown_so_the_next_drop_alerts(tmp_path):
    write_session(tmp_path / "sessions", 100)

    run_sweep(tmp_path, FakeTmux(), pids=(100,))  # fails, alerts, sets cooldown
    run_sweep(tmp_path, FakeTmux(), pids=(100,), reconnect_on_wait=(100,))

    # Drop again; the cooldown must not swallow this one.
    path = tmp_path / "sessions" / "100.json"
    data = json.loads(path.read_text())
    data["bridgeSessionId"] = None
    path.write_text(json.dumps(data))

    _, notes = run_sweep(tmp_path, FakeTmux(), pids=(100,))
    assert len(notes) == 1
