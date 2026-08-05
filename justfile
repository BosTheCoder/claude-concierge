default:
    @just --list

test:
    uv run pytest -q

up:
    uv run python -m concierge.cli ensure-up

jobs:
    uv run python -m concierge.cli jobs
