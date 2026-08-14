default:
    @just --list

test:
    uv run pytest -q

up:
    uv run python -m concierge.cli ensure-up

jobs:
    uv run python -m concierge.cli jobs

# The Remote Control server — this machine in the Claude app's device list.
rc-server:
    uv run python -m concierge.cli rc-server
