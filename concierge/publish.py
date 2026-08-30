"""Get one file onto GitHub before its link is sent.

A link that 404s is worse than the path it replaced, and the ordering here is
not a race that can be won by luck. The `Stop` hook that auto-commits
(`claude-wip.sh` -> `bootstrap wip`) is registered `async` and fires *after* the
turn ends; `notify` is a tool call *inside* the turn. The hook therefore always
loses, every time, by construction. So the push has to happen in `notify`
itself, synchronously, before the message goes out.

Deliberately narrow. `bootstrap wip` does `git add -A` with no size guard, and
once swallowed a 437 MB EPUB and wedged the repo. This stages exactly the one
file being linked and nothing else, and refuses outright above a size ceiling,
so adding a push step cannot widen what gets committed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# GitHub refuses a push containing a blob over 100 MB and warns over 50. Nothing
# a job writes to link at is a large file: this is a report, in markdown, for
# reading on a phone. Anything over this is a mistake, and committing a mistake
# is what wedged the repo last time.
MAX_BYTES = 25 * 1024 * 1024

# git@github.com:Owner/repo.git · ssh://git@github.com/Owner/repo.git
# https://github.com/Owner/repo.git · https://user@github.com/Owner/repo
_REMOTE = re.compile(
    r"""^(?:
          (?:ssh://)?(?:[^@/]+@)?github\.com[:/]     # ssh, scp-ish or url form
        | https?://(?:[^@/]+@)?github\.com/          # https, with or without user
        )
        (?P<slug>[^/]+/[^/]+?)                       # owner/repo
        (?:\.git)?/?$""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class Published:
    """Where the file ended up, or why it did not get there.

    `ok` is the only thing the caller may use to decide whether to send a link.
    It means: this exact content is on the remote, on `branch`, now.
    """

    ok: bool
    branch: str = ""
    detail: str = ""


def _run(argv: list[str], cwd: str) -> tuple[int, str]:
    """Never raises. A missing cwd, a missing git, a hung push — all of these
    are just "that did not work" to every caller here, and none of them may be
    allowed to take down the message the link was going to be attached to."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def remote_slug(cwd: str, runner=None) -> str | None:
    """`owner/repo` for the origin of the repo at `cwd`, or None.

    Read from the repo itself rather than from concierge.toml, so a job in a
    repo nobody remembered to configure still gets a working link.
    """
    runner = runner or _run
    code, out = runner(["git", "remote", "get-url", "origin"], cwd)
    if code != 0:
        return None
    match = _REMOTE.match(out.strip().splitlines()[0] if out.strip() else "")
    return match.group("slug") if match else None


def repo_root(cwd: str, runner=None) -> str | None:
    """The top of the working tree containing `cwd`, or None if there isn't one."""
    runner = runner or _run
    code, out = runner(["git", "rev-parse", "--show-toplevel"], cwd)
    return out.strip() if code == 0 and out.strip() else None


def current_branch(cwd: str, runner=None) -> str:
    """The branch the link must point at — the one the push lands on.

    Not the default branch: the file has to be reachable at the ref in the URL,
    and on a side branch `main` would not contain it. Detached HEAD has no
    branch to push, so fall back to what origin considers default and let the
    push fail honestly.
    """
    runner = runner or _run
    code, out = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    branch = out.strip() if code == 0 else ""
    if branch and branch != "HEAD":
        return branch
    code, out = runner(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd)
    if code == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    return "main"


def _too_big(cwd: str, relpath: str) -> str | None:
    target = Path(cwd) / relpath
    if not target.exists():
        return f"no such file: {relpath}"
    size = target.stat().st_size
    if size > MAX_BYTES:
        return f"{relpath} is {size // 1024 // 1024} MB — too big to commit"
    return None


def publish(cwd: str, relpath: str, runner=None) -> Published:
    """Commit and push exactly `relpath`, then say whether it is on the remote.

    Idempotent: a file already committed and already pushed reports ok without
    making an empty commit. The `pull --rebase` is only attempted when a push is
    actually rejected, which keeps the ordinary path to two git commands.
    """
    runner = runner or _run

    refusal = _too_big(cwd, relpath)
    if refusal:
        return Published(False, detail=refusal)

    branch = current_branch(cwd, runner)

    code, out = runner(["git", "add", "--", relpath], cwd)
    if code != 0:
        return Published(False, branch, f"git add failed: {out}")

    # An empty commit is a failure to `git commit`; here it just means the file
    # was already committed, which is the good case, not an error.
    code, out = runner(
        ["git", "commit", "-m", f"job output: {relpath}", "--", relpath], cwd
    )
    committed = code == 0

    code, out = runner(["git", "push", "origin", branch], cwd)
    if code != 0:
        runner(["git", "pull", "--rebase", "--autostash", "origin", branch], cwd)
        code, out = runner(["git", "push", "origin", branch], cwd)
    if code != 0:
        return Published(False, branch, f"push failed: {out.splitlines()[-1] if out else ''}")

    return Published(
        True, branch, "committed and pushed" if committed else "already pushed"
    )
