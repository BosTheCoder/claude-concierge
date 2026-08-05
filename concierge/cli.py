"""The concierge CLI. Jobs and the concierge session call this, not the API."""

from __future__ import annotations

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

    body = f"[{job_id}] {text}"
    if file:
        body += "\n" + github_link(job["cwd"], job["task_folder"], file)

    telegram.send(job["chat_id"], body, reply_to=job.get("root_message_id"))

    if status:
        registry.upsert(job_id, state_path, status=status)


@app.command("notify")
def notify_cmd(
    job_id: str,
    text: str,
    file: str = typer.Option(None, help="Filename inside the job's task folder"),
    status: str = typer.Option(None, help="New status, e.g. done|failed|waiting"),
):
    notify(job_id, text, file=file, status=status)


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


if __name__ == "__main__":
    app()
