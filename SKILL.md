---
name: benchctl
description: Drive an ESP-IDF unit on the bench through its serial console with the benchctl CLI. Covers attaching once, sending shell commands, waiting for log lines since a mark, resetting, flashing, capturing boots, and running scenarios. Use it when testing firmware on real hardware, checking boot or reset behaviour, or reproducing a device bug from the shell.
---

# benchctl

One daemon per unit holds the serial port for the whole session. Every `benchctl` verb is a
short, stateless client call to that daemon, and it returns in bounded time. Read results
from `--json` and the exit code, never from the human text.

## Rules that matter

1. **Attaching resets the unit, once.** The bench adapter resets the chip when the port is
   opened, so the daemon opens it once, at attach. Any verb that needs a live unit starts
   the daemon if none is running: `attach`, `mark`, `cmd`, `wait`, `reset`, `monitor`, `run`,
   `identity`. That first call reboots the unit. After that, nothing resets it except
   `reset`, `flash`, `capture --reset`, or the firmware itself. These verbs never attach:
   `units`, `status`, `validate`, `lease`, `detach` and `log`. Run `benchctl attach` first
   when you want the reboot to happen at a known moment.
   `attach` is also what verifies the unit's identity against `units.toml`, and it fails
   with exit 4 on the wrong unit. Skip it and the first mutating verb (`cmd`, `mark`,
   `reset`, …) runs the same check, so that call first waits for the shell to come up.
2. **Mark, then stimulate, then wait since the mark.** Log offsets only grow. Take a mark
   before you cause something, then wait from that mark. A line that arrives between your
   command and your `wait` is still found. Do not wait `--since attach` for a reaction to
   your own action: an older line would match.
3. **Pass `--json` when you parse output.** stdout carries exactly one JSON object.
   Heartbeats (`benchctl: waiting 20/600 s for /pattern/ since 812 (offset now 850)`) go to
   stderr every 10 s; `--quiet` turns them off.
4. **The exit code is the verdict.** Check it before you read fields.

| Exit | Meaning | JSON `error.code` |
|---|---|---|
| 0 | ok | — |
| 1 | assertion failed, timeout, or step failure | `assert`, `timeout` |
| 2 | usage error, invalid profile or scenario, deny-listed command | `usage` |
| 3 | unit busy: another owner holds the lease | `busy` |
| 4 | port, device, esptool or daemon error (identity mismatch included) | `device`, `internal` |

Success is `{"ok": true, "unit": "u1", ...}`. Any other exit prints `{"ok": false, "error":
{"code", "message", "data"}}`. For verbs that produce a result (`cmd`, `wait`, `run`,
`validate`), a failed result still carries its fields next to `error`.

## The core loop

```sh
benchctl attach --json                        # reset once, verify identity, print status
benchctl mark before_cmd                      # prints the offset; the name is the cursor
benchctl cmd version --json                   # send; captures the reply up to the prompt
benchctl wait 'Application Version' --since before_cmd --timeout 10 --json
benchctl log --since before_cmd               # `<offset> +<t> <text>` lines
```

When the device reacts later or on its own:

```sh
benchctl mark before_reboot
benchctl cmd restart --reboots --no-wait     # --reboots: the reset is expected, not a crash
benchctl wait 'shell started' --since before_reboot --timeout 30 --json
benchctl wait --absent 'rst:' --since before_reboot --window 50s --json   # nothing reset since
```

## When to use `run`

Use the ad-hoc verbs above to explore: try a command, look at the log, narrow the bug down.
Write a scenario and use `benchctl run` once the sequence must be repeatable. That covers
fixed timing between steps (`at = "+5s"`), a `human` step that waits for the operator's
action to show up in the log, and an `external` step with an `undo` that always runs. A run
leaves a run dir with `results.md`, ready to paste into a PR, plus `summary.json`, the
`serial.log` slice and `events.jsonl`.

- Validate first with `benchctl run SCENARIO --dry-run`. It checks the scenario and `--var`
  values and prints the worst-case budget without attaching. A real `run` attaches, and
  that resets the unit.
- Keep scenarios under `~/.config/benchctl/profiles/<name>/scenarios/`; `benchctl validate`
  checks them alongside the profile. `examples/scenarios/` in the benchctl repo shows the
  step kinds.

## Cursors

`--since` and `--until` take `attach` (offset 0), `boot` (the most recent reset line), a
non-negative offset, or a mark name. An unknown mark is a usage error (exit 2). Marks live
in the daemon and are lost on `detach`.

## Leases

