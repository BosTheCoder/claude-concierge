import shlex

import pytest

from concierge import config, rcserver

# Captured from a live `claude remote-control`. The banner above
# the status line is the point: "Connecting" stays on screen for the life of
# the server, so these are not interchangeable with hand-written fragments.
CONNECTED = """\
Remote Control v2.1.231
Connecting to claude.ai...

·✔︎· Connected · notes · main
    Capacity: 1/32 · New sessions will be created in the current directory
    test-box

Continue coding in the Claude mobile app or https://claude.ai/code?environment=env_01H
space to show QR code · w to toggle spawn mode
"""

RECONNECTING = """\
Remote Control v2.1.231
Connecting to claude.ai...

⠋ Reconnecting · retrying in 8s · disconnected 42s
"""


class FakeTmux:
    """Only the calls rcserver makes. Real `build_shell_command` — the quoting
    is the thing a caller can get wrong."""

    build_shell_command = staticmethod(rcserver.tmuxctl.build_shell_command)

    def __init__(self, *, sessions=(), window_cmd=None, pane="", windows=("0",)):
        self.sessions = set(sessions)
        self._window_cmd = window_cmd
        self._pane = pane
        self._windows = list(windows)
        self.calls = []
        self.started = []
        self.piped = []

    def has_session(self, session):
        return session in self.sessions

    def window_command(self, session, window):
        return self._window_cmd

    def list_windows(self, session):
        return list(self._windows)

    def capture(self, session, window):
        return self._pane

    def new_session(self, session, window, shell_command):
        self.calls.append(("new_session", session, window))
        self.started.append(shell_command)
        self.sessions.add(session)

    def new_window(self, session, window, shell_command):
        self.calls.append(("new_window", session, window))
        self.started.append(shell_command)

    def kill_window(self, session, window):
        self.calls.append(("kill_window", session, window))

    def pipe_pane(self, session, window, command):
        self.calls.append(("pipe_pane", session, window))
        self.piped.append(command)
        return True


@pytest.fixture
def state(tmp_path):
    return tmp_path / "rcserver.json"


def run(tmux, state, **kw):
    notes = []
    result = rcserver.ensure_up(
        tmux=tmux, state_path=state, notifier=notes.append, **kw
    )
    return result, notes


# --- pane_status ------------------------------------------------------------


def test_pane_status_reads_the_status_line_not_the_startup_banner():
    """The banner keeps the word "Connecting" on screen forever. Reading
    top-down reports a server that has been healthy for a day as mid-connect,
    and the strike counter then recycles it every twenty minutes."""
    assert rcserver.pane_status(CONNECTED) == "connected"


def test_pane_status_spots_the_backoff():
    assert rcserver.pane_status(RECONNECTING) == "reconnecting"


def test_pane_status_of_a_pane_saying_nothing_is_unknown():
    assert rcserver.pane_status("") == "unknown"
    assert rcserver.pane_status("bash: claude: command not found") == "unknown"


# --- argv -------------------------------------------------------------------


def test_server_argv_never_spawns_into_worktrees():
    """~/projects/personal is inside the home-wsl repo, and worktrees of a
    notes repo are pointless. Either way this must stay same-dir."""
    argv = rcserver.server_argv()
    assert argv[argv.index("--spawn") + 1] == "same-dir"


def test_server_argv_runs_in_the_configured_permission_mode():
    argv = rcserver.server_argv()
    assert argv[argv.index("--permission-mode") + 1] == config.PERMISSION_MODE
    assert "--dangerously-skip-permissions" not in argv


# --- alive ------------------------------------------------------------------


def test_a_session_whose_window_dropped_to_a_shell_is_not_alive():
    """The window's shell outlives the server, leaving a session that is
    present and answers nothing."""
    tmux = FakeTmux(sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="zsh")
    assert rcserver.alive(tmux) is False


def test_alive_when_the_window_holds_claude():
    tmux = FakeTmux(sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="claude")
    assert rcserver.alive(tmux) is True


# --- ensure_up --------------------------------------------------------------


