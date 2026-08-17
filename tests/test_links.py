from datetime import datetime, timezone
import pytest
from concierge import config
from concierge.links import github_link, humanize_age


def test_github_link_uses_the_url_configured_for_that_repo():
    link = github_link(str(config.REPOS[0].path), "2026-08-05-x", "report.md")
    assert link == (
        "https://github.com/example/notes/blob/main/2026-08-05-x/report.md"
    )


def test_each_repo_gets_its_own_url():
    link = github_link(str(config.REPOS[1].path), "2026-08-05-y", "notes.md")
    assert link.startswith("https://github.com/example/app/blob/main/")


def test_github_link_matches_a_tilde_path_to_its_repo():
    """The concierge is told to spawn with the path from the prompt, which is
    written out in full; a job reporting back may pass either form."""
    assert github_link("~/notes", "f", "x.md").startswith(
        "https://github.com/example/notes"
    )


def test_github_link_rejects_an_unconfigured_repo():
    with pytest.raises(ValueError, match="no github url"):
        github_link("/tmp/somewhere", "f", "x.md")


def test_github_link_rejects_a_repo_with_no_url_configured():
    from concierge.settings import Repo
    from pathlib import Path

    repos = (Repo(name="local", path=Path("/tmp/local-only"), github=None),)
    with pytest.raises(ValueError, match="no github url"):
        github_link("/tmp/local-only", "f", "x.md", repos=repos)


def test_humanize_age_in_minutes():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    assert humanize_age("2026-08-05T14:45:00+00:00", now) == "15m"


def test_humanize_age_in_hours():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    assert humanize_age("2026-08-05T12:00:00+00:00", now) == "3h"


def test_humanize_age_rolls_over_to_days_past_48_hours():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    assert humanize_age("2026-08-03T14:00:00+00:00", now) == "2d"
