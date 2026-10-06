# benchctl

Bench harness for ESP-IDF units. One daemon per unit holds the serial port for the whole
session, so the unit resets once at attach; every `benchctl` command is a short, stateless
client call against that daemon. Project-specific knowledge (prompt, commands, identity,
redaction) lives in a profile, not in this repo.

## Install

```sh
uv tool install .
benchctl --version
```

## Develop

```sh
scripts/gates.sh   # ruff format --check, ruff check, mypy (strict), pytest
```

Tests need no hardware: a simulated ESP-IDF device runs on a pty (`tests/fakedev.py`).

## Design

[`docs/design.md`](docs/design.md) holds the structure and every contract between modules.
