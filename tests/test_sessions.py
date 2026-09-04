"""The rules that decide whether a session may be killed.

Everything here is about one asymmetry: showing a busy session as idle loses
work, and showing an idle one as busy costs a page refresh. So the tests that
matter are the ones that would fail if a future edit made `idle` easier to
reach, not the ones that pin a string.
"""

import subprocess

import pytest

from concierge import sessions


def proc(pid=100, ppid=1, argv=("claude",), rss_kb=1000, ticks=0, starttime=42):
    return sessions.Proc(
        pid=pid, ppid=ppid, argv=tuple(argv), rss_kb=rss_kb, ticks=ticks,
        starttime=starttime, cwd="/home/bosire/projects/personal/tasks",
    )


def state(**kwargs):
    args = {
        "protected": False,
        "cpu_rate": 0.5,
        "pane": None,
        "job_status": None,
        "transcript_age": 3600.0,
    }
    args.update(kwargs)
    return sessions.classify(**args)[0]


# --- nothing busy may ever come out reapable ---------------------------------


def test_a_job_the_registry_calls_running_is_running():
    assert state(job_status="running") == "running"


def test_a_registered_running_job_wins_over_a_quiet_cpu():
    # The dangerous combination: the row says running, the process happens to
    # be between API calls and reading idle. The registry has to win.
    assert state(job_status="running", cpu_rate=0.1, pane="idle") == "running"


def test_a_busy_pane_wins_over_a_quiet_cpu():
    assert state(cpu_rate=0.2, pane="busy") == "running"


def test_a_busy_cpu_wins_over_a_finished_pane():
    assert state(cpu_rate=9.0, pane="idle") == "running"


def test_a_fresh_transcript_write_means_still_working():
    assert state(cpu_rate=0.2, transcript_age=1.0) == "running"


def test_a_waiting_job_is_never_offered():
    # It is blocked on a human and holding the question it asked. The concierge
    # reaper gives these hours; the dashboard gives them a state of their own
    # so the button never appears at all.
    assert state(job_status="waiting", cpu_rate=0.1) == "waiting"


def test_no_cpu_sample_means_unknown_not_idle():
    assert state(cpu_rate=None) == "unknown"


def test_unknown_is_not_reapable():
    row = session_row(state="unknown")
    assert row.reapable is False


def test_waiting_is_not_reapable():
    assert session_row(state="waiting").reapable is False


# --- and the quiet ones must actually come out reapable ----------------------


def test_a_quiet_session_with_a_sample_is_idle():
    assert state(cpu_rate=0.8) == "idle"


def test_a_finished_job_says_so():
    assert state(cpu_rate=0.8, job_status="done") == "finished"
    assert sessions.classify(
        protected=False, cpu_rate=0.8, pane=None, job_status="done",
        transcript_age=None,
    )[0] == "finished"


def test_idle_and_finished_are_both_reapable():
    assert session_row(state="idle").reapable
    assert session_row(state="finished").reapable


def test_protected_is_never_reapable_whatever_its_state():
    for value in ("idle", "finished", "running", "unknown"):
        assert session_row(state=value, protected=True).reapable is False


def session_row(*, state="idle", protected=False):
    return sessions.Session(
        pid=1, kind="session", protected=protected, state=state, why="",
        rss_self_kb=0, rss_tree_kb=0, cwd=None, title="", job_id=None,
        job_status=None, tmux=None, tmux_target=None, uuid=None, cpu_rate=0.5,
        transcript_age=None, age_seconds=0, starttime=1,
    )


# --- telling the concierge apart from everything else ------------------------


def test_the_concierge_is_recognised_by_its_remote_control_name():
    assert sessions.is_concierge(proc(argv=("claude", "--remote-control", "concierge")))


def test_the_concierge_is_recognised_by_its_rendered_prompt():
    # Either mark alone is enough. A false positive costs one row he cannot
    # reap; a false negative costs the concierge.
    assert sessions.is_concierge(
        proc(argv=("claude", "--append-system-prompt-file", "/x/concierge.rendered.md"))
    )


def test_a_job_is_not_the_concierge():
    assert not sessions.is_concierge(
        proc(argv=("claude", "--remote-control", "[E8] whatever",
                   "--append-system-prompt-file", "/x/job.rendered.md"))
    )


