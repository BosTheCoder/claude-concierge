"""The concierge CLI. Jobs and the concierge session call this, not the API."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import typer

from concierge import config, publish, registry, spawn as spawn_mod, telegram, tmuxctl
from concierge.links import github_link, humanize_age

app = typer.Typer(add_completion=False, help="Claude messaging concierge")


def format_jobs(jobs: dict, now: datetime) -> str:
    live = registry.active(jobs)
    if not live:
        return "no active jobs"
    return "\n".join(
        f"[{j['id']}] {j['title']} — {j['status']} · {humanize_age(j['opened_at'], now)}"
        for j in sorted(live.values(), key=lambda j: j["opened_at"])
    )


def format_status(job: dict, now: datetime) -> str:
    lines = [
        f"[{job['id']}] {job['title']} — {job['status']} · "
        f"{humanize_age(job['opened_at'], now)}"
    ]
    if job.get("rc_url"):
        lines.append(job["rc_url"])
    else:
        lines.append(f"no remote-control link — find it as \"[{job['id']}] {job['title']}\" in claude.ai/code")
    return "\n".join(lines)


def notify(
    job_id: str,
    text: str,
    file: str | None = None,
    status: str | None = None,
    *,
    state_path: Path | None = None,
) -> None:
    """Send a message to the chat recorded for this job.

    The destination is derived from the job id by the host. Claude never
    supplies a chat id, so a job cannot message the wrong chat.
    """
    jobs = registry.load(state_path)
    if job_id not in jobs:
        raise KeyError(f"unknown job: {job_id}")
    job = jobs[job_id]

    body = text
    if file:
        body += "\n" + attach(job, file)

    telegram.send(
        job["chat_id"],
        body,
        reply_to=job.get("root_message_id"),
        prefix=f"[{job_id}] ",
    )
    registry.remember_chat(job["chat_id"], state_path)

    # Kept so the reaper can quote a blocked job's question back at him before
    # closing it — a `waiting` job reaped silently loses whatever it asked.
    # `last_update` moves with it, which is what both timers measure from.
    fields = {"last_message": body[:1000]}
    if status:
        fields["status"] = status
        # A job that answers and goes back to work starts its wait afresh.
        fields["nudged_at"] = None
    registry.upsert(job_id, state_path, **fields)


def attach(job: dict, file: str, publisher=None) -> str:
    """The line that carries a file back to him: a GitHub link, or an honest path.

    He reads on a phone. A local path is a dead end there, so the file is pushed
    first and linked second — in that order, synchronously, because the `Stop`
    hook that would otherwise commit it is async and fires after the turn, which
    means it is always too late. See publish.py.

    Every failure here falls back to the path with the reason attached. A
    message that does not arrive is worse than a message with a path in it, so
    nothing in this function may raise.
    """
    publisher = publisher or publish.publish
    folder = job.get("task_folder") or ""
    relpath = f"{folder}/{file}" if folder else file
    cwd = job.get("cwd")

    if not cwd:
        return f"{relpath} (no GitHub link: no repo recorded for this job)"

    try:
        result = publisher(cwd, relpath)
        if not result.ok:
            return f"{relpath} (no GitHub link: {result.detail})"
        return github_link(cwd, folder, file, branch=result.branch)
    except Exception as exc:  # noqa: BLE001 - a broken link must not eat the message
        return f"{relpath} (no GitHub link: {exc})"


def send(
    job_id: str,
    text: str,
    *,
    state_path: Path | None = None,
    runner=None,
    sleeper=None,
) -> None:
    """Pass a message to a running job, and raise unless it was submitted.

    Never `tmux send-keys '<text>' Enter`: that leaves a long message sitting
    unsent in the job's input box. See tmuxctl.submit.
    """
    jobs = registry.load(state_path)
    if job_id not in jobs:
        raise KeyError(f"unknown job: {job_id}")
    pane = f"{config.TMUX_SESSION}:{jobs[job_id].get('tmux_window') or job_id}"
    problem = tmuxctl.submit(pane, text, runner=runner, sleeper=sleeper)
    if problem:
        raise RuntimeError(f"[{job_id}] not delivered: {problem}")


RESUME_PREFIX = (
    "You are resuming an interrupted job. Its task folder has notes.md and "
    "index.md — read them first.\n\n"
)


def respawn(job_id: str, *, state_path: Path | None = None) -> dict:
    """Start a fresh session for a job the restart interrupted."""
    jobs = registry.load(state_path)
    if job_id not in jobs:
        raise KeyError(f"unknown job: {job_id}")
    job = jobs[job_id]

    brief, cwd = job.get("brief"), job.get("cwd")
    if not brief or not cwd:
        raise ValueError(
            f"cannot respawn {job_id}: no brief or cwd stored — spawn it fresh"
        )

    fresh = spawn_mod.spawn_job(
        title=job.get("title", job_id),
        brief=RESUME_PREFIX + brief,
        cwd=cwd,
        chat_id=job["chat_id"],
        root_message_id=job.get("root_message_id"),
        task_folder=job.get("task_folder"),
        state_path=state_path,
    )
    registry.upsert(job_id, state_path, status="respawned")
    return fresh


@app.command("notify")
def notify_cmd(
    job_id: str = typer.Argument(
        None, help="Defaults to $CONCIERGE_JOB_ID, set for you at spawn"
    ),
    text: str = typer.Argument(None),
    file: str = typer.Option(None, help="Filename inside the job's task folder"),
    status: str = typer.Option(None, help="New status, e.g. done|failed|waiting"),
):
    job_id, text = resolve_notify_args(job_id, text)
    notify(job_id, text, file=file, status=status)


def resolve_notify_args(
    job_id: str | None, text: str | None, env: dict | None = None
) -> tuple[str, str]:
    """`notify A3 "done"` and `notify "done"` both have to work.

    The env var is the primary mechanism because it survives compaction,
    which the session name and the brief do not.
    """
    env = os.environ if env is None else env
    env_id = env.get("CONCIERGE_JOB_ID")
    if text is None:
        # One positional means it is the message; the id comes from the spawn.
        job_id, text = env_id, job_id
    else:
        job_id = job_id or env_id
    if not job_id:
        raise typer.BadParameter(
            "no job id given and CONCIERGE_JOB_ID is not set in the environment"
        )
    if not text:
        raise typer.BadParameter("no message text given")
    return job_id, text


@app.command("link")
def link_cmd(
    path: str = typer.Argument(..., help="A file, relative to your cwd or absolute"),
):
    """Push one file and print its GitHub URL.

    The concierge answers short questions inline rather than spawning a job, so
    it never goes through `notify` and would otherwise have no way to hand over
    a file at all. Same guarantee as `notify --file`: the URL is printed only
    once the content behind it is actually on the remote.
    """
    typer.echo(publish_and_link(path))


def publish_and_link(path: str, cwd: Path | None = None, publisher=None) -> str:
    """Resolve a path to its repo, push it, and return the URL — or say why not."""
    publisher = publisher or publish.publish
    target = Path(path).expanduser()
    target = target if target.is_absolute() else (cwd or Path.cwd()) / target
    target = target.resolve()

    root = publish.repo_root(str(target.parent))
    if not root:
        raise typer.BadParameter(f"not inside a git repo: {path}")

    relpath = str(target.relative_to(Path(root).resolve()))
    result = publisher(root, relpath)
    if not result.ok:
        raise typer.BadParameter(f"cannot link {relpath}: {result.detail}")
    return github_link(root, "", relpath, branch=result.branch)


@app.command("send")
def send_cmd(job_id: str, message: str):
    """Pass a message to a running job and confirm it was submitted."""
    try:
        send(job_id, message)
    except (KeyError, RuntimeError) as exc:
        typer.echo(exc.args[0], err=True)
        raise typer.Exit(1)
    typer.echo(f"[{job_id}] sent")


@app.command("respawn")
def respawn_cmd(job_id: str):
    job = respawn(job_id)
    typer.echo(job["id"])
    typer.echo(job.get("rc_url") or "")


@app.command("jobs")
def jobs_cmd():
    typer.echo(format_jobs(registry.load(), datetime.now(timezone.utc)))


@app.command("kill")
def kill_cmd(job_id: str):
    jobs = registry.load()
    if job_id not in jobs:
        raise typer.BadParameter(f"unknown job: {job_id}")
    tmuxctl.kill_window(config.TMUX_SESSION, jobs[job_id]["tmux_window"])
    registry.upsert(job_id, status="killed")
    typer.echo(f"[{job_id}] killed")


@app.command("status")
def status_cmd(job_id: str):
    jobs = registry.load()
    if job_id not in jobs:
        raise typer.BadParameter(f"unknown job: {job_id}")
    typer.echo(format_status(jobs[job_id], datetime.now(timezone.utc)))


@app.command("spawn")
def spawn_cmd(
    title: str,
    brief: str,
    cwd: str,
    chat_id: str,
    root_message_id: int = typer.Option(None),
    task_folder: str = typer.Option(None),
):
    job = spawn_mod.spawn_job(
        title=title, brief=brief, cwd=cwd, chat_id=chat_id,
        root_message_id=root_message_id, task_folder=task_folder,
    )
    typer.echo(job["id"])
    typer.echo(job.get("rc_url") or "")


@app.command("ensure-up")
def ensure_up_cmd():
    from concierge import supervisor

    typer.echo(supervisor.ensure_up())
    typer.echo(run_rc())
    typer.echo(run_reap())
    typer.echo(run_heartbeat())
    typer.echo(run_lanes())
    typer.echo(run_dashboard())


@app.command("heartbeat")
def heartbeat_cmd(
    force: bool = typer.Option(False, help="Poll now, ignoring the hourly interval"),
):
    """Check that the scheduled jobs we watch are still actually running."""
    from concierge import heartbeat as hb

    state = hb.load_state()
    if force:
        state.pop("last_poll", None)
        hb.save_state(state)
    typer.echo(hb.run_if_due())


@app.command("lanes")
def lanes_cmd(
    dry_run: bool = typer.Option(False, help="Ask each lane to plan without acting"),
):
    """Run the fast lanes now (normally ridden by ensure-up)."""
    from concierge import lanes as lanes_mod

    configured = lanes_mod.LANES
    if dry_run:
        configured = tuple(
            lanes_mod.Lane(lane.name, lane.command + ("--dry-run",))
            for lane in configured
        )
    typer.echo(lanes_mod.run_all(configured))


@app.command("reap")
def reap_cmd(
    dry_run: bool = typer.Option(
        False, help="Say what would be closed and why, without closing anything"
    ),
):
    """Close the tmux window of a job that has stopped working.

    Rides `ensure-up` every 5 minutes; this is the manual handle for looking at
    what it thinks, and for forcing a sweep now."""
    from concierge import reaper

    typer.echo(reaper.report() if dry_run else reaper.run())


@app.command("rc")
def rc_cmd():
    """Reconnect any session that has dropped off Remote Control."""
    from concierge import rc

    typer.echo(rc.sweep())


@app.command("rc-server")
def rc_server_cmd():
    """Keep `claude remote-control` up — the server that puts this machine in
    the Claude app's device list. Its own scheduled tasks drive this; it is
    deliberately not part of `ensure-up`, so a broken concierge cannot take the
    machine off the app and a broken server cannot stop the concierge."""
    from concierge import rcserver

    typer.echo(rcserver.ensure_up())


@app.command("dash")
def dash_cmd(
    port: int = typer.Option(config.DASHBOARD_PORT, help="Port to listen on"),
    expose: bool = typer.Option(
        True, help="Also publish it on the tailnet so the phone can reach it"
    ),
):
    """Serve the session dashboard — every Claude process on the box, what
    state it is in, and a button to close the idle ones.

    Normally started by `ensure-up` into its own tmux window, so it is there
    when no terminal is. This is the handle for running it in the foreground.
    """
    from concierge import dashboard

    dashboard.serve(port=port, exposed=expose, echo=typer.echo)


@app.command("sessions")
def sessions_cmd():
    """Which Claude Code sessions are on Remote Control, and which have fallen
    off. Read-only — this is the question, `rc` is the fix."""
    from concierge import rc

    typer.echo(rc.report())


def run_dashboard() -> str:
    """The dashboard rides ensure-up for exactly the reason it exists: it is
    only useful when he has no terminal open, which is precisely when nobody is
    there to start it. Same total guard as the heartbeat and the lanes — a
    broken dashboard must never stop the concierge coming up.
    """
    try:
        from concierge import dashboard

        return dashboard.ensure_up()
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return f"dash-error: {exc}"


def run_rc() -> str:
    """The Remote Control sweep rides ensure-up as well, and runs before the
    heartbeat and the lanes because a concierge that is up but unreachable from
    the phone is the failure this whole repo exists to prevent. Same total
    guard as the others: a bug in the sweep must not stop the concierge coming
    up, and typing into panes is exactly the sort of thing that can throw.
    """
    try:
        from concierge import rc

        return rc.sweep()
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return f"rc-error: {exc}"


def run_reap() -> str:
    """The reaper rides ensure-up for the same reason the heartbeat does: this
    tick is the most reliably executed thing on the machine, and a reaper on
    its own schedule is one more thing that can rot silently. Same total guard
    — a bug in the sweep must never stop the concierge coming up, and this one
    sends signals, so that matters more here than anywhere else.
    """
    try:
        from concierge import reaper

        return reaper.run()
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return f"reap-error: {exc}"


def run_heartbeat() -> str:
    """The heartbeat rides ensure-up (see heartbeat.py) rather than taking a
    scheduled task of its own. It must never be able to take the concierge
    watchdog down with it — keeping the concierge alive is the job that
    matters, and a broken heartbeat is not worth failing that over.
    """
    try:
        from concierge import heartbeat

        return heartbeat.run_if_due()
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return f"heartbeat-error: {exc}"


def run_lanes() -> str:
    """Fast lanes ride ensure-up too (see lanes.py). Same total guard as the
    heartbeat, and for a stronger reason: these ones act on the outside world,
    so a bug here is exactly the sort of thing that must not also take the
    concierge down with it.
    """
    try:
        from concierge import lanes

        return lanes.run_all()
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return f"lanes-error: {exc}"


if __name__ == "__main__":
    app()