The first mutating call takes the lease for your owner. The owner is `BENCHCTL_OWNER`, or
else `user@host:<worktree root>`. Mutating calls are `cmd`, `reset`, `flash`, `mark`,
`capture`, `identity` and `detach`. While you hold the lease, another owner's mutating call
fails with exit 3 and the message `busy: held by <owner> since <time>`. Read-only calls
(`status`, `log`, `wait`) always work, so you can watch while a human types. Take over
with `benchctl attach --take` or `benchctl lease --take`. A lease left idle for 30 minutes
(`BENCHCTL_LEASE_STALE_S`) is taken over automatically. Give a lease back with
`benchctl lease --release`. To release it and stop the daemon in one step, run
`benchctl detach`.

## Verbs

Every verb takes `--unit U` (default: the only registered unit), `--profile P`, `--json`
(except `monitor`) and `--quiet`.

| Verb | Use |
|---|---|
| `benchctl units` | registered units, the port each matches, `candidates` (>1 means ambiguous), daemon, lease |
| `benchctl attach [--take] [--no-verify]` | start the daemon (resets once), verify identity, print status. A mismatch exits 4; the daemon stays up but refuses mutating calls until `--no-verify` |
| `benchctl status` | daemon, lease, identity, last reset, offset, gate; `daemon: null` when not attached |
| `benchctl mark [NAME]` | name the current offset, or print it |
| `benchctl cmd LINE` / `--name MAP --arg k=v` | send one shell line. `--expect RE` succeeds on a matching line; `--error RE` fails on one; `--capture` reads up to the prompt; `--no-wait` sends and returns; `--secret` logs the line as `<redacted>`; `--timeout S`. A `status` other than `ok` exits 1 |
| `benchctl wait [PATTERN] --since CURSOR` | block until PATTERN appears. `--absent RE` with `--window S` checks that it stays absent; `--order "A,B"` checks sequence; `--timeout S` (default 60) |
| `benchctl reset [--bootloader]` | reset and report the `rst:` line |
| `benchctl flash ARTIFACTS…` | build dir, `flasher_args.json`, or `0xADDR=file`; `--only app,otadata`, `--after no-reset`, `--baud N`. Uses the daemon when attached, else opens the port itself |
| `benchctl capture --reset --seconds N [--out F]` | reset and collect N seconds of boot output |
| `benchctl log [--since C] [--until C] [--grep RE] [--limit N] [--tail]` | stored lines; with no daemon, reads the newest `serial.log` and says so |
| `benchctl monitor` | interactive: live lines; typed lines are sent; `!reset` resets; Ctrl-] exits |
| `benchctl run SCENARIO [--dry-run] [--var k=v] [--run-dir D] [--skip-gate]` | run a scenario TOML; `fail` exits 1; `--dry-run` validates and prints the budget without attaching |
| `benchctl validate PATH…` | check profiles, `units.toml` and scenarios without hardware; errors exit 2 |
| `benchctl lease [--take \| --release]` | show, take or release the lease |
| `benchctl identity [--reprobe]` | identity fields (`fw_version`, `mac`, …) checked against `units.toml` |
| `benchctl detach` | release the lease and stop the daemon; prints `not attached` when there is none |
| `benchctl daemon --unit U` | internal: the daemon that `attach` spawns. Never run it by hand |

## Things that look like failures

- `cmd` waits for the shell-ready gate, which opens after a reset once the prompt has been
  quiet for a moment. A command sent in the first second after boot is held back, not
  lost. `--force` skips the gate.
- A command that reboots the unit on purpose needs `--reboots`. Without it, the reset is
  recorded as unexpected, with context lines.
- A deny-listed command (for example one that crashes the console task) exits 2 before
  anything is sent.
- Secrets matched by the profile's redaction patterns appear as `***` everywhere: replies,
  `log`, `serial.log`, and events. You cannot read them back.
- `port busy` (exit 4) from `flash` or `capture` means another process holds the port,
  such as `idf.py monitor` or a daemon from another state dir.

## Files

State lives in `~/.local/state/benchctl/<unit>/` (or `BENCHCTL_STATE_DIR`). Each attach
gets a `sessions/<timestamp>/` directory with `serial.log` and `events.jsonl`; both survive
`detach`. A `run` writes to `runs/<unit>/<timestamp>-<scenario>/`: `results.md`,
`summary.json`, a `serial.log` slice, and `events.jsonl`. Paste `results.md` into a PR.
Config lives in `~/.config/benchctl/`: `units.toml` and `profiles/<name>/profile.toml`.