def test_starts_the_server_when_there_is_no_session(state):
    tmux = FakeTmux()
    result, notes = run(tmux, state)

    assert result == "started"
    assert tmux.calls == [
        ("new_session", config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW),
        ("pipe_pane", config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW),
    ]
    started = shlex.split(tmux.started[0].split("&&", 1)[1].replace("exec ", "", 1))
    assert started[:2] == ["claude", "remote-control"]
    assert str(config.SETTINGS.default_repo.path) in tmux.started[0]
    assert notes == []


def test_replaces_a_dead_window_without_killing_the_session(state):
    """kill_window, not kill_session: `new_session` fails on a session that is
    already there, and the session may hold other windows."""
    tmux = FakeTmux(sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="zsh")
    result, _ = run(tmux, state)

    assert result == "started"
    assert tmux.calls == [
        ("kill_window", config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW),
        ("new_window", config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW),
        # Re-attached to the replacement window: pipe-pane is per-pane.
        ("pipe_pane", config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW),
    ]


def test_losing_the_startup_race_is_not_a_failure(state):
    """The logon task and the watchdog can fire together and both see nothing."""
    tmux = FakeTmux()

    def explode(*a, **kw):
        raise RuntimeError("duplicate session: rc")

    tmux.new_session = explode
    result, notes = run(tmux, state)

    assert result == "healthy"
    assert notes == []


def test_a_healthy_server_is_left_alone_and_says_nothing(state):
    tmux = FakeTmux(
        sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="claude", pane=CONNECTED
    )
    result, notes = run(tmux, state)

    assert result == "healthy"
    assert tmux.calls == []
    assert notes == []


def test_one_bad_reading_does_not_restart_anything(state):
    """The CLI's own backoff recovers a ten-minute network drop. Restarting on
    the first strike would kill a live phone session to fix nothing."""
    tmux = FakeTmux(
        sessions={config.RC_SERVER_TMUX_SESSION},
        window_cmd="claude",
        pane=RECONNECTING,
    )
    result, notes = run(tmux, state)

    assert result.startswith("degraded")
    assert tmux.calls == []
    assert notes == []


def test_recycles_and_speaks_up_once_the_strikes_run_out(state):
    tmux = FakeTmux(
        sessions={config.RC_SERVER_TMUX_SESSION},
        window_cmd="claude",
        pane=RECONNECTING,
    )
    for _ in range(rcserver.DEGRADED_STRIKES - 1):
        assert run(tmux, state)[0].startswith("degraded")

    result, notes = run(tmux, state)

    assert result == "recycled"
    assert ("new_window", config.RC_SERVER_TMUX_SESSION, config.RC_SERVER_WINDOW) \
        in tmux.calls
    assert len(notes) == 1
    assert "restarted it" in notes[0]
    # Counted from zero again, or a flapping server alerts every tick.
    assert rcserver.load_state(state).get("strikes") == 0


def test_recovering_clears_the_strikes(state):
    """Otherwise three drops spread across a week add up to a restart."""
    tmux = FakeTmux(
        sessions={config.RC_SERVER_TMUX_SESSION},
        window_cmd="claude",
        pane=RECONNECTING,
    )
    run(tmux, state)
    run(tmux, state)
    tmux._pane = CONNECTED
    assert run(tmux, state)[0] == "healthy"
    assert rcserver.load_state(state).get("strikes") == 0

    tmux._pane = RECONNECTING
    assert run(tmux, state)[0].endswith(f"(1/{rcserver.DEGRADED_STRIKES})")


# --- restart history --------------------------------------------------------
#
# Every server start mints a fresh session ~10s later, which the Claude app
# shows as an empty chat. So the restart rate is the empty-chat rate, and these
# assert that rate is actually recoverable after the fact.


def _history(state):
    return rcserver.load_state(state).get("history", [])


def test_a_healthy_server_writes_no_history(state):
    """288 ticks a day. If healthy ones recorded, the file would be nothing but
    noise and the real transitions would be unfindable."""
    tmux = FakeTmux(
        sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="claude", pane=CONNECTED
    )
    for _ in range(5):
        run(tmux, state)

    assert _history(state) == []