def test_the_remote_control_server_is_its_own_kind():
    assert sessions.kind_of(proc(argv=("claude", "remote-control", "--name", "x"))) == "server"


def test_background_machinery_is_a_helper():
    for argv in (
        ("claude", "bg-spare", "--bg-spare", "/tmp/x.sock"),
        ("claude", "bg-pty-host", "--bg-pty-host", "/tmp/x.sock"),
        ("claude.exe", "daemon", "run", "--json-path", "/x"),
    ):
        assert sessions.kind_of(proc(argv=argv)) == "helper", argv


def test_a_throwaway_print_call_is_a_helper():
    # claude-mem shells out to one of these every few minutes. They are 200 MB
    # while they last and nobody's conversation, so they belong in the totals
    # and not in the list.
    assert sessions.kind_of(
        proc(argv=("claude", "--print", "--no-session-persistence"))
    ) == "helper"


def test_an_ordinary_session_is_a_session():
    assert sessions.kind_of(proc(argv=("claude", "--permission-mode", "auto"))) == "session"


def test_a_brief_is_not_mistaken_for_a_subcommand():
    # `claude '<brief>'` is how every job starts, and the brief is positional.
    assert sessions.kind_of(proc(argv=("claude", "go and do the thing"))) == "session"


# --- memory attribution -------------------------------------------------------


def test_mcp_children_are_counted_against_their_session():
    procs = {
        10: proc(pid=10, ppid=1, argv=("claude",), rss_kb=300),
        11: proc(pid=11, ppid=10, argv=("node", "mcp.js"), rss_kb=100),
        12: proc(pid=12, ppid=11, argv=("node", "proxy.js"), rss_kb=50),
    }
    owned = sessions.owners(procs, {10})
    assert owned == {10: 10, 11: 10, 12: 10}


def test_a_session_spawned_by_the_server_owns_its_own_subtree():
    # Otherwise the Remote Control server's row shows the RAM of every session
    # started from the phone, and reaping the session appears to free nothing.
    procs = {
        10: proc(pid=10, ppid=1, argv=("claude", "remote-control"), rss_kb=170),
        20: proc(pid=20, ppid=10, argv=("claude.exe", "--print"), rss_kb=240),
        21: proc(pid=21, ppid=20, argv=("node", "mcp.js"), rss_kb=90),
    }
    owned = sessions.owners(procs, {10, 20})
    assert owned[20] == 20
    assert owned[21] == 20


def test_a_process_with_no_claude_ancestor_belongs_to_nobody():
    procs = {5: proc(pid=5, ppid=1, argv=("bash",))}
    assert sessions.owners(procs, set()) == {}


# --- reading a pane -----------------------------------------------------------


WORKING = (
    "  ⏺ writing it out\n"
    "✽ Twisting… (3m 52s · ↓ 12.0k tokens · thought for 1s)\n"
    "❯ \n"
    "  tasks | Opus 5 (1M context) | ctx 11% | main synced | "
    "c756041f-bbd3-4ea9-865c-2088d32242ba\n"
)
FINISHED = (
    "  the essay text\n"
    "✻ Cogitated for 33s · done 11:00 AM\n"
    "❯ \n"
    "  tasks | Opus 5 (1M context) | ctx 6% | main synced | "
    "69a210da-a26b-4481-8e66-37c177978363\n"
)


def fake_capture(text, returncode=0):
    def runner(_argv):
        return subprocess.CompletedProcess([], returncode, stdout=text, stderr="")

    return runner


def test_a_live_turn_timer_reads_as_busy():
    assert sessions.pane_read("%1", runner=fake_capture(WORKING))[0] == "busy"


def test_a_finished_turn_reads_as_idle():
    assert sessions.pane_read("%1", runner=fake_capture(FINISHED))[0] == "idle"


def test_the_newest_status_line_wins():
    # A finished turn is still on screen above the one now running.
    assert sessions.pane_read("%1", runner=fake_capture(FINISHED + WORKING))[0] == "busy"


def test_the_session_id_comes_out_of_the_footer():
    _, uuid = sessions.pane_read("%1", runner=fake_capture(FINISHED))
    assert uuid == "69a210da-a26b-4481-8e66-37c177978363"


def test_a_pane_that_is_gone_says_nothing():
    assert sessions.pane_read("%9", runner=fake_capture("", returncode=1)) == (None, None)


