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

## Configure

Register each unit in `~/.config/benchctl/units.toml` and give it a profile:

```toml
[units.u1]
profile = "esp-idf"

[units.u1.console]
match = { vid = "303a", pid = "1001", serial = "AA:BB:CC:DD:EE:FF" }   # or device = "*usbserial-*1"

[units.u1.expect]                     # optional: attach fails on a different unit
mac = "AA:BB:CC:DD:EE:FF"
```

Start the profile from [`examples/profile.toml`](examples/profile.toml). Install it as
`~/.config/benchctl/profiles/<name>/profile.toml`, or as `<repo>/.benchctl/profile.toml` for
a single project. Then set the prompt, the command map, the deny list, the redaction
patterns and the identity fields for your firmware. Check everything with
`benchctl validate ~/.config/benchctl/profiles/<name>`.

`benchctl units` shows which port each unit matches. A `candidates` count above 1 means
the match is ambiguous; add a key that tells the ports apart.

## Use

```sh
benchctl attach                         # starts the daemon; the unit resets once here
benchctl mark before
benchctl cmd version
benchctl wait 'Application Version' --since before --timeout 10
benchctl flash build/debug --only app
benchctl run examples/scenarios/boot_banner.toml
benchctl detach                         # stops the daemon; serial.log stays on disk
```

Every verb takes `--json` (one object on stdout) and `--unit U` (default: the only registered
unit). Exit codes: `0` ok, `1` assertion failed or timeout, `2` usage or invalid config,
`3` unit busy (another owner holds the lease), `4` port, device or daemon error.

[`SKILL.md`](SKILL.md) is the full usage guide: verbs, cursors, leases, and the mark → act →
wait pattern. It is also a Claude Code skill, so an agent can drive the bench with it.

State lives in `~/.local/state/benchctl/<unit>/`: the socket, `lease.json`, `daemon.log`,
and one `sessions/<timestamp>/` per attach holding `serial.log` and `events.jsonl`. Scenario
runs go to `~/.local/state/benchctl/runs/<unit>/`. `BENCHCTL_CONFIG_DIR` and
`BENCHCTL_STATE_DIR` move both trees.

## Develop

```sh
scripts/gates.sh   # ruff format --check, ruff check, mypy (strict), pytest
```

Tests need no hardware: a simulated ESP-IDF device runs on a pty (`tests/fakedev.py`). The
CLI tests spawn the real daemon over a fake session (`tests/fake_daemon.py`) through
`BENCHCTL_DAEMON_ARGV`.

## Design

[`docs/design.md`](docs/design.md) holds the structure and every contract between modules.
