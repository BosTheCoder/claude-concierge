from datetime import datetime, timezone
import pytest
from concierge import config
from concierge.links import github_link, humanize_age


def test_github_link_for_the_tasks_repo():
    link = github_link(
        str(config.TASKS_REPO), "2026-08-05-x", "report.md"
    )
    assert link == (
        "https://github.com/BosTheCoder/tasks/blob/main/2026-08-05-x/report.md"
    )


def test_github_link_for_the_property_repo():
    link = github_link(str(config.NPM_REPO), "2026-08-05-y", "notes.md")
    assert link == (
        "https://github.com/BosTheCoder/nyakundi-property-management"
        "/blob/main/2026-08-05-y/notes.md"
    )


def test_github_link_rejects_an_unknown_repo():
    with pytest.raises(ValueError, match="unknown repo"):
        github_link("/tmp/somewhere", "f", "x.md")


def test_humanize_age_in_minutes():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    assert humanize_age("2026-08-05T14:45:00+00:00", now) == "15m"


def test_humanize_age_in_hours():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    assert humanize_age("2026-08-05T12:00:00+00:00", now) == "3h"


def test_humanize_age_rolls_over_to_days_past_48_hours():
    now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    assert humanize_age("2026-08-03T14:00:00+00:00", now) == "2d"
