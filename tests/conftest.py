import os
from pathlib import Path

# Before anything imports concierge.config, which reads the config at import
# time. Without this the suite would assert against whichever repos, lanes and
# watches this particular machine is set up for, and would pass or fail for
# reasons that have nothing to do with the code.
os.environ["CONCIERGE_CONFIG"] = str(Path(__file__).parent / "fixture.toml")

import pytest  # noqa: E402

from concierge import tmuxctl  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch):
    """The suite may never drive the tmux server it is running inside.

    This is not hypothetical caution. Adding the Remote Control sweep to
    `ensure-up` gave one existing test an unstubbed path into the real
    `tmux send-keys`, and running the suite typed `/rc` into a live concierge
    window and left a dialog open on it. Every test that legitimately needs
    tmux passes its own `runner`; nothing should reach this.
    """

    def refuse(argv):
        raise AssertionError(
            f"test tried to run a real tmux command: {' '.join(argv)}. "
            "Inject a fake runner, or stub the caller."
        )

    monkeypatch.setattr(tmuxctl, "_run", refuse)


@pytest.fixture(autouse=True)
def no_rendered_prompts(monkeypatch, tmp_path):
    """Rendering writes into state/. Keep the suite out of the real one."""
    from concierge import settings

    monkeypatch.setattr(settings, "RENDERED_DIR", tmp_path / "state")