def test_a_recycle_is_recorded_with_what_the_pane_said(state):
    """"Restarted" alone does not distinguish a reboot from a four-strike
    recycle, and they call for different fixes."""
    tmux = FakeTmux(
        sessions={config.RC_SERVER_TMUX_SESSION},
        window_cmd="claude",
        pane=RECONNECTING,
    )
    for _ in range(rcserver.DEGRADED_STRIKES):
        run(tmux, state)

    events = [entry["event"] for entry in _history(state)]
    assert events == ["degraded", "recycled"]
    assert "reconnecting" in _history(state)[-1]["why"]


def test_a_start_records_whether_there_was_a_session_at_all(state):
    """A missing tmux session is a reboot or a wsl --shutdown; a session whose
    window fell back to a shell is the server having exited under us."""
    run(FakeTmux(), state)
    assert _history(state)[-1]["why"] == "no tmux session"

    dropped = FakeTmux(sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="zsh")
    run(dropped, state)
    assert _history(state)[-1]["why"] == "window not running claude"


def test_history_is_bounded(state):
    """A server flapping every five minutes must not grow the state file without
    limit — it is read by hand."""
    tmux = FakeTmux(sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="zsh")
    for _ in range(rcserver.HISTORY_LIMIT + 15):
        run(tmux, state)

    assert len(_history(state)) == rcserver.HISTORY_LIMIT


# --- output capture ---------------------------------------------------------


def test_the_replacement_window_also_gets_its_output_captured(state):
    """pipe-pane is per-pane, not per-session, so a recycle that only re-attached
    on the first start would go dark exactly when the server is misbehaving."""
    tmux = FakeTmux(sessions={config.RC_SERVER_TMUX_SESSION}, window_cmd="zsh")
    run(tmux, state)

    assert tmux.calls[-1][0] == "pipe_pane"
    assert str(config.RC_SERVER_LOG) in tmux.piped[-1]


def test_capture_appends_rather_than_truncating(state):
    """Truncating would race the exit we are trying to catch: the server dies,
    the watchdog restarts it, and the restart erases the last words."""
    tmux = FakeTmux()
    run(tmux, state)

    assert ">>" in tmux.piped[-1]


def test_the_capture_log_is_trimmed_to_its_tail(tmp_path):
    """41 MB/day measured. Unbounded logging into the guest is what the WSL
    diagnosis blamed for the journald storm."""
    log = tmp_path / "rcserver.log"
    log.write_bytes(b"x" * (rcserver.LOG_TAIL_BYTES * 3))

    rcserver._trim_log(log)

    assert log.stat().st_size <= rcserver.LOG_TAIL_BYTES


def test_trimming_keeps_the_end_not_the_beginning(tmp_path):
    """The server's last words before it exits are the whole point."""
    log = tmp_path / "rcserver.log"
    log.write_bytes(b"OLDEST\n" + b"y" * rcserver.LOG_TAIL_BYTES + b"\nNEWEST\n")

    rcserver._trim_log(log)

    body = log.read_bytes()
    assert body.endswith(b"NEWEST\n")
    assert b"OLDEST" not in body


def test_an_already_open_writer_survives_the_trim(tmp_path):
    """pipe-pane's `cat >>` holds the file open for the life of the server.
    Recreating the path would leave that writer appending to an unlinked inode:
    the log stops growing and the failure looks exactly like a healthy quiet
    server. So drive the real seam — hold an fd across the trim and check its
    writes still land at the path.

    Asserting st_ino instead does NOT catch this: the freed inode is routinely
    handed straight back, so the number matches and the test passes green
    against the broken version. Verified 2026-08-18.
    """
    log = tmp_path / "rcserver.log"
    log.write_bytes(b"z" * (rcserver.LOG_TAIL_BYTES * 2))

    with log.open("ab") as writer:  # stands in for pipe-pane's `cat >>`
        rcserver._trim_log(log)
        writer.write(b"AFTER-TRIM\n")
        writer.flush()

    assert log.read_bytes().endswith(b"AFTER-TRIM\n")


def test_a_small_log_is_left_alone(tmp_path):
    log = tmp_path / "rcserver.log"
    log.write_bytes(b"short\n")

    rcserver._trim_log(log)

    assert log.read_bytes() == b"short\n"
