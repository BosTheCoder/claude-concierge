import os
import subprocess
from pathlib import Path

BIN = Path.home() / "projects/personal/claude-concierge/bin/concierge"


def test_entrypoint_works_from_an_unrelated_cwd(tmp_path):
    result = subprocess.run(
        [str(BIN), "jobs"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_entrypoint_finds_uv_without_a_login_shell(tmp_path):
    """How the scheduled tasks call it: `wsl.exe -- …` sources no rc file."""
    bare = {"HOME": os.environ["HOME"], "PATH": "/usr/local/bin:/usr/bin:/bin"}
    result = subprocess.run(
        [str(BIN), "jobs"], cwd=tmp_path, capture_output=True, text=True, env=bare
    )
    assert result.returncode == 0, result.stderr
    assert "uv: not found" not in result.stderr