def test_slug_matches_claude_codes_own_transcript_folder():
    assert (
        sessions.slug_for("/home/bosire/projects/personal/tasks")
        == "-home-bosire-projects-personal-tasks"
    )


def test_short_path_does_not_double_the_root_slash():
    assert sessions._short_path("/tmp") == "tmp"
    assert sessions._short_path("/home/bosire/projects/personal/tasks") == "personal/tasks"


# --- the guards on the way out ------------------------------------------------


class FakeTmux:
    def __init__(self):
        self.killed = []

    def kill_window(self, session, window):
        self.killed.append((session, window))


def snapshot_of(row, monkeypatch):
    monkeypatch.setattr(sessions, "snapshot", lambda *a, **k: {"sessions": [row]})


def row(**kwargs):
    base = {
        "pid": 500, "fingerprint": 77, "protected": False, "reapable": True,
        "state": "idle", "why": "idle at 0.8 ticks/s", "jobId": None,
        "rssTreeMb": 350,
    }
    base.update(kwargs)
    return base


def test_reaping_a_protected_session_is_refused(monkeypatch):
    snapshot_of(row(protected=True), monkeypatch)
    with pytest.raises(sessions.RefusedError, match="protected"):
        sessions.reap(500, 77)


def test_reaping_a_running_session_is_refused(monkeypatch):
    snapshot_of(row(reapable=False, state="running"), monkeypatch)
    with pytest.raises(sessions.RefusedError, match="running"):
        sessions.reap(500, 77)


def test_a_stale_page_cannot_kill_a_recycled_pid(monkeypatch):
    # The whole reason the fingerprint exists: a page left open on a phone
    # overnight is describing a machine that has moved on.
    snapshot_of(row(fingerprint=88), monkeypatch)
    with pytest.raises(sessions.RefusedError, match="different process"):
        sessions.reap(500, 77)


def test_a_pid_that_is_no_longer_a_session_is_refused(monkeypatch):
    monkeypatch.setattr(sessions, "snapshot", lambda *a, **k: {"sessions": []})
    with pytest.raises(sessions.RefusedError, match="no longer"):
        sessions.reap(500, 77)


def test_a_registered_job_goes_out_through_the_registry(monkeypatch, tmp_path):
    """Not a bare kill: the row has to end up `reaped` so `respawn E8` still
    works and the reaper does not come back to a window that is already gone."""
    snapshot_of(row(jobId="E8"), monkeypatch)
    tmux = FakeTmux()
    upserts = []
    monkeypatch.setattr(sessions.registry, "load", lambda *a, **k: {"E8": {"tmux_window": "E8"}})
    monkeypatch.setattr(
        sessions.registry, "upsert",
        lambda job_id, *a, **fields: upserts.append((job_id, fields)),
    )
    message = sessions.reap(500, 77, tmux=tmux)
    assert tmux.killed == [(sessions.config.TMUX_SESSION, "E8")]
    assert upserts == [("E8", {"touch": False, "status": "reaped"})]
    assert "E8" in message


def test_a_session_the_server_spawned_does_not_inherit_the_servers_pane():
    """It would be labelled rc:0 and, worse, judged on a pane showing the
    server's log instead of any conversation."""
    server = proc(pid=10, ppid=1, argv=("claude", "remote-control"))
    child = proc(pid=20, ppid=10, argv=("claude.exe", "--print", "--sdk-url", "x"))
    procs = {10: server, 20: child}
    pane = sessions.Pane(10, "rc", "0", "0", "%1")
    assert sessions._pane_for(child, {10: pane}, procs) is None
    assert sessions._pane_for(server, {10: pane}, procs) is pane


def test_a_hand_started_session_still_finds_its_pane_through_the_shell():
    shell = proc(pid=10, ppid=1, argv=("zsh",))
    claude = proc(pid=20, ppid=10, argv=("claude",))
    pane = sessions.Pane(10, "work", "1", "editor", "%2")
    assert sessions._pane_for(claude, {10: pane}, {10: shell, 20: claude}) is pane


def test_a_phone_started_session_is_named_by_its_cloud_id():
    argv = ("claude.exe", "--print", "--session-id", "cse_01EETyTC7mDqksPTSshL8HV9")
    assert sessions.cloud_session_id(argv) == "cse_01EETyTC7mDqksPTSshL8HV9"
    assert sessions.cloud_session_id(("claude", "--permission-mode", "auto")) is None
