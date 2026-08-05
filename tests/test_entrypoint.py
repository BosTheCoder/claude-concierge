import subprocess
from pathlib import Path

BIN = Path.home() / "projects/personal/claude-concierge/bin/concierge"


def test_entrypoint_works_from_an_unrelated_cwd(tmp_path):
    result = subprocess.run(
        [str(BIN), "jobs"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
