import pytest

from concierge import tmuxctl


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
