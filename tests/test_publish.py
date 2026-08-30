import subprocess
from pathlib import Path

import pytest

from concierge import publish


def git(cwd, *argv):
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_pair(tmp_path):
    """A real clone with a real origin. The whole point of this module is what
    git actually does, so the git here is real; only the network is not."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))

    work = tmp_path / "work"
    git(tmp_path, "clone", str(origin), str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    git(work, "remote", "set-url", "origin", "git@github.com:Owner/notes.git")
    (work / "seed").write_text("seed\n")
    git(work, "add", "-A")
    git(work, "commit", "-m", "seed")
    git(work, "push", str(origin), "main")
    # Push by path so the fake GitHub url stays put for slug parsing.
    git(work, "config", "remote.origin.pushurl", str(origin))
    return work, origin


def test_a_pushed_file_is_actually_on_the_remote(repo_pair):
    work, origin = repo_pair
    (work / "folder").mkdir()
    (work / "folder" / "report.md").write_text("the detail\n")

    result = publish.publish(str(work), "folder/report.md")

    assert result.ok
    assert result.branch == "main"
    # The claim is "this content is on the remote", so ask the remote.
    blob = subprocess.run(
        ["git", "show", "main:folder/report.md"],
        cwd=origin, capture_output=True, text=True,
    )
    assert blob.stdout == "the detail\n"


def test_only_the_named_file_is_committed(repo_pair):
    """`bootstrap wip` does `git add -A` and once swallowed a 437 MB EPUB.
    Adding a push step must not widen what gets committed."""
    work, _ = repo_pair
    (work / "report.md").write_text("linked\n")
    (work / "junk.bin").write_text("not mine\n")

    publish.publish(str(work), "report.md")

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=work, capture_output=True, text=True
    ).stdout.split()
    assert "report.md" in tracked
    assert "junk.bin" not in tracked


def test_an_oversized_file_is_refused_rather_than_committed(repo_pair, monkeypatch):
    work, _ = repo_pair
    monkeypatch.setattr(publish, "MAX_BYTES", 10)
    (work / "big.md").write_text("x" * 5000)

    result = publish.publish(str(work), "big.md")

    assert not result.ok
    assert "too big" in result.detail
    assert "big.md" not in subprocess.run(
        ["git", "ls-files"], cwd=work, capture_output=True, text=True
    ).stdout


def test_a_missing_file_is_refused_before_any_git_runs(repo_pair):
    work, _ = repo_pair
    calls = []

    result = publish.publish(
        str(work), "nope.md", runner=lambda a, c: calls.append(a) or (0, "")
    )

    assert not result.ok
    assert "no such file" in result.detail
    assert calls == []


def test_publishing_twice_does_not_make_an_empty_commit(repo_pair):
    work, _ = repo_pair
    (work / "report.md").write_text("same\n")
    publish.publish(str(work), "report.md")
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True
    ).stdout

    again = publish.publish(str(work), "report.md")

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True
    ).stdout
    assert again.ok
    assert before == after


def test_a_failed_push_is_reported_as_not_ok(repo_pair):
    """Offline, or auth gone. The caller must send a path, not a dead link."""
    work, _ = repo_pair
    git(work, "config", "remote.origin.pushurl", "/nonexistent/nowhere.git")
    (work / "report.md").write_text("x\n")

    result = publish.publish(str(work), "report.md")

    assert not result.ok
    assert "push failed" in result.detail


def test_the_branch_is_the_one_the_push_landed_on(repo_pair):
    """A side branch must not be linked at main — main would not contain it."""
    work, origin = repo_pair
    git(work, "checkout", "-b", "side")
    (work / "report.md").write_text("on the side\n")

    result = publish.publish(str(work), "report.md")

    assert result.branch == "side"
    assert subprocess.run(
        ["git", "show", "side:report.md"], cwd=origin, capture_output=True, text=True
    ).stdout == "on the side\n"


@pytest.mark.parametrize(
    "url,slug",
    [
        ("git@github.com:Owner/repo.git", "Owner/repo"),
        ("https://github.com/Owner/repo.git", "Owner/repo"),
        ("https://github.com/Owner/repo", "Owner/repo"),
        ("ssh://git@github.com/Owner/repo.git", "Owner/repo"),
        ("https://tok@github.com/Owner/repo.git", "Owner/repo"),
        ("git@gitlab.com:Owner/repo.git", None),
    ],
)
def test_remote_slug_parses_the_forms_a_remote_actually_takes(url, slug):
    assert publish.remote_slug("/x", runner=lambda a, c: (0, url)) == slug


def test_remote_slug_is_none_when_there_is_no_origin():
    assert publish.remote_slug("/x", runner=lambda a, c: (128, "no such remote")) is None
