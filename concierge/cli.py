"""The concierge CLI. Jobs and the concierge session call this, not the API."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import typer

from concierge import config, registry, spawn as spawn_mod, telegram, tmuxctl
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
        folder = job.get("task_folder")
        try:
            if not folder:
                raise ValueError("no task folder recorded for this job")
            body += "\n" + github_link(job["cwd"], folder, file)
        except ValueError as exc:
            # A link we cannot build must never cost the message itself.
            path = f"{folder}/{file}" if folder else file
            body += f"\n{path} (no GitHub link: {exc})"

    telegram.send(
        job["chat_id"],
        body,
        reply_to=job.get("root_message_id"),
        prefix=f"[{job_id}] ",
    )
    registry.remember_chat(job["chat_id"], state_path)

    if status:
        registry.upsert(job_id, state_path, status=status)


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
    typer.echo(run_heartbeat())
    typer.echo(run_lanes())


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


@app.command("rc")
def rc_cmd():
    """Reconnect any session that has dropped off Remote Control."""
    from concierge import rc

    typer.echo(rc.sweep())


@app.command("sessions")
def sessions_cmd():
    """Which Claude Code sessions are on Remote Control, and which have fallen
    off. Read-only — this is the question, `rc` is the fix."""
    from concierge import rc

    typer.echo(rc.report())


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
