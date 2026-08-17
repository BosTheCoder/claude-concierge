"""The config loader.

Two things here are worth real tests. Parsing, because a typo in
concierge.toml should name the offending entry rather than surface three
modules away as a TypeError. And prompt rendering, because an unsubstituted
placeholder reaches the model as literal `{{REPO_ROUTING}}` — the concierge
then has no idea where jobs go, and nothing in the system errors.
"""

from pathlib import Path

import pytest

from concierge import settings

FIXTURE = Path(__file__).parent / "fixture.toml"


def write(tmp_path, body):
    p = tmp_path / "concierge.toml"
    p.write_text(body)
    return p


# --- parsing ----------------------------------------------------------------


def test_it_reads_the_repos_in_order():
    parsed = settings.load(FIXTURE)
    assert [r.name for r in parsed.repos] == ["notes", "app"]


def test_it_expands_a_tilde_in_a_repo_path():
    parsed = settings.load(FIXTURE)
    assert "~" not in str(parsed.repos[0].path)
    assert parsed.repos[0].path.is_absolute()


def test_the_default_repo_is_the_one_marked_default(tmp_path):
    parsed = settings.load(write(tmp_path, """
[[repos]]
name = "a"
path = "/a"

[[repos]]
name = "b"
path = "/b"
default = true
"""))
    assert parsed.default_repo.name == "b"


def test_the_first_repo_is_the_default_when_none_is_marked(tmp_path):
    parsed = settings.load(write(tmp_path, """
[[repos]]
name = "a"
path = "/a"
"""))
    assert parsed.default_repo.name == "a"


def test_asking_for_a_default_with_no_repos_says_what_to_do(tmp_path):
    parsed = settings.load(write(tmp_path, "[tmux]\nsession = 's'\n"))
    with pytest.raises(settings.ConfigError, match="concierge.example.toml"):
        parsed.default_repo


def test_a_trailing_slash_on_a_github_url_is_dropped(tmp_path):
    parsed = settings.load(write(tmp_path, """
[[repos]]
name = "a"
path = "/a"
github = "https://github.com/example/a/"
"""))
    # Otherwise every built link comes out with a double slash in it.
    assert parsed.repos[0].github == "https://github.com/example/a"


def test_a_repo_without_a_path_names_the_entry(tmp_path):
    with pytest.raises(settings.ConfigError, match=r"\[\[repos\]\] #2 has no path"):
        settings.load(write(tmp_path, """
[[repos]]
name = "a"
path = "/a"

[[repos]]
name = "b"
"""))


def test_a_lane_with_an_empty_command_is_rejected(tmp_path):
    with pytest.raises(settings.ConfigError, match="empty command"):
        settings.load(write(tmp_path, """
[[lanes]]
name = "l"
command = []
"""))


def test_a_lane_keeps_its_arguments_and_expands_only_the_program(tmp_path):
    parsed = settings.load(write(tmp_path, """
[[lanes]]
name = "l"
command = ["~/bin/send.sh", "--dry-run", "~/keep"]
"""))
    program, *args = parsed.lanes[0].command
    assert "~" not in program
    assert args == ["--dry-run", "~/keep"]


def test_a_watch_missing_a_url_names_the_entry(tmp_path):
    with pytest.raises(settings.ConfigError, match=r"\[\[watches\]\] #1 has no url"):
        settings.load(write(tmp_path, """
[[watches]]
name = "w"
kind = "source"
"""))


def test_broken_toml_says_which_file(tmp_path):
    p = write(tmp_path, "[[repos]\nname = 'a'\n")
    with pytest.raises(settings.ConfigError, match="not valid TOML"):
        settings.load(p)


def test_a_missing_file_that_was_asked_for_by_name_is_an_error(tmp_path):
    with pytest.raises(settings.ConfigError, match="no config file"):
        settings.load(tmp_path / "nope.toml")


def test_the_example_file_parses():
    """It is the fallback for a fresh clone and the thing everyone copies, so
    a syntax error in it breaks the first run for every new user."""
    parsed = settings.load(settings.EXAMPLE_FILE)
    assert parsed.repos
    assert parsed.default_repo


# --- prompt rendering -------------------------------------------------------


def test_the_routing_block_lists_every_repo_with_its_topics():
    block = settings.routing_block(settings.load(FIXTURE))
    assert "the web app" in block
    assert "everything that isn't the app" in block


def test_the_routing_block_names_the_fallback_repo():
    block = settings.routing_block(settings.load(FIXTURE))
    assert "If you are unsure" in block
    assert str(settings.load(FIXTURE).default_repo.path) in block


def test_the_routing_block_survives_a_config_with_no_repos(tmp_path):
    empty = settings.load(write(tmp_path, "[tmux]\nsession = 's'\n"))
    # It must not raise: the concierge still has to come up and be able to say
    # what is wrong. `default_repo` would throw.
    assert "concierge.toml" in settings.routing_block(empty)


def test_rendering_leaves_no_placeholders_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RENDERED_DIR", tmp_path)
    for name in ("concierge", "job"):
        body = settings.render_prompt(name, settings.load(FIXTURE)).read_text()
        assert "{{" not in body, f"{name}.md has an unsubstituted placeholder"


def test_the_rendered_concierge_prompt_carries_the_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RENDERED_DIR", tmp_path)
    body = settings.render_prompt("concierge", settings.load(FIXTURE)).read_text()
    assert "the web app" in body


def test_the_rendered_prompts_point_at_this_checkouts_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RENDERED_DIR", tmp_path)
    body = settings.render_prompt("job", settings.load(FIXTURE)).read_text()
    assert str(settings.REPO_ROOT / "bin" / "concierge") in body


def test_the_committed_prompts_name_nobodys_repos():
    """The templates are public. A path or a repo name pasted back into one is
    the regression this whole config layer exists to prevent."""
    for name in ("concierge", "job"):
        body = (settings.PROMPTS_DIR / f"{name}.md").read_text()
        assert "/home/" not in body
        assert "~/projects" not in body
