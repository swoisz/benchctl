# Contributing

## Setup

benchctl uses [uv](https://docs.astral.sh/uv/). From a checkout:

```sh
uv sync
scripts/gates.sh
```

`scripts/gates.sh` runs what CI runs, cheapest first: `ruff format --check`, `ruff check`,
`mypy` (strict, on `src/` and `tests/`) and `pytest`. The integration tests drive a
simulated ESP-IDF device over a pty, so no hardware is needed. benchctl targets macOS and
Linux.

## Changes

- Open an issue first for anything bigger than a small fix, so the shape is agreed before
  you build it.
- One change per pull request, with tests. CI must pass.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`fix(session): …`, `docs: …`), as in `git log`.
- Device-specific knowledge belongs in a profile, not in this repo: no product names,
  hostnames, credentials or logs captured from real units in code, tests or docs.
