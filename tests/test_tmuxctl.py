import subprocess
import pytest
from concierge import tmuxctl


def fake_runner(stdout="", returncode=0):
    calls = []

    def run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    run.calls = calls
    return run


def test_extract_rc_url_finds_the_session_link():
    pane = (
        "Remote Control active\n"
        "  https://claude.ai/code/session_01ABCdef\n"
        "> \n"
    )
    assert tmuxctl.extract_rc_url(pane) == "https://claude.ai/code/session_01ABCdef"


def test_extract_rc_url_takes_the_last_match():
    pane = "https://claude.ai/code/old\nhttps://claude.ai/code/new\n"
    assert tmuxctl.extract_rc_url(pane) == "https://claude.ai/code/new"


def test_extract_rc_url_strips_trailing_punctuation():
    assert tmuxctl.extract_rc_url("see https://claude.ai/code/abc.") == \
        "https://claude.ai/code/abc"


def test_extract_rc_url_returns_none_when_absent():
    assert tmuxctl.extract_rc_url("nothing here") is None


def test_build_shell_command_quotes_arguments_with_spaces():
    cmd = tmuxctl.build_shell_command("/tmp/my repo", ["claude", "--rc", "[A3] a b"])
    assert cmd == "cd '/tmp/my repo' && exec claude --rc '[A3] a b'"


def test_build_shell_command_survives_a_single_quote_in_the_title():
    cmd = tmuxctl.build_shell_command("/tmp", ["claude", "Bos's job"])
    # shlex.quote produces the safe '"'"' form; the point is it round-trips.
    assert "Bos" in cmd and cmd.startswith("cd /tmp && exec claude ")


def test_build_shell_command_exports_env_before_exec():
    cmd = tmuxctl.build_shell_command(
        "/tmp", ["claude", "brief"], {"CONCIERGE_JOB_ID": "A3"}
    )
    assert cmd == "cd /tmp && CONCIERGE_JOB_ID=A3 exec claude brief"


def test_build_shell_command_quotes_env_values():
    cmd = tmuxctl.build_shell_command("/tmp", ["claude"], {"X": "a b"})
    assert "X='a b' exec claude" in cmd


def test_build_shell_command_without_env_is_unchanged():
    assert tmuxctl.build_shell_command("/tmp", ["claude"]) == "cd /tmp && exec claude"


def test_window_command_reads_the_pane_command():
    run = fake_runner(stdout="claude\n")
    assert tmuxctl.window_command("concierge", "0", runner=run) == "claude"
    assert run.calls[0] == [
        "tmux", "list-panes", "-t", "concierge:0",
        "-F", "#{pane_current_command}",
    ]


def test_window_command_returns_none_when_the_window_is_gone():
    assert tmuxctl.window_command("concierge", "0", runner=fake_runner(returncode=1)) \
        is None


def test_window_command_returns_none_on_empty_output():
    assert tmuxctl.window_command("concierge", "0", runner=fake_runner(stdout="\n")) \
        is None


def test_has_session_true_on_exit_zero():
    run = fake_runner(returncode=0)
    assert tmuxctl.has_session("concierge", runner=run) is True
    assert run.calls[0] == ["tmux", "has-session", "-t", "concierge"]


def test_has_session_false_on_nonzero():
    assert tmuxctl.has_session("concierge", runner=fake_runner(returncode=1)) is False


def test_new_window_passes_session_window_and_command():
    run = fake_runner()
    tmuxctl.new_window("concierge", "A3", "cd /x && exec claude", runner=run)
    assert run.calls[0] == [
        "tmux", "new-window", "-t", "concierge", "-n", "A3",
        "cd /x && exec claude",
    ]


def test_list_windows_parses_names():
    run = fake_runner(stdout="0\nA3\nB7\n")
    assert tmuxctl.list_windows("concierge", runner=run) == ["0", "A3", "B7"]


def test_capture_targets_session_and_window():
    run = fake_runner(stdout="pane text")
    assert tmuxctl.capture("concierge", "A3", runner=run) == "pane text"
    assert run.calls[0] == ["tmux", "capture-pane", "-p", "-t", "concierge:A3"]


def test_new_session_passes_session_window_and_command():
    run = fake_runner()
    tmuxctl.new_session("concierge", "0", "cd /x && exec claude", runner=run)
    assert run.calls[0] == [
        "tmux", "new-session", "-d", "-s", "concierge", "-n", "0",
        "cd /x && exec claude",
    ]


def test_new_session_raises_on_nonzero():
    run = fake_runner(returncode=1, stdout="")
    # Update the fake_runner to return stderr for the error message
    def run_with_stderr(argv):
        return subprocess.CompletedProcess(argv, 1, "", "session already exists")

    with pytest.raises(RuntimeError, match="tmux new-session failed: session already exists"):
        tmuxctl.new_session("concierge", "0", "cd /x && exec claude", runner=run_with_stderr)


def test_new_window_raises_on_nonzero():
    def run_with_stderr(argv):
        return subprocess.CompletedProcess(argv, 1, "", "bad session name")

    with pytest.raises(RuntimeError, match="tmux new-window failed: bad session name"):
        tmuxctl.new_window("concierge", "A3", "cd /x && exec claude", runner=run_with_stderr)


def test_kill_window_targets_session_and_window():
    run = fake_runner()
    tmuxctl.kill_window("concierge", "A3", runner=run)
    assert run.calls[0] == ["tmux", "kill-window", "-t", "concierge:A3"]


def test_pipe_pane_sends_output_only():
    """-O is not optional. Without it tmux also pipes the command's output back
    INTO the pane, so `cat >> log` would echo the server's own status line at it
    and pane_status() would be reading its own tail."""
    run = fake_runner()
    tmuxctl.pipe_pane("rc", "0", "cat >> /x/rcserver.log", runner=run)
    assert run.calls[0] == [
        "tmux", "pipe-pane", "-O", "-t", "rc:0", "cat >> /x/rcserver.log"
    ]


def test_pipe_pane_reports_failure_without_raising():
    """A lost log copy must not fail a server start — the server matters, the
    diagnostic does not."""
    assert tmuxctl.pipe_pane("rc", "0", "cat", runner=fake_runner(returncode=1)) is False
