#!/usr/bin/env bash
# Cheapest first; stops at the first failing gate.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --quiet
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q
