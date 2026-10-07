# benchctl v1 design

Design for the v1 build of `benchctl`, the bench harness specified in the benchctl v1 spec.
The spec owns scope and requirements; this document owns structure and
contracts. Where the two disagree, fix this document.

Status: design v1, 2026-10-06. Implementers build against this file. Every interface
between units is written here; nothing is left to be negotiated between implementers.

Contracts marked **[portable]** are language-neutral: a Rust port implements them
unchanged. Everything else is Python detail.

---

## 1. Principles

- One daemon per unit owns the port for the whole session. Opening the port resets the
  unit on the bench adapter, so the open happens once, at attach.
- Every client call is stateless and returns in bounded time. Cursors (log offsets) are
  explicit in every request and reply.
- The core knows ESP-IDF (esptool, DTR/RTS reset, `I (ms) TAG:` lines, `rst:` lines, ELF
  SHA). Everything project-specific lives in a profile. No client-specific strings in this
  repo.
- Redaction has one choke point. Raw bytes are never persisted.
- Tests run without hardware against a simulated device on a pty. Only the esptool wire
  protocol is out of reach; it is tested by argv/sequence verification.

---

## 2. Toolchain and repo layout

### 2.1 Toolchain

| Item | Choice | Why |
|---|---|---|
| Python | `requires-python = ">=3.12"`, `.python-version` = `3.13` | `tomllib` (3.11+), `typing` niceties; esptool 5.4 supports 3.10–3.14 |
| Packaging | uv project, `hatchling` build backend (src layout), `[project.scripts] benchctl = "benchctl.cli:main"`, `uv tool install .` | spec |
| Runtime deps | `pyserial>=3.5`, `esptool>=5,<6`, `click>=8,<9` | click is already in esptool 5's dependency closure (`click<9`, `rich_click<2`), so it costs nothing; esptool 4.x (the IDF venv ships 4.10) has different verbs, hence `>=5` |
| Dev deps | `pytest`, `ruff`, `mypy`, `types-pyserial` | |
| Type checker | **mypy** (`strict = true` on `src/` and `tests/`) | pure Python, installs through uv with no Node runtime; `types-pyserial` exists; one `[[tool.mypy.overrides]] module = "esptool.*"  ignore_missing_imports = true`; a sibling project already uses mypy so the team knows its output |
| Lint/format | ruff (`line-length = 100`, `target-version = "py312"`, select `E,F,I,B,UP,SIM,RUF`) | |

### 2.2 Gates (`scripts/gates.sh`)

Cheapest first, fail fast, exit on the first failing step:

```sh
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --quiet
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q
```

### 2.3 Repository layout

```
benchctl/
  pyproject.toml  uv.lock  .python-version  README.md  SKILL.md
  scripts/gates.sh
  docs/design.md
  examples/
    profile.toml                       # generic ESP-IDF profile (no client content)
    scenarios/boot_banner.toml         # reset, wait for boot lines, assert no second rst:
    scenarios/cmd_roundtrip.toml       # cmd with expect/error, mark/wait pattern
    scenarios/external_undo.toml       # external run/undo, human step with confirm
  src/benchctl/
    __init__.py            # __version__
    __main__.py            # python -m benchctl → cli.main()
    types.py               # shared dataclasses + Protocols (Section 4.1)   [scaffold]
    errors.py              # BenchctlError(code) hierarchy → exit codes      [scaffold]
    paths.py               # config/state dir resolution, env overrides     [scaffold]
    durations.py           # "50s"/"15m" parsing                            [scaffold]
    defaults.py            # one home for shared defaults: reset wait, wait timeout, log limit, progress tick, `<redacted>`, iso_now  [U5]
    linebuf.py             # LineEngine: bytes→UTF-8→lines, tail, ANSI      [U2]
    idf.py                 # ESP-IDF line parsing, reset/sha detection       [U2]
    redact.py              # Redactor built from profile patterns           [U2]
    profile.py             # Profile TOML load/validate/lookup              [U3]
    registry.py            # units.toml load + port listing and matching    [U3]
    scenario.py            # Scenario TOML load/validate (schema only)      [U3]
    artifacts.py           # flasher_args.json / build dir / addr=file, app desc, sha  [U3]
    transport.py           # Transport Protocol + PySerialTransport + ModemLines  [U4]
    logstore.py            # LogStore: offsets, memory + serial.log, marks, Condition  [U4]
    gate.py                # shell-ready gate state machine                 [U4]
    session.py             # Session: reader thread, detector, reconnect, cmd/wait/reset  [U4]
    daemon.py              # unix-socket server, ops dispatch, lease, lifecycle  [U5]
    lease.py               # lease.json + lock                              [U5]
    client.py              # Client: connect, auto-attach, request/progress [U5]
    cli.py                 # click CLI, --json rendering, exit codes        [U5]
    monitor.py             # interactive pass-through                       [U5]
    flash.py               # in-process esptool on the held handle; direct mode  [U6; scaffold ships the stub]
    capture.py             # capture --reset (daemon and direct)            [U6; scaffold ships the stub]
    events.py              # EventWriter (events.jsonl), record builders    [U7]
    runner.py              # scenario execution, triggers, undo, run dir    [U7; scaffold ships the stub]
    report.py              # results.md                                     [U7]
  tests/
    conftest.py            # fixtures: fakedev, state_dir, config_dir, profile  [scaffold]
    fakedev.py             # simulated ESP-IDF device on a pty             [scaffold]
    test_scaffold.py       # fixture self-test                             [scaffold]
    test_linebuf.py test_idf.py test_redact.py                            [U2]
    test_profile.py test_registry.py test_scenario.py test_artifacts.py   [U3]
    test_transport.py test_logstore.py test_gate.py test_session.py       [U4]
    test_daemon.py test_client.py test_cli.py test_lease.py fake_daemon.py test_skill_doc.py  [U5]
    test_flash.py test_capture.py                                          [U6]
    test_runner.py test_report.py test_events.py                          [U7]
    integration/           # written after all units merge (Section 9.4)
```

Bracketed tags are file ownership. A unit edits only its own files plus its own tests.
The scaffold's shared files (`types.py`, `errors.py`, `paths.py`, `durations.py`,
`conftest.py`, `fakedev.py`) are frozen once U1 lands; a unit that needs a change there
reports it instead of editing. The scaffold also ships `flash.py`, `capture.py` and
`runner.py` as stubs with the exact signatures of Section 4.13 and `NotImplementedError`
bodies, so `daemon.py`/`cli.py` import and type-check before U6/U7 land; U6 and U7 replace
those files wholesale.

Import dependencies (not the same as file ownership) fix the build order in Section 8:
`session.py` imports U2 and U3; `daemon.py`/`cli.py` import U3 and the U6/U7 entry points;
`runner.py` imports U3's `scenario.py`.

---

## 3. Process model

### 3.1 Directories **[portable]**

| Purpose | Path | Override |
|---|---|---|
| Config | `~/.config/benchctl/` | `BENCHCTL_CONFIG_DIR`, else `$XDG_CONFIG_HOME/benchctl` |
| State | `~/.local/state/benchctl/` | `BENCHCTL_STATE_DIR`, else `$XDG_STATE_HOME/benchctl` |

```
<config>/units.toml
<config>/profiles/<name>/profile.toml
<config>/profiles/<name>/scenarios/*.toml          # optional, by convention
<state>/<unit>/sock                                # AF_UNIX stream socket
<state>/<unit>/daemon.pid
<state>/<unit>/lease.json
<state>/<unit>/lease.lock                          # flock; held by the daemon for its lifetime
<state>/<unit>/daemon.log                          # daemon stderr
<state>/<unit>/sessions/<attach-ts>/serial.log     # one per attach (daemon lifetime)
<state>/<unit>/sessions/<attach-ts>/events.jsonl
<state>/runs/<unit>/<ts>-<scenario>/               # run dirs (Section 7.6)
```

`<attach-ts>` and `<ts>` are `YYYYmmddTHHMMSS` local time.

Unit names are path components: `[A-Za-z0-9][A-Za-z0-9_.-]*`, and `runs` is reserved
(`paths.check_unit_name`, raises `usage`). The registry loader applies the same check.

### 3.2 Roles

- **Client** (`benchctl <verb>`): resolves the unit, connects to the socket, sends one
  request, prints the reply (text or `--json`), exits with the mapped code. Prints a
  heartbeat line to stderr every 10 s while blocked (Section 7.1).
- **Daemon** (`benchctl daemon --unit U`, hidden verb): holds the port, serves the socket,
  logs, flashes, reconnects. Runs until `detach`, SIGTERM, or SIGINT. Idle clients do not
  stop it.
- **Direct mode**: with no daemon, `flash` and `capture --reset` open the port themselves.
  Both reset the unit anyway.
- **Auto-attach resets the unit**, so only verbs that need a live unit do it: `attach`,
  `mark`, `cmd`, `wait`, `reset`, `monitor`, `run`, `identity`. Verbs that never attach:
  `units`, `status` (reports `daemon: null` when none is running), `validate`, `lease`,
  `detach` (prints `not attached`, exit 0), and `log`, which with no daemon reads the most
  recent `sessions/*/serial.log` on disk and says so.

### 3.3 Auto-attach

1. Client connects to `<state>/<unit>/sock`. Success → send `hello`.
2. `ENOENT`/`ECONNREFUSED` → the daemon is dead when the pidfile's process is gone, or when
   that pid is alive but `lease.lock` is free (a reused pid after a reboot; the probe is one
   `LOCK_NB` flock, released at once, and runs only on this refused-socket path) → remove a
   stale socket file → spawn the daemon: `Popen([sys.executable, "-m", "benchctl", "daemon", "--unit", U],
   start_new_session=True, stdin=DEVNULL, stdout=stderr=<state>/<unit>/daemon.log)`.
   `BENCHCTL_DAEMON_ARGV` (space-separated) replaces the program only; `--unit U` is always
   appended. It is the test seam that lets the CLI tests spawn `tests/fake_daemon.py`. The
   spawned daemon inherits the environment, so `BENCHCTL_PORTS_FILE` (Section 7.4) reaches
   it too; the client's `--profile` reaches it as `BENCHCTL_PROFILE`, and every later
   client names its own profile in `hello` so the daemon can refuse another (Section 7.2).
3. Poll for the socket every 100 ms up to `attach_timeout` (default 15 s). Then `hello`.
   A spawned daemon that exits with any status but 3 failed to start: its last stderr line
   in `daemon.log` is the Section 7.1 error JSON, which the client re-raises (an
   unregistered unit is exit 2 at once, not after the poll). Daemon startup order is lock → pidfile →
   session → bind, so a connected client always talks to a live session.
4. The daemon takes the port lock first (`lease.lock`, `flock(LOCK_EX|LOCK_NB)`). Two
   clients racing to spawn: the loser's daemon exits 3 within a second; the loser client's
   poll then finds the winner's socket, or, if the winner dies starting up, re-checks the
   pidfile and spawns again (exit 3 is forgotten, not kept as "our daemon"). No TOCTOU on the port itself: the daemon opens it
   with pyserial `exclusive=True` (POSIX `flock`), so a stray `idf.py monitor` or esptool
   fails fast instead of stealing bytes, and vice versa.
5. `attach` is this same path, run explicitly, followed by `identity` (mutating, so a
   non-owner's `attach` is `busy` unless `--take`); it returns `status`.

### 3.4 Lease **[portable]**

- Owner string: `BENCHCTL_OWNER` if set, else `<user>@<host>:<cwd>` where cwd is the git
  worktree root if inside one, else the plain cwd.
- `lease.json`: `{"owner": str, "since": iso8601, "last_seen": iso8601, "daemon_pid": int}`.
  Written by the daemon on acquire/take, and `last_seen` refreshed in memory on every
  request from the owner, on every progress tick of the owner's blocking op and on every
  tick of its `tail` (a long `wait` or `human` step must not read as idle), and flushed to
  disk every 5 s. The daemon's liveness is
  `lease.lock` + pid, not the heartbeat.
- Every request carries `owner` (in `hello`). The lease is acquired by the owner's first
  mutating op, not by `hello`, so a watcher never holds it. Mutating ops (`cmd`, `reset`,
  `flash`, `mark`, `arm_reset`, `detach`, `identity`, `capture`) from a different owner fail
  with code `busy` (exit 3): `busy: held by <owner> since <since>`; so does `lease
  --release` of a lease that is not stale. Read-only ops (`status`, `log`, `wait`, `tail`,
  `gate`, `resolve`) are allowed to any owner, so an agent can watch while a human types.
- Takeover: `attach --take` or `lease --take` transfers the lease (event `lease`,
  `action: "take"`). Takeover is automatic, without `--take`, when the lease is stale:
  `last_seen` older than `BENCHCTL_LEASE_STALE_S` (default 1800) or the daemon pid dead.
- `detach` releases the lease and stops the daemon. Marks and the in-memory log die with
  the daemon; `serial.log`/`events.jsonl` on disk remain.

### 3.5 Concurrency across units

Every path above is keyed by unit name. Two units are two daemons, two sockets, two state
subtrees, two port locks. The registry match must identify ports without a `/dev` path
(Section 7.4); the integration test runs two fake devices and two daemons side by side.

---

## 4. Shared types and daemon internals

### 4.1 `types.py` (scaffold, frozen)

Dataclasses (all `frozen=True, slots=True`, JSON-serialisable via `dataclasses.asdict`):

```python
Offset = int                      # line index in the daemon log since attach, 0-based
ModemLines = Callable[[bool], None]   # reset strategy: argument True = enter bootloader; default pulses RTS/DTR via esptool.reset
class Cursor: ...                 # parse "boot"|"attach"|"<int>"|"<mark name>" → resolved by Session
@dataclass class LogLine: offset: Offset; t: float; ts: str; text: str; meta: bool   # ts = HH:MM:SS.mmm local, the first token of a serial.log line; t = seconds since attach
@dataclass class ResetInfo: offset: Offset; t: float; code: int|None; reason: str; boot_mode: str|None;
                            elf_sha: str|None; expected: bool; cause: str|None; context: list[str]
@dataclass class Identity: fields: dict[str, str]; verified: bool; mismatches: dict[str, tuple[str, str]]
@dataclass class CmdResult: tx_offset: Offset; status: Literal["ok","error","timeout"]; line: str|None;
                            groups: list[str]; captured: str|None; duration: float
@dataclass class WaitResult: result: Literal["match","timeout","absent_ok","absent_violated","order_ok","order_violated"];
                             line: str|None; groups: list[str]; offset: Offset|None; t: float|None; duration: float;
                             order: list[Offset]
@dataclass class FlashFile: addr: int; path: str; name: str; sha256: str; size: int   # name: app|bootloader|partition-table|otadata|<addr hex> for bare pairs
@dataclass class AppDesc: version: str; project: str; elf_sha256: str; idf_ver: str; time: str; date: str
@dataclass class FlashResult: files: list[FlashFile]; app: AppDesc|None; baud: int; duration: float; reset: ResetInfo|None
@dataclass class GateState: state: Literal["booting","settling","ready","unknown","bootloader"]; since: float; method: str|None
@dataclass class Status: unit: str; pid: int; attached_at: str; port: str|None; connected: bool; baud: int;
                         lease: dict; identity: Identity|None; last_reset: ResetInfo|None; offset: Offset;
                         gate: GateState; session_dir: str
```

Protocols (the seams that let units run in parallel):

```python
class Transport(Protocol):                         # implemented by U4; used by U4, U6
    path: str
    def read(self, max_bytes: int, timeout: float) -> bytes: ...   # b"" on timeout; raises TransportLost
    def write(self, data: bytes) -> None: ...
    def reset(self, bootloader: bool = False) -> None: ...
    def get_settings(self) -> dict: ...
    def apply_settings(self, s: dict) -> None: ...  # restores the saved settings AND discards pending input; callers do nothing more
    def close(self) -> None: ...
    @property def raw(self) -> serial.Serial: ...  # for esptool only

class Session(Protocol):                           # implemented by U4; used by U5, U6, U7(tests)
    profile: Profile; unit: str; session_dir: Path
    def status(self) -> Status: ...
    def mark(self, name: str | None) -> Offset: ...
    def note(self, text: str) -> Offset: ...        # appends the meta line `## <text>` (redacted, occupies an offset); flash begin/end use it
    def resolve(self, cursor: str) -> Offset: ...
    def cmd(self, line: str, *, expect: str|None, error: list[str], capture: bool|None,
            timeout: float, reboots: bool, secret: bool, force: bool, source: str) -> CmdResult: ...
    def wait(self, pattern: str|None, since: Offset, timeout: float, *, absent: str|None,
             window: float|None, order: list[str]|None, progress: Callable[[dict], None]|None) -> WaitResult: ...
    def log(self, since: Offset, until: Offset|None, grep: str|None, limit: int) -> list[LogLine]: ...
    def reset(self, bootloader: bool, wait_rst: float = 5.0, *, cause: str = "reset") -> ResetInfo | None: ...
                                                   # cause is the arming record's cause (Section 4.5); flash passes "flash"
    def set_gate(self, state: Literal["booting", "bootloader"], method: str | None = None) -> None: ...
                                                   # the only gate write from outside gate.py; flash --after no-reset → "bootloader"
    def arm_reset(self, cause: str, window: float) -> None: ...
    def identity(self, reprobe: bool = False) -> Identity: ...
    def gate_check(self, since: Offset, timeout: float) -> dict: ...   # env gate (Section 5)
    def subscribe(self) -> Subscription: ...       # see below
    def device_lock(self) -> ContextManager[None]: ...
    def pause_reader(self) -> ContextManager[Transport]: ...  # parks the reader; yields the transport
    def emit(self, event: dict) -> None: ...       # append to session events.jsonl + subscribers
    def close(self) -> None: ...

class DeviceClient(Protocol):                      # implemented by U5 Client; used by U7 runner
    # one method per socket op in Section 7.2, same names and arguments; raises BenchctlError(code)
```

- `emit` stamps the common fields of Section 7.5 (`seq, ts, t, unit, fw, dev`, and `offset:
  null` when absent). Callers pass `type`, the per-type fields, and `offset` when the record
  refers to one.
- `Subscription` is a concrete class in `types.py`: a bounded queue (`maxsize=10_000`) of
  `LogLine | Event`. `offer(item) -> bool` never blocks; a full queue ends the feed and the
  consumer's next `get(timeout)` raises `BenchctlError(code="slow_client")`. `get` returns
  `None` on timeout; `close()` unsubscribes. Producers drop a feed whose `offer` returned
  False.
- `DeviceClient` return shapes, where the op result is not a single 4.1 dataclass: `hello`,
  `gate`, `lease` return `dict`; `mark` returns `Offset`; `log` returns `(lines, next)`;
  `reset` returns `(offset, reset)`; `capture` returns `(lines, reset)`; `tail` returns a
  `Subscription`; `detach` returns `None`. `since`/`until` accept `Offset | str` (a cursor in
  the 7.1 grammar); `Session` takes `Offset` only, so the daemon resolves strings (7.2).
  `flash` takes `files, *, after, baud, flash_args`; `chip` and `before` come from the
  daemon's profile.

`scenario.py` (U3) owns `Scenario` and `Step` dataclasses; the runner (U7) imports them.

`errors.py`: `class BenchctlError(Exception)` constructed as `BenchctlError(message, *,
code=None, data=None)`; `code` defaults to the subclass's class attribute, and the keyword
exists for the wire-only codes `slow_client`/`protocol` that have no subclass. Subclasses
`UsageError("usage")`, `BusyError("busy")`, `DeviceError("device")`,
`TimeoutError_("timeout")`, `AssertionFailed("assert")`, `InternalError("internal")`, and
`TransportLost(DeviceError)`. `to_dict()`/`from_dict()` are the 7.1 error JSON;
`exit_code(code)` maps per Section 7.1, unknown codes → 4.

### 4.2 Threads inside the daemon

| Thread | Owns | Never does |
|---|---|---|
| main | socket accept loop, signal handling, shutdown | touch the port |
| reader | the only `transport.read()` caller; LineEngine → Redactor → LogStore → Detector/Gate; reconnect loop | block on a client |
| handler (per connection) | one request at a time; `cmd`/`reset`/`flash` under the device lock | read the port directly (reads go through LogStore) |
| housekeeping | lease `last_seen` flush (5 s), gate settle timer, LogStore fsync (1 s) | |

- **Device lock** (`threading.RLock`): held across any TX and its reply capture: `cmd`,
  `reset`, `flash`, identity probing, gate probing. `wait`, `log`, `mark`, `status` do not
  take it, so a watcher is never blocked by a long command.
- **LogStore** is the single rendezvous: reader appends under its lock and notifies a
  `Condition`; waiters block on the Condition with a deadline and rescan from their cursor.
  The reader also publishes the engine's current `tail` into `LogStore.tail` under the same
  lock and notifies on every tail change, so capture-until-prompt and the gate observe the
  prompt without touching the port.
- **Subscribers** (`tail`, runner) get a bounded `queue.Queue(maxsize=10_000)`; on overflow
  the daemon drops that subscriber and closes its connection with error `slow_client`. The
  reader never waits on a queue.
- **Pause** (for flash): `pause_reader()` sets a flag and waits until the reader thread
  acknowledges it is parked (Event, 2 s bound), then yields the transport; on exit it
  resumes. The reader's blocking `read` uses a 100 ms timeout so pause latency is bounded.

### 4.3 Line engine (`linebuf.py`) **[portable semantics]**

Port of hail's `RxBuf`/`take_line`/`strip_ansi`:

- `LineEngine.feed(bytes) -> list[str]`: incremental UTF-8 decode
  (`codecs.getincrementaldecoder("utf-8")(errors="replace")`): a multibyte char split
  across reads is held back, invalid bytes become U+FFFD.
- Line terminators: `\n`, `\r\n`, bare `\r`. A lone trailing `\r` is held back until the
  next byte decides whether it is `\r\n`. Lines are ANSI-stripped (CSI `ESC [ … <0x40–0x7E>`,
  non-letter finals included; an unterminated CSI runs to the end of the line, as in hail)
  and returned without their terminator. `take_line(buf) -> (line, rest) | None` is the
  pure split underneath.
- `LineEngine.tail -> str`: decoded, ANSI-stripped text after the last terminator; `""`
  while a lone `\r` is held, and never includes a held-back partial multibyte char. The
  prompt `demo> ` is **unterminated**; it only ever exists in the tail until the next
  echo completes the line (`demo> version`). Three things match against the tail, not
  lines: capture-until-prompt, the gate's prompt detection, and the prompt probe. There is
  no consuming `read_until`: a marker stays in the tail (hail's test keeps its name and
  asserts this).
- `LineEngine` is per reader: make a fresh one after a reconnect or a flash.
- hail's `classify` (success marker **starts** the trimmed line, error marker is
  **contained**) is not ported as a function: `cmd --expect`/`--error` are regexes, and the
  map's `expect` string is compiled as `re.escape(expect)` anchored with `^\s*`, its `error`
  markers as `re.escape(marker)`, which preserves hail behaviour for existing maps. The
  conversion lives in U3 (`Command.expect_regex`, `Command.error_regexes`); `Session.cmd`
  only ever sees regexes.

### 4.4 ESP-IDF detection (`idf.py`)

- `parse_idf_line(text) -> IdfLine|None`: `^([IWEDV]) \((\d+)\) ([^:]+): (.*)$` → level,
  device_ms, tag, msg. Device ms stays in the stored text; it is parsed for reboot
  inference only.
- Reset line: default pattern `rst:0x(?P<code>[0-9a-fA-F]+) \((?P<reason>[A-Z_0-9]+)\)(?:,boot:0x[0-9a-fA-F]+ \((?P<boot>[A-Z_0-9]+))?`.
  The `boot` group has no closing `\)`: download mode nests parentheses
  (`boot:0x0 (DOWNLOAD(USB/UART0))`) and yields `boot = "DOWNLOAD"`. Profile
  `boot.reset_patterns` are tried after it (each must define `reason`, may define `code`,
  `boot`). An optional `reason` that did not participate reads `"unknown"`; `code` is parsed
  as hex with or without `0x`, `null` when it is not hex.
- ELF SHA line: default `ELF file SHA256:\s*(?P<sha>[0-9a-fA-F]+)`. The device prints a
  prefix (IDF's `CONFIG_APP_RETRIEVE_LEN_ELF_SHA`); it is recorded on the reset and in the
  `elf_sha` event, and nothing in v1 compares it with a build's `AppDesc`.
- Reboot inference: an IDF line whose device_ms is lower than the previous one by more than
  `boot.ms_regression` (default 1000; `0` disables it, for a firmware whose deferred or
  buffered log replays older timestamps) with no reset line in between → synthetic reset
  `reason: "unknown"`, `code: null`, `inferred: true`. Any matched reset line (default or
  profile pattern, IDF-shaped or not) clears the baseline; the next IDF line starts it.
- API: `IdfDetector(reset_patterns, sha_pattern, ms_regression).feed(text) -> Detection |
  None`, with `Detection = (reset: ResetMatch | None, elf_sha: str | None)` and
  `ResetMatch = (code, reason, boot_mode, inferred)`. One line can carry both (a missed
  reset surfacing on the SHA line). U4 builds `types.ResetInfo` from a `ResetMatch` plus
  offset, time, arming and context. `compile_reset_patterns`/`compile_sha_pattern` raise
  `UsageError` and are what U3 validates with. The detector is fed **device lines only**,
  never `## ` meta lines, and is per reader like `LineEngine`.

### 4.5 Reset classification

- On a reset line the Detector emits `reset` (Section 7.5). `expected: true` if an arming
  record is live: `reset` op, `flash` op, a `cmd` whose map entry has `reboots = true` or
  was sent with `--reboots`, or an explicit `arm_reset(cause, window)` (the runner uses it
  for steps with `expect_reset = true`). Arming lasts `boot.expected_reset_window` (default
  10 s) from the arming moment and is consumed by the first reset it classifies, so a second
  reboot inside the window is unexpected (a crash right after a flash must not hide behind
  it). Attach arms `"attach"` before its one explicit pulse. An adapter that also resets on
  open could in principle deliver that boot's `rst:` line first and spend the arm; at
  115200 the line cannot complete before the pulse lands (~5 ms after open), so this is a
  Section 9 bench check, not a code path. A reconnect arms `"port_back"` the same way, after
  its open succeeded and before the reader resumes, because reopening the bench adapter
  resets the unit. `cause` is `"attach"`, `"port_back"`, `"reset"`, `"flash"`,
  `"cmd:<line as logged>"`, or the runner's `"step:<n>"`. Expected resets carry `context: []`.
- Otherwise `expected: false, cause: null, context: <last 20 stored lines before it>`.
- A reset inferred from a device-ms regression (Section 4.4) is a `reset` with `reason:
  "unknown", code: null, boot_mode: null` and `inferred: true` on the event only.
- The reset's `elf_sha` is filled when the SHA line arrives; `last_reset` in `status`
  reflects it. Any reset moves the gate to `booting` (`bootloader` when its boot mode starts
  with `DOWNLOAD`) and sets the `boot` cursor to the reset line's offset.

### 4.6 Shell-ready gate (`gate.py`)

States and transitions:

```
attach / any reset line / reset op            → booting   (t_boot; the op moves it before the pulse)
ready pattern seen (line or tail)             → settling  (t_ready)
t ≥ t_ready + console.settle                  → ready
port_lost, or port_back without a reset line  → unknown
booting for > boot.ready_timeout, or unknown  → probe: send boot.probe + line_ending every
                                                 boot.probe_interval until the prompt is in
                                                 the tail → settling
                                                 (no console.prompt: no probe; → settling at once)
```

- `boot.ready` defaults to the escaped prompt when absent. The firmware today has no stable
  ready line, so the prompt plus `settle = 2.5` (an earlier bring-up finding: shell up at
  ~1.2 s, config init done at ~2.0 s, quiet by ~2.3 s) is the gate.
- `cmd` waits for `ready` within its own timeout; failing that it returns `status:
  "timeout"` with `line` `shell not ready (state=<s>)` (or `device busy` when another op
  holds the device lock for the whole timeout), `offset` = the next offset: nothing was sent
  and no `## tx` line exists. `cmd --force` skips both the ready wait and the bootloader
  refusal (used by `capture`-style debugging only).
- `bootloader`: entered before the pulse of `reset --bootloader`, after `flash --after
  no-reset` or a failed flash, and on any reset line whose boot mode starts with `DOWNLOAD`
  (the chip sits in the ROM bootloader or the flasher stub). No probing; `cmd` fails
  immediately with `device` ("unit is in bootloader; run `reset`"). Left on the next reset
  line or `reset`.
- `cmd` capture semantics: `captured` is the stored device text from `tx_offset + 1` up to
  the prompt appearing in the tail, minus the echo: the first device line that ends with the
  sent text (the shell completes the pending `demo> ` into `demo> version`; an
  asynchronous log line landing first displaces it onto its own line). Once the echo has
  been seen, a device line that starts with the prompt also ends the capture and is not part
  of it: a log line printed right after the prompt turns `demo> ` into `demo> I (5000)
  wifi: …`, and no second prompt follows. Error regexes apply to captured lines too
  (`status: "error"`, `line` = the first hit, `captured` still filled).
- Probing is a TX and takes the device lock; a probe line is logged as a meta line
  (`## probe`) and its reset arming is none.

### 4.7 LogStore and offsets **[portable semantics]**

- Offset = index of a line in the daemon session, starting at 0 at attach; monotonic;
  includes meta lines. `attach` = 0. `boot` = offset of the most recent reset line (or 0 if
  none since attach). A mark is a name → offset mapping kept in the daemon; `mark` with no
  name returns the current offset (`== number of lines stored`, i.e. the offset the **next**
  line will get); `mark NAME` writes `## mark NAME` and returns that line's own offset (a
  meta line, so a wait from it sees everything after). A name that parses as a cursor
  (`attach`, `boot`, digits) is `usage`. Marks do not survive detach.
- Memory holds the whole session (`list[LogLine]`); the file is the durable copy. A
  saturated 115200 line is ~40 MB/hour of text, and at ~250 B per 80-character `LogLine`
  all in (~520k lines/hour) ~130 MB/hour resident; bench rates are far lower, so the design
  does not page or prune. Section 9 records the daemon's RSS after the longest soak.
- `serial.log` line format: `HH:MM:SS.mmm +SSSSS.mmm <text>` where `+t` is seconds since
  attach with three decimals. First line: `## attach unit=<u> port=<p> baud=<b> at=<iso>`.
- Meta lines are benchctl's own and start with `## `: `## tx <line>` (what was sent,
  redacted; `## tx <redacted>` when `secret`), `## reset requested bootloader=true|false`,
  `## flash begin|end …` (written by U6 through `Session.note`), `## mark <name>`,
  `## probe`, `## port lost|back …`, `## reconnect failed <error>` (once per distinct
  error, so a permanent failure is readable in the log). They occupy offsets so `cmd` can return its `tx` offset
  and a `wait --since <that>` cannot miss the reply.

### 4.8 Redaction (`redact.py`)

- `Redactor(patterns).apply(text) -> text`. Each profile pattern is a regex compiled with
  `re.IGNORECASE` off, applied in profile order to every occurrence. With exactly one
  capturing group the match becomes the group's text followed by `***`, wherever the group
  sat (`(sip_pass=)\S+` → `sip_pass=***`; write prefix groups: a suffix group gives
  `domain***`, not `***domain`). With no group, or a group that did not participate, the
  whole match becomes `***`. More than one capturing group is a profile validation error
  (`Redactor()` raises `UsageError`; U3 validates with it).
- A match whose masked part is empty or already only `*` is left unchanged, so a second
  `apply` over its own output is a no-op for ordinary patterns; the tail (redacted before
  prompt matching) and stored lines can both pass through it.
- Applied in exactly one place: the reader, to every line before it reaches LogStore, and
  to every meta line text before it is appended. Replies, captures, waits, `log`, `tail`,
  events, and `results.md` are all derived from stored lines, so they inherit it. The one
  exception is direct-mode `capture`, which has no reader: the CLI redacts its lines
  (Section 4.11).
- `cmd --secret` replaces the TX meta text with `<redacted>` regardless of patterns
  (`wifi set <ssid> <pass>`), and the session adds the exact sent line to a per-session
  literal mask the reader applies, before the patterns, to every device line and meta text
  it stores and to the tail: the shell's echo is stored as `demo> <redacted>`. The mask
  lives as long as the session, because a `--no-wait` echo lands after `cmd` returned and a
  later line repeating the command is as secret as the first. It matches the whole sent
  line only; a shell that echoes it altered (character by character, truncated) is a v1
  ceiling. `monitor` has no secret entry: typed lines are logged as sent.

### 4.9 Transport and reconnect (`transport.py`)

- `PySerialTransport.open(path, baud)`: construct unopened `serial.Serial()`, set `port`,
  `baudrate`, `timeout=0.1`, `exclusive=True`, `dtr=False`, `rts=False`, then `open()`.
  Line state failures (`OSError ENOTTY`, as on a pty) are logged once and ignored.
- `reset(bootloader=False)`: run → esptool's `HardReset(port)()` (RTS pulse, DTR low;
  `uses_usb` stays False even for native-USB units, whose re-enumeration is the reconnect
  loop's job); bootloader → `ClassicReset(port, reset_delay=0.05)()`. Both come from
  `esptool.reset` so the sequence is never re-implemented here. The reset strategy is a
  `ModemLines` callable injected at construction (default pulses the real lines; tests
  inject a hook that calls the fake device's `reset()`), because RTS/DTR ioctls fail on a
  pty. A failed pulse is `TransportLost`.
- Attach: write meta line 0, arm `attach`, discard whatever a reset-on-open already
  printed (`apply_settings(get_settings())`), start the reader, then pulse one reset.
- Loss: pyserial raises `SerialException` ("device reports readiness to read but returned
  no data") or `OSError` when the device vanishes; the transport converts both to
  `TransportLost`. The reader then: emits `port_lost`, closes, and loops every 0.5 s
  (esp-idf-monitor's cadence): re-resolve the port through `registry.list_ports()` +
  `match_port` (the path may change on native USB; zero candidates while unplugged means
  keep looping), open with lines low, emit `port_back {path, gap}`, set gate `unknown`
  (already `unknown` since the loss; a `cmd` awaiting its reply when the port went raises
  `TransportLost`, code `device`). A port esptool closed underneath reads as `TransportLost`
  too, and `apply_settings` tolerates a closed port, so a failed flash recovers this way.
  After `reconnect_timeout` (default 120 s) it keeps trying every 5 s; `status` shows
  `connected: false`. An attempt that fails for a new reason (two ports now match, the unit
  came back with another VID) writes `## reconnect failed <error>` and a warning to
  `daemon.log`, once per distinct message. A successful reopen arms `"port_back"` (Section
  4.5) before the reader resumes. Waits are bounded by their own timeouts regardless.

### 4.10 Flashing on the held handle (`flash.py`)

Facts from esptool 5.4 that fix the sequence (`esptool/__init__.py: prepare_esp_object`,
`cmds.py: reset_chip`):

- `esptool.main(argv, esp=esp)` with an external `esp` **skips `connect_esp`**, runs the
  stub (`run_stub` returns a *new* loader object bound to the same `_port`), calls
  `esp.change_baud(baud)` when `-b` exceeds 115200, runs the command, and in teardown calls
  `reset_chip(esp, after)` but **does not close the port**. The pyserial handle is left at
  the flash baud.
- `--before no-reset` still matters with an external `esp`: it sets `initial_baud =
  min(115200, baud)`, which is what makes `change_baud` fire (`no-reset-no-sync` would not).
- `reset_chip(esp, "no-reset")` on the stub calls `soft_reset(True)`: the chip **reboots into
  the ROM loader** and prints its banner. `"no-reset-stub"` sends nothing. The teardown also
  runs after a failed command, so a `no-reset` soft reset to a wedged stub can replace the
  real error.
- `ESPLoader.connect()` **closes the port** when every attempt fails.
- `esptool.main` re-raises a non-zero `SystemExit` from click (a usage error in our argv).

Daemon flash op. `flash_on_session` takes `session.device_lock()` itself (an RLock, so the
daemon may already hold it). Bad input (`after` not in `hard-reset`/`no-reset`, unknown chip,
`flash_args` without exactly the three keys, an unreadable file) is `UsageError` before any
session call.

The **client** resolves artifacts (`artifacts.resolve(args) -> list[(addr, path, name)]`,
`--only` applied) and sends `files: [{addr, path, name}]`; the daemon hashes each file and
parses `AppDesc` from the entry with `name == "app"`.

```
1. files = [FlashFile(addr, path, name, sha256(path), size)]; app = appdesc(files["app"]) if present
   begin = session.note(f"flash begin {' '.join(f.name for f in files)} baud={baud} after={after}")
2. with session.pause_reader() as t:
3.     settings = t.get_settings()
4.     with redirect_stdout(EventLineWriter(session, "flash_log")):   # connect + write output → events, never the tty
5.         esp = esptool.targets.CHIP_DEFS[profile.flash.chip](t.raw, 115200)
6.         esp.connect(profile.flash.before)            # "default-reset": DTR/RTS dance into the ROM bootloader
7.         argv = ["--chip", chip, "--baud", str(flash.baud), "--before", "no-reset", "--after", "no-reset-stub",
                   "write-flash", "--flash-mode", m, "--flash-size", s, "--flash-freq", f, *[hex(addr), path, ...]]
8.         esptool.main(argv, esp=esp)                  # FatalError / OSError / StopIteration / SystemExit → DeviceError(exit 4)
9.     t.apply_settings(settings)                  # console baud back, pending input discarded (chip is parked in the stub: only SLIP leftovers)
       on failure the restore runs too but its own error is dropped: connect() closed the port, and U4's reconnect reopens it
10. reader resumed. session.note(f"flash end status={ok|error} duration={d:.1f}")   # duration = connect + write
    error:                 session.set_gate("bootloader", method="flash"); emit flash event {status: error, error}; re-raise
    after == "hard-reset": FlashResult.reset = session.reset(False, wait_rst=5.0, cause="flash")
    after == "no-reset":   session.set_gate("bootloader", method="flash"); FlashResult.reset = None
11. emit flash event (offset = begin)
```

- esptool never resets the chip on the held handle: `--after no-reset-stub`. A hard reset
  would deliver the ROM banner at the flash baud while the reader is parked, and plain
  `no-reset` soft-resets the stub into the ROM loader, whose banner arrives after the
  reader resumes as an unarmed `reset`. The RTS pulse of `session.reset` and the next
  `connect("default-reset")` both work from the stub. Resetting through `session.reset`
  after the reader resumes makes the arming, the gate move, and the `ResetInfo` the
  ordinary reset path.
- Every failure, including one in `pause_reader` itself, ends with `## flash end
  status=error`, the `flash` event, and the gate in `bootloader`: the chip is in the stub,
  the ROM loader, or somewhere unknown, so `cmd` must refuse until an explicit `reset`.
  When the failure closed the port (a failed `connect`), the reader's reconnect moves the
  gate on to `unknown` and probes; a chip in the ROM loader ignores the probe, so `cmd`
  still fails (`shell not ready (state=unknown)`, exit 1 rather than 4) until `reset`.
  esptool's errors become `DeviceError`; anything else is re-raised unchanged.
- `flash_log` lines are esptool's stdout, not stored lines, so they bypass redaction (they
  carry no device output).
- `flasher_args.json` uses underscore flags (`--flash_mode`); `artifacts.py` normalises to
  esptool 5 hyphen names itself rather than relying on the deprecation shim.
- `after` is `hard-reset` by default and `no-reset` when `--after no-reset` (for an OTA test's
  otadata + app writes followed by a controlled reset through `reset`).
- `AppDesc` is parsed from the app `.bin` at byte offset 32 (24-byte image header + 8-byte
  segment header) with struct `<II8s32s32s16s16s32s32s` and magic `0xABCD5432`: version,
  project, time, date, idf_ver, `app_elf_sha256` (full 64 hex). Signed images keep the
  header, so this works on the signed artifacts the bench flashes.
- Direct mode (no daemon): `esptool.main(argv_with("--port", path, "--before", before,
  "--after", after))`, letting esptool open, reset and close the port (nobody is reading, so
  esptool's own after-reset is fine); its stdout goes to stderr so `--json` stays clean. If
  the daemon holds the port the `exclusive` open fails → `DeviceError("port busy: <path> is
  locked by another process …")`, exit 4 (the unit name is not known here). `FlashResult.reset`
  is `None`. The client only takes the direct path when no socket answered.

### 4.11 Capture (`capture.py`)

- `seconds <= 0` is `UsageError` in both modes.
- Daemon mode: `start = session.mark(None)`, then `session.reset(False, wait_rst=min(N, 5.0))`,
  sleep out the remainder of N, `end = session.mark(None)`, return `session.log(start, …)`
  up to `end`. The window starts at the reset request so it holds the `## reset requested`
  meta line and the ROM banner that precedes `rst:`, as direct mode does. No device lock is
  held across the window; the lines are stored lines, so redacted.
- Direct mode: open the port, reset through `lines` (default: esptool `HardReset` on the
  handle), read for N seconds through `linebuf.LineEngine`, return `LogLine`s with `offset`
  counted from 0 and `t` since the reset call. Output format equals `log`'s text format.
  `capture_direct` opens `serial.Serial` itself with the Section 4.9 settings and never
  imports `transport.py`; it is the one second opener in the tree, so U6 depends on U2 but
  not on U4. A locked port is `DeviceError("port busy: …")`; a port that vanishes
  mid-capture is `DeviceError`. **Its lines are unredacted**: the CLI applies the profile's
  `Redactor` before printing (the one exception to Section 4.8's single site).

### 4.12 Identity (`session.identity`)

- On the first `identity()` call (after the gate reaches `ready`; the session does not run
  it by itself — the daemon calls it from `attach` and before the first mutating op when
  the session has not probed yet, so an auto-attaching `cmd` or `mark` is verified too),
  run each `[[identity.field]]`: `source = "boot"` →
  regex over stored device lines since `boot`; `source = "cmd"` → `cmd(send, capture=True)`
  (without a prompt: `cmd(send, expect=<regex>)` and the matched line) then optional
  `filter` (shell command, stdin = capture, stdout = text to match) then regex group 1. A
  field that yields nothing is absent from `fields` and compares as `""`. Cached until
  `reprobe`.
- Compare with `units.<u>.expect` (Section 7.4). Any mismatch → `Identity.verified = false`,
  event `identity` with `mismatches`, and **attach fails with exit 4** (`identity mismatch:
  mac expected X got Y`). The daemon stays up and keeps the port (no other process grabs
  it, no second reset) but is **unverified**: every mutating op is refused with code
  `device` and the same message until `attach --no-verify` clears it (event `identity`,
  `verified: true, override: true`) or `detach`. Read-only ops still work so the operator
  can inspect what is actually plugged in.
- `fw_version` (`fw`) and the device id (`dev`: `expect.id_field` when set, else the first
  present of `mac`, `cert_cn`) tag every event emitted after identity ran.

### 4.13 Cross-unit entry points

The exact public signatures `daemon.py`/`cli.py` call. The scaffold ships them as stubs.

```python
# flash.py (U6)
def flash_on_session(session: Session, files: list[tuple[int, str, str]], *, chip: str,
                     before: str, after: str, baud: int, flash_args: dict[str, str]) -> FlashResult: ...
def flash_direct(port_path: str, files: list[tuple[int, str, str]], *, chip: str,
                 before: str, after: str, baud: int, flash_args: dict[str, str]) -> FlashResult: ...
# files: (addr, path, name); flash_args: {"flash-mode": "dio", "flash-size": "keep", "flash-freq": "80m"} already normalised by artifacts.py

# capture.py (U6)
def capture_on_session(session: Session, seconds: float) -> tuple[list[LogLine], ResetInfo | None]: ...
def capture_direct(port_path: str, baud: int, seconds: float, lines: ModemLines | None = None) -> list[LogLine]: ...

# runner.py (U7)
@dataclass class RunResult: run_dir: Path; result: Literal["pass","fail","precondition_failed"];
                            budget: dict[str, float | None]; steps: list[dict[str, object]]
                            def to_json(self, unit: str) -> dict[str, object]: ...   # the 7.1 `run` result fields with `ok`/`unit`; summary.json
def run_scenario(client: DeviceClient, scenario: Scenario, profile: Profile, *, run_dir: Path,
                 variables: dict[str, str], skip_gate: bool, dry_run: bool,
                 prompt_out: TextIO) -> RunResult: ...
# Raises UsageError before any client call for a missing/unused --var, an unknown map command, a bad
# template, or a denied line. dry_run stops after those checks and the budget line: no client call, no run dir.
def compute_budget(scenario: Scenario, profile: Profile) -> float: ...   # profile supplies the cmd default (Section 7.8)

# artifacts.py (U3), used by cli.py and runner.py
def resolve(specs: list[str], only: list[str] | None, overrides: dict[str, str]) -> tuple[list[tuple[int, str, str]], dict[str, str]]: ...
def read_app_desc(path: str) -> AppDesc | None: ...
def sha256_file(path: str) -> tuple[str, int]: ...
# resolve: paths come back absolute, sorted by address; relative specs resolve against the cwd,
# flasher_args.json entries against the json's directory; flash_args always has all three keys
# ("keep" when the build sets none); overrides take hyphen or underscore keys.

# profile.py / registry.py / scenario.py (U3)
def load_profile(name_or_path: str | None, cwd: Path, config_dir: Path) -> Profile: ...
def render(template: str, args: dict[str, str]) -> str: ...          # hail's render: missing/unused args are errors
def placeholders(template: str) -> list[str]: ...                    # the {name}s of a template, for subsetting --var
def load_registry(config_dir: Path) -> Registry: ...
def list_ports() -> list[ListPortInfo]: ...   # the only comports() caller; BENCHCTL_PORTS_FILE seam (Section 7.4); a missing or malformed ports file is DeviceError
def match_port(spec: dict[str, str], ports: list[ListPortInfo]) -> str: ...   # raises DeviceError naming candidates
def matching_ports(spec: Mapping[str, str], ports: list[ListPortInfo]) -> list[ListPortInfo]: ...   # `units` candidates count
def load_scenario(path: Path) -> Scenario: ...
def validate_paths(paths: list[Path]) -> list[dict[str, object]]: ... # the `validate` verb's result rows
# Invalid config raises ConfigError(UsageError) with data["errors"], one "<file>: <path>: <message>" each.
# On the loaded objects: Registry.unit(name | None) (None = the only unit, else usage);
# Profile.command(name), Profile.check_allowed(line) (usage "denied: <reason>"), Profile.flash.chip,
# FlashConfig.overrides (hyphen keys, for resolve), Command.expect_regex / Command.error_regexes
# (Section 4.3), Step.flash_specs (a flash step's inputs in resolve form).
```

---

## 5. Scenario runner (`runner.py`)

- Loads profile (`--profile`, scenario's `profile`, unit's default) and scenario
  (`scenario.py` validates; the runner trusts validated input).
- Announces the budget: `budget` if declared, else the sum of step timeouts plus `at`
  offsets; prints `worst case: 25m (declared) / 23m40s (computed)` and warns when the
  declared value is smaller than the computed one.
- Before the preconditions: `status` (unit, attach time), then `identity` — the probe runs
  here, not lazily inside the first `mark`, so `fw`/`dev` tag the run's records and
  `results.md` even when the run itself attached — then the first `mark`, and only then the
  run dir (a failure in those three calls leaves no half-written run dir behind).
- Preconditions: `fw_version` regex against `identity.fw_version`; `env_gate = true` runs
  `gate_check(since=boot, timeout=env_gate.timeout)` from the profile. Failures stop the run
  before step 1 (result `precondition_failed`, exit 1).
- Steps run sequentially. `at = "+Ns"` sleeps until `trigger_t + N` where `trigger_t` is
  the match time of the most recent trigger (a `wait` or `human` step with `trigger =
  true`, the default for both); with no trigger yet, from scenario start. A step without
  `at` starts when the previous step ends. The trigger moves only on a matched line
  (`match`/`order_ok`, the last match); `absent_ok` leaves it. The matched line's device `t`
  is mapped onto the runner's clock from the streamed items' arrival times, so `trigger_t`
  can be late by the delivery delay but never early; the wait's return time is the ceiling.
- `wait.since` resolution: `"prev"` (default) = offset recorded at the start of the previous
  step; `"step"` = now; `"trigger"` = offset of the last trigger match (the run's first
  offset before any); `"boot"`, `"attach"`, or a mark name. The step-start offset is taken
  after the `at` sleep.
- `expect_reset = true` arms `arm_reset("step:<n>", span + boot.expected_reset_window)` at
  step start, where `span` (`runner.step_span`) is the step's timeout or a wait's declared
  `window` when longer; put it on the step during which the `rst:` line is expected to
  arrive. `compute_budget` sums the same spans. On such a step a `cmd` is sent with
  `reboots = false` whatever its map entry says: the session's own `cmd:` arm would replace
  the step's wider one.
- `human`: print the prompt (stderr and the terminal bell), then `wait(confirm, since=step,
  timeout)`. The human never says "done".
- `external`: shell command in its own process group (`start_new_session`), env merged over
  the runner's, output tails redacted; a timeout or an interrupt kills the group. Non-zero
  exit fails the step. On success, if `undo` is set, push `(undo, cwd, env, timeout)`.
- Undo: at scenario end, on failure, and on SIGINT/SIGTERM, pop and run every undo in
  reverse order, each bounded by its timeout; an undo failure is recorded (`external`
  event, `phase: "undo"`, and a note in `results.md`) and does not stop the others. The run
  result is `fail` if any step or undo failed.
- Step results: `fail` is an assertion the device did not meet (timeout, error line, no
  `rst:` after a `reset`, non-zero exit); `error` is a `BenchctlError`/`OSError` or an
  interrupt; steps after a stop are `skip` with the reason in `detail` (`not run: step 3
  failed`), and after a failed precondition every step is `skip`. `continue_on_fail` keeps
  going after `fail`/`error`, but never after an interrupt.
- SIGINT/SIGTERM anywhere from the preconditions to the report (during a step, between
  steps, during the preconditions, during an undo, during the final log fetch): the current
  step (or undo) becomes `error`, the remaining steps `skip`, the remaining undos still run,
  and `run_scenario` returns `fail` with the run dir written; it does not re-raise. Before
  the preconditions (`status`, `identity`, the first `mark`, then run dir setup) there is nothing to undo and
  the interrupt propagates. SIGTERM is caught only in the main thread and the previous
  handler is restored on return.
- Output: run dir (Section 7.6). The runner subscribes to the daemon's event stream for the
  run's duration and is the **sole writer** of the run dir's `events.jsonl` (daemon events
  passed through, plus the runner's own `run`, `step`, `human`, `external` records). The
  daemon's own session `events.jsonl` keeps everything regardless.

---

## 6. Testing strategy

### 6.1 Fake device (`tests/fakedev.py`, scaffold)

`FakeDevice(script: DeviceScript, timescale=0.05)`:

- Owns a pty master; `path` is the slave (`os.ttyname`). A thread reads TX bytes, splits on
  the configured line ending (default `\r`), echoes them like a shell, dispatches commands.
- `reset(bootloader=False)`: prints the ROM banner and the `rst:` line itself (`rst:0x1
  (POWERON)` for `reset()`, `rst:0xc (RTC_SW_CPU_RST)` for a software reboot, `boot:0x0
  (DOWNLOAD…)` plus `waiting for download` for `bootloader=True`), then replays the boot
  script: a list of `(delay_s, bytes)` with ESP-IDF-shaped lines (ANSI-coloured `I (ms) TAG:`
  lines with increasing ms, `ELF file SHA256:  <16 hex>`, a line containing `°`), then the
  prompt `demo> ` **without** a terminator. A script cannot omit the `rst:` line; the
  ms-regression inference is tested on `idf.py` alone. Writes are deliberately split
  mid-UTF-8 and between `\r` and `\n` to exercise the line engine. Timings scale by
  `timescale`. The device is silent until the first `reset()`.
- Boot behaviours: commands within `blind_s` after reset are dropped; commands matching
  `reboot_window_cmds` sent inside `[win_start, win_end]` after reset trigger a reboot with
  `rst:0xc (RTC_SW_CPU_RST)`; `reboot` triggers a software reset; `version` answers
  `Application Version: 1.2.3-4-gabc`; `secret` answers a line containing `sip_pass=hunter2`;
  `slow` answers after 2 s; unknown commands answer `command not found`. Every reply ends
  with the prompt.
- `vanish()` closes the master (the slave then raises on read, as the real port does);
  `reappear()` opens a new pty pair and returns the **new** path, so reconnect must
  re-resolve. `reappear()` does **not** replay boot (shell up, no `rst:`): that is the
  `port_back → unknown → probe` path of 4.6. A test that wants the native-USB sequence calls
  `reappear()` then `reset()`.
- Each fake carries a stable `serial_number` (`FAKE0001`…) and Espressif USB-Serial/JTAG
  `vid`/`pid`; `port_info()` returns the `ListPortInfo` `comports()` would. `present` is
  True while the pty exists; `on_change` (constructor argument) is called after every
  open/vanish/reappear/close. `received` (`RxRecord(line, outcome, since_reset)`) and
  `resets` record what happened for assertions.
- Fixtures (`conftest.py`): `make_fakedev`/`fakedev`; `state_dir` (short path under `/tmp`,
  AF_UNIX's 104-byte cap) and `config_dir` (`tmp_path`), exported as
  `BENCHCTL_STATE_DIR`/`BENCHCTL_CONFIG_DIR`, both autouse so no test touches the real
  trees; `ports_file` (writes the 7.4 ports file for every present fake made through
  `make_fakedev`, re-written automatically through `on_change`, exported as
  `BENCHCTL_PORTS_FILE`); `registry` (writes `units.toml` registering `u1` with
  `match = { serial = <fakedev.serial_number> }`, which survives `reappear()`); `profile`
  (`install_profile(config_dir)` then U3's `load_profile`); `fake_lines` (ModemLines hook
  bound to `fakedev.reset`).
- `install_profile(config_dir, source=examples/profile.toml, timescale)` fits the generic
  example to the fake and installs it as `<config>/profiles/esp-idf/profile.toml`: forces
  `profile.name = "esp-idf"`, sets `console.prompt` to the fake's prompt, drops `boot.ready`
  (so it defaults to the prompt), multiplies `boot.settle`/`ready_timeout`/`probe_interval`
  by `timescale`, and appends a `sip_pass` redaction when none exists. It writes TOML through
  `conftest.dump_toml` (floats via `repr`, strings as JSON-escaped basic strings, dicts as
  tables, lists of dicts as table arrays). U3's example and validator must accept that
  output.

### 6.2 Per-unit tests

| Unit | Tests against | What they prove |
|---|---|---|
| U2 | pure functions | hail's cases ported one-to-one (Section 8, U2), IDF parsing, redaction rules |
| U3 | TOML strings, synthetic `.bin` with an app-desc struct, a temp build dir with `flasher_args.json` | load/validate/lookup, port matching, argv normalisation, sha/app-desc |
| U4 | `fakedev` through the real `PySerialTransport` | offsets, gate transitions, reset classification, cmd/wait semantics, vanish/reappear |
| U5 | `tests/fake_daemon.py` (real `daemon.py` serving a `FakeSession`), spawned via `BENCHCTL_DAEMON_ARGV`; `fake_daemon.py` replaces `flash.flash_on_session`, `capture.capture_on_session` and the CLI test process replaces `runner.run_scenario` with canned results, so U5 needs only the scaffold's stub signatures | protocol, lease/busy/take, auto-attach race, every verb's `--json` shape and exit code, two state dirs in parallel, SKILL.md covers every verb |
| U6 | injected fake `esptool.main` + a `FakeSession`/`FakeTransport`; `fakedev` for direct capture | argv, pause/settings-restore then `session.reset(cause="flash")` or `set_gate("bootloader")`, failure path still resumes the reader, hashes |
| U7 | `FakeClient` implementing `DeviceClient` | triggers/`at`, since resolution, undo order on failure and SIGINT, human confirm, run dir contents, results.md, budget |

### 6.3 Rules

- No test sleeps longer than 1 s wall time; timings go through `timescale`.
- No test needs hardware, network, or esptool's wire protocol.
- Tests are deterministic: fakedev scripts are fixed; time-based assertions use bounds, not
  equality.

### 6.4 Integration (after all units merge; `tests/integration/`)

Written in the QA pass, not by a unit: real daemon on `fakedev` end to end through the
CLI; two fake devices, two daemons, two sessions issuing interleaved `cmd`/`wait`; `run`
of each example scenario; redaction visible in `serial.log` and absent from `--json` output.

---

## 7. Contracts

### 7.1 CLI **[portable]**

Invocation: `benchctl <verb> [options] [args]`. Every verb accepts `--unit U`
(`BENCHCTL_UNIT`; defaults to the only registered unit when exactly one exists), `--json`,
`--profile P` (`BENCHCTL_PROFILE`), `--quiet` (no stderr heartbeat), `--timeout S` where a
wait is involved. `monitor` has no `--json`.

The daemon redacts and deny-lists with the profile it was spawned with, so a verb that
loads a profile for its own rendering and deny check (`cmd`, `monitor`, `run`, `flash`)
names that file in `hello`, and another file than the daemon's is refused with `usage`:
`daemon for U runs profile Y (<path>); this command resolved X; pass --profile Y or detach
first`. Every other verb (`status`, `detach`, `mark`, `wait`, `log`, `reset`, `identity`,
`lease`, `capture`) names none and is never refused, whatever `--profile` says, so the way
out is always open.

Exit codes: `0` ok · `1` assertion failed / timeout / step failure · `2` usage, invalid
profile or scenario, deny-listed command · `3` unit busy (lease) · `4` port, device,
esptool or daemon error.

Error JSON (any verb, any non-zero exit except `monitor`):
`{"ok": false, "error": {"code": "usage|busy|device|timeout|assert|internal", "message": str, "data": {...}}}`
on stdout; code → exit: `usage`→2, `busy`→3, `device`/`internal`→4, `timeout`/`assert`→1.
A verb whose *result* is the failure (`cmd` not ok, `wait` timeout or violation, `run` not
pass, `validate` errors) prints `{"ok": false, "unit", <its result fields>, "error": {...}}`,
so the fields stay readable next to the verdict. `unit` is `null` when the verb loaded no
unit (`validate`).

Success JSON is `{"ok": true, "unit": str, ...}` with verb fields below. Text mode prints
the same information in one or a few lines; the heartbeat (`benchctl: waiting 20/600 s for
/pattern/ since 812 (offset now 850)`) goes to stderr every 10 s in both modes.

| Verb | Options / args | JSON result fields |
|---|---|---|
| `units` | | `units: [{unit, profile, port: str\|null, candidates: int, daemon: bool, lease: {owner, since, last_seen}\|null}]` — `candidates > 1` flags an ambiguous match |
| `attach` | `--take`, `--no-verify`, `--timeout S` (daemon start bound, default 15) | the `status` object; identity mismatch exits 4 with the `Identity` in `error.data.identity` |
| `detach` | | `detached: true`; `detached: false` when no daemon was running (exit 0) |
| `status` | | `daemon: {pid, since, port, connected, baud}, profile: {name, path}, lease, identity: {fields, verified, mismatches}, last_reset: ResetInfo, offset, gate: {state, since, method}, session_dir` — every field `null` when not attached |
| `mark` | `[NAME]` | `offset, name: str\|null` |
| `cmd` | `LINE` or `--name MAP [--arg k=v]…`, `--expect RE`, `--error RE` (repeatable), `--capture`, `--no-wait`, `--reboots`, `--secret`, `--force`, `--timeout S` (default `console.timeout`). Default mode with none of `--expect`/`--capture`/`--no-wait`: capture to the prompt when the profile has one, else fire-and-forget | `offset` (tx), `status: ok\|error\|timeout`, `line`, `groups`, `captured`, `duration` — `status != ok` exits 1 |
| `wait` | `[PATTERN]`, `--since CURSOR` (required), `--timeout S` (default 60), `--absent RE`, `--window S` (sent only when given; see the rules below), `--order "A,B,…"` | `result, line, groups, offset, t, duration, order: [offsets]` — `timeout`/`*_violated` exit 1 |
| `reset` | `--bootloader`, `--timeout S` (rst-line wait, default 5) | `offset` (before the request), `reset: ResetInfo\|null` |
| `flash` | `ARTIFACTS…` (build dir \| `flasher_args.json` \| `addr=file` pairs), `--only name,…` (`app,bootloader,partition-table,otadata`), `--after hard-reset\|no-reset`, `--baud N` | `files: [FlashFile], app: AppDesc, baud, duration, reset` |
| `capture` | `--reset` (required in v1), `--seconds N`, `--out FILE` | `lines: [LogLine], reset: ResetInfo\|null` |
| `log` | `--since CURSOR` (default `attach`), `--until CURSOR` (exclusive), `--grep RE`, `--limit N` (default 10000), `--tail` (follow; text mode only) | `lines: [{offset, t, ts, text}], next: offset`; offline (no daemon) adds `file` and resolves only `attach`, `boot` and offsets — a mark name is `usage` |
| `monitor` | | interactive: live lines; typed lines are sent as `cmd --no-wait` (logged `source: "monitor"`, never secret: a password goes through `cmd --secret`); `!reset` resets; `Ctrl-]` exits. Line-oriented in v1 (no device-side tab completion) |
| `run` | `SCENARIO`, `--run-dir D`, `--dry-run` (validate + budget only), `--skip-gate`, `--var k=v` (template vars for `send`/`run`) | `run_dir, result: pass\|fail\|precondition_failed, budget: {declared, computed}, steps: [{index, name, kind, result, duration, detail}]` — `fail` exits 1 |
| `validate` | `PATH…` (profile dir, `profile.toml`, scenario file; a dir with `scenarios/` validates them all) | `files: [{path, kind: profile\|scenario\|units, ok, errors: [str]}]` — any error exits 2; no hardware |
| `lease` | `--take` \| `--release` | `lease: object\|null` (`null` after release or with no daemon) |
| `identity` | `--reprobe` | `Identity` (`fields, verified, mismatches`); a mismatch on an un-overridden daemon exits 4 |
| `daemon` | `--unit U` (hidden; foreground) | — |

Cursor grammar (`--since`, `--until`): `attach` · `boot` · a non-negative integer · a mark
name. Unknown mark → `usage`.

`wait` argument rules: `PATTERN` is required unless `--order` or `--absent` is given;
`PATTERN` and `--order` are exclusive (`usage`); `--absent` alone checks absence over
`--window` (default: the timeout); `PATTERN` + `--absent` requires the pattern to match and
the absent pattern not to appear on or before the matching line (a line that does both is
`absent_violated`), and a given `--window` then extends the absence
watch to `window` seconds from the call even past the match, so the wait returns at the
later of the two. `--window` means nothing without `--absent`: a plain pattern or order
wait returns at its match whatever window was sent. `--order` requires each pattern to
match after the previous match; `order_violated` (a later pattern seen before an earlier one
that never followed) is decided at the timeout, and a pattern never seen is `timeout`. All
matches are `re.search` on stored **device** lines; `## ` meta lines are never matched
(`log --grep` does include them).

Deny-list: a `cmd` whose rendered line matches a `[[deny]]` pattern is refused with code
`usage`, message `denied: <reason>`, exit 2; nothing is sent.

Text output formats: `log`/`capture` print `<offset> +<t> <text>`; `mark` prints the
offset; `cmd` prints the captured text or the matched line; `wait` prints the matched line
(prefixed by its offset).

### 7.2 Socket protocol **[portable]**

- Transport: `AF_UNIX`, `SOCK_STREAM`, path `<state>/<unit>/sock`. Framing: one JSON
  object per line (`\n`), UTF-8, no embedded newlines.
- One request in flight per connection. A client that needs parallel ops opens another
  connection. `tail` streams until the client closes the connection.
- Request: `{"id": str, "op": str, "args": {…}}`. First request must be `hello`.
- Replies, correlated by `id`:
  - `{"id", "ok": true, "result": {…}}`
  - `{"id", "ok": false, "error": {"code", "message", "data"}}` — codes as in 7.1 plus
    `slow_client`, `protocol` (both map to exit 4).
  - `{"id", "progress": {"elapsed": float, "offset": int, "note": str}}` every 5 s during
    blocking ops (`cmd`, `wait`, `flash`, `gate`, `identity`, `reset`, `capture`, and any op
    that triggers the first identity probe) so the client can distinguish a slow wait from
    a dead daemon; the client gives up (`device`) after 30 s without any frame.
  - `{"id", "line": LogLine}` and `{"id", "event": Event}` for `tail`.
- Ops and args (`result` is the dataclass from 4.1 as JSON unless stated):

| op | args | result |
|---|---|---|
| `hello` | `owner, client, profile?` (the resolved `profile.toml` path the client will render and deny-check with; absent when the verb loaded none) | `daemon: str, proto: 1, unit, offset, lease, owner_ok: bool, profile: {name, path}`; a `profile` that is not the daemon's own file (paths compared resolved) is `usage` and the connection stays un-greeted |
| `status` | | `Status` |
| `mark` | `name?` | `offset, name` |
| `resolve` | `cursor` | `offset` |
| `cmd` | `line, expect?, error: [], capture?, timeout, reboots, secret, force, source` | `CmdResult` |
| `wait` | `pattern?, since, timeout, absent?, window?, order?` | `WaitResult` |
| `log` | `since, until?, grep?, limit` | `lines: [LogLine], next` |
| `reset` | `bootloader, wait_rst` | `offset, reset` |
| `arm_reset` | `cause, window` | `{}` |
| `flash` | `files: [{addr, path, name}], after, baud, flash_args: {flash-mode, flash-size, flash-freq}` (client-resolved) | `FlashResult` |
| `capture` | `seconds` | `lines, reset` |
| `identity` | `reprobe, override` (`attach --no-verify`: accept a mismatch for this daemon's life) | `Identity`; a mismatch without override is `device` |
| `gate` | `since, timeout` | `ok: bool, missing: [str], forbidden_hits: [{pattern, line, offset}]`; returns early only on a forbidden hit or once every `require` entry is found, so a forbid-only gate watches the whole `timeout` |
| `tail` | `since, events: bool` | stream |
| `lease` | `action: take\|release` | `{"lease": object\|null}` |
| `detach` | | `{}` then the daemon exits |

Owner enforcement per 3.4: `hello` records the connection's owner; mutating ops check it.

`since`/`until` on the wire are an int offset or a cursor string in the 7.1 grammar. The
daemon resolves strings through `Session.resolve` before dispatch; an unknown mark is
`usage`.

### 7.3 Profile TOML **[portable]**

```toml
[profile]
name = "esp-idf"              # required
chip = "esp32s3"              # esptool chip name; required for flash (loaded as Profile.flash.chip)

[console]
baud = 115200                 # default 115200
line_ending = "\r"            # TX line ending; default "\r\n"
prompt = "demo> "          # optional; enables capture-until-prompt and prompt gating
timeout = 8.0                 # cmd reply timeout, seconds; default 5.0 (hail's)
tx_chunk = 64                 # bytes per write
tx_chunk_delay_ms = 10

[boot]
ready = "demo> "           # regex; default = re.escape(console.prompt); matched on lines and tail
settle = 2.5                  # seconds after ready before the first cmd
ready_timeout = 20.0          # booting longer than this → probe
probe = ""                    # line sent to provoke a prompt (empty = bare line ending)
probe_interval = 1.0
reset_patterns = []           # extra regexes with named groups reason (required), code, boot
sha_pattern = 'ELF file SHA256:\s*(?P<sha>[0-9a-fA-F]+)'
expected_reset_window = 10.0
ms_regression = 1000          # device-ms drop that implies a missed reset; 0 turns inference off

[[identity.field]]
name = "fw_version"           # fw_version is the field used to tag events
source = "cmd"                # "cmd" | "boot"
send = "version"
regex = 'Application Version:\s*(\S+)'
# filter = "openssl x509 -noout -subject"   # optional shell filter, stdin = capture
# timeout = 5.0

[commands.version]            # hail's command map, unchanged keys
description = "Firmware version"
send = "version"              # {name} placeholders filled from --arg / step args; unused or missing args are errors
capture = true                # read to the prompt; exclusive with expect
# expect = "PASSED"           # success: line starts with this
# error = ["FAIL", "ERROR"]   # failure: line contains any of these
# timeout = 30.0              # per-command override (addition)
# reboots = false             # the command is expected to reset the unit (addition)

[[deny]]
pattern = '^sip disable\b'    # regex on the rendered line
reason = "crashes from the console task"

[[redact]]
name = "sip_pass"
pattern = '(sip_pass["\s:=]+)[^",\s]+'   # ≤1 capture group (kept); rest of the match → ***

[flash]
baud = 921600                 # default 460800
before = "default-reset"      # esptool connect mode
after = "hard-reset"          # default; "no-reset" allowed
# flash_mode / flash_size / flash_freq override flasher_args.json when set

[env_gate]                    # optional; Profile.env_gate is None without it
timeout = 90.0                # default 60.0
require = ['got ip', 'MQTT connected']            # regexes that must appear since the cursor
forbid = ['PARTITION MISMATCH']                   # regexes that must not appear since the cursor
```

Validation rules: unknown keys are errors (`deny_unknown_fields` semantics, as hail);
`capture` and `expect` are exclusive; `capture` needs `console.prompt`; `expect` non-empty;
every regex compiles and no list entry (`error`, `require`, `forbid`, `reset_patterns`) is
empty; `redact` patterns compile and have ≤1 group; `redact.name`, `deny.reason` and
identity `source` are required; an identity `regex` has ≥1 group, `source = "cmd"` needs
`send`, `source = "boot"` takes neither `send` nor `filter`; `reset_patterns` define
`reason`; durations are seconds as numbers in the profile (scenarios use duration strings).
Errors are reported as `<file>: <toml path>: <message>`, array entries numbered from 1
(`deny[2].pattern`); every problem in a file is reported in one pass.

Lookup order for `<name>`: `--profile` (path to a dir or `profile.toml`; a bare word that
is a directory under the cwd counts as a path) → `.benchctl/profile.toml` walked up from the
cwd, used when no name was asked for or its `[profile].name` equals the name → `<config>/
profiles/<name>/profile.toml` (the file's `name` need not equal the directory's).

### 7.4 `units.toml` **[portable]**

```toml
[units.u1]
profile = "demo"                                  # default profile for this unit

[units.u1.console]                                   # v1: one console port per unit
match = { vid = "0403", pid = "6010", location = "2-1", device = "*usbserial-*1" }
# keys: serial, location, vid, pid, product, device (fnmatch glob on the path).
# All given keys must match exactly one present port; otherwise `device`/`port` errors (exit 4)
# naming the candidates. The bench FTDI (Dual RS232-HS) reports no serial number and the same
# location for both channels, so `device` is the only discriminator there; native-USB chips
# usually carry a serial.

[units.u1.expect]                                    # optional identity verification at attach
mac = "AA:BB:CC:DD:EE:FF"                            # any identity field name → expected value
# id_field = "mac"                                   # which field becomes events' `dev`; default mac, then cert_cn
```

Port enumeration: `registry.list_ports()` is the only place that calls
`serial.tools.list_ports.comports()`; `units`, attach, reconnect, and direct mode all take
their candidates from it. With `BENCHCTL_PORTS_FILE` set it instead reads that file on every
call (no caching: a reconnect re-resolves after the path changed). The file is a JSON list of
objects with the keys `device, vid, pid, serial_number, location, manufacturer, product,
description` (`vid`/`pid` ints or null, the rest strings or null); each becomes a
`ListPortInfo(device, skip_link_detection=True)` with those attributes set and `hwid =
usb_info()`. ptys never appear in `comports()`, so this is how a fake device is found; the
match `vid`/`pid` are strings of up to four hex digits (`0x` prefix allowed), compared
against `f"{vid:04x}"` case-insensitively; a TOML integer is rejected, because `6010` and
`0x6010` are indistinguishable once parsed.

### 7.5 `events.jsonl` **[portable]**

One JSON object per line. Common fields on every record:

| field | type | meaning |
|---|---|---|
| `seq` | int | monotonic within the file. The daemon numbers its session file; the runner renumbers every record it writes to the run dir and keeps the daemon's number in `daemon_seq` (absent on runner-originated records) |
| `ts` | str | host time, ISO 8601 with offset, ms precision |
| `t` | float | seconds since attach |
| `unit` | str | |
| `fw` | str\|null | identity `fw_version` once known |
| `dev` | str\|null | identity `id_field` value once known |
| `type` | str | one of the types below |
| `offset` | int\|null | log offset the record refers to |

Who writes what: the session emits `attach`, `gate`, `reset`, `elf_sha`, `tx`, `mark`,
`port_lost`, `port_back` and `identity`; the daemon emits `reply`, `wait`, `lease`,
`detach` and `flash`/`flash_log` (through U6); the runner the rest.

Per-type fields:

| type | fields |
|---|---|
| `attach` | `port, baud, pid, owner, session_dir` |
| `detach` | `reason` |
| `identity` | `fields: {…}, verified, mismatches: {field: [expected, got]}` |
| `reset` | `code, reason, boot_mode, elf_sha, expected, cause, context: [str], inferred: bool` |
| `elf_sha` | `sha, boot_offset` (when the SHA line follows the reset) |
| `gate` | `state, method: pattern\|probe\|flash\|null` |
| `tx` | `line, source: cmd\|monitor\|probe\|identity\|runner, request_id: null` (the session writes it and knows no request; the daemon's `reply` carries `request_id` with `offset` = the tx offset) |
| `reply` | `request_id, status, line, groups, captured, duration` |
| `mark` | `name` |
| `wait` | `request_id, pattern, since, result, line, groups, duration, order` |
| `flash` | `files: [FlashFile], app: AppDesc\|null, baud, after, duration, status: ok\|error, error: str\|null`; `offset` = the `## flash begin` line |
| `flash_log` | `line` (esptool stdout, one record per non-blank line, `\r` progress updates included; not redacted) |
| `port_lost` | `error` |
| `port_back` | `path, gap` |
| `lease` | `action: acquire\|take\|release\|stale, owner, prev_owner` |
| `run` | `phase: start\|end, scenario, run_dir, result, budget: {declared, computed}` |
| `step` | `index, name, kind, result: pass\|fail\|skip\|error, duration, detail` |
| `human` | `prompt, result: confirmed\|timeout, line` |
| `external` | `phase: run\|undo, cmd, exit, duration, stdout_tail, stderr_tail` |

### 7.6 Run directory **[portable]**

`<state>/runs/<unit>/<ts>-<scenario-name-slug>/` (or `--run-dir`):

```
scenario.toml      # copy of the input
profile.toml       # copy of the resolved profile
serial.log         # slice of the session log from the run's first offset to its last, same line format
events.jsonl       # Section 7.5, runner-filtered to the run's time span plus runner records
results.md         # Section 7.7
summary.json       # RunResult.to_json: the `run` --json fields with ok/unit, minus the CLI's `error` object
```

Six files. `serial.log` is fetched once at the end through paged `log` calls, after the
undos and before the `run` end record, so that record's `result` reflects an interrupted
fetch (`fail`, with a note); `events.jsonl` holds daemon events only while the run is
subscribed, dropping any whose `offset` precedes the run's first offset. A dry run creates
no run dir.

### 7.7 `results.md`

```
## <scenario name> — <pass|fail> (<unit>, fw <fw_version>, <date>)

| # | Step | Kind | Result | t (s) | Evidence |
|---|------|------|--------|-------|----------|
| 1 | place a call | human | pass | 42.1 | `ENTERED new state: CALL_ACTIVE` @ 812 |
| 2 | reboot rpc | external | pass | 47.3 | exit 0 |
| 3 | mqtt_silent 600 | cmd | pass | 53.0 | `## tx mqtt_silent 600` @ 830 |
| 4 | held for the call | wait | pass | 61.2 | `… held for the call` @ 877; no `rst:` in 50 s |

Resets: 1 expected (step 2), 0 unexpected. Run dir: `<path>`.
```

Evidence is the matched line (redacted, truncated to 120 chars) and its offset: a `wait`
or `human` shows its matched line; a `cmd` its `## tx` line (none when the shell was not
ready and nothing was sent); a `reset` or `flash` its `rst:` line; an `external` its exit.
Step names come from `name`, else from the unrendered template, so `--var` values never
reach the report. `t (s)` is scenario start (after the preconditions) to the step's end;
skipped steps show `-`. A reset is attributed to the step whose start offset is the last
one at or before it, so one arriving during a later step's `at` sleep counts against the
step before it, as in the example. Notes (precondition failure, failed undos, an
incomplete `serial.log`) sit between the table and the `Resets:` line, one bullet each.

### 7.8 Scenario TOML **[portable]**

```toml
name = "restart held during a call"       # required
profile = "demo"                        # optional; overrides the unit's default
budget = "25m"                             # optional declared worst case

[preconditions]
env_gate = true                            # run profile [env_gate] since boot
fw_version = '^1\.7\.4'                    # regex on identity fw_version

[[step]]                                   # common keys on every step:
name = "place a call"                      #   name (optional), kind (required), at (optional, "+5s" from last trigger),
kind = "human"                             #   timeout (duration), unit (reserved, unused in v1), expect_reset (bool),
prompt = "Place a call to the unit. Hang up when told."   # continue_on_fail (bool, default false)
confirm = "ENTERED new state:.*CALL_ACTIVE"
timeout = "15m"
# trigger = true                           # default true for human and wait

[[step]]
kind = "external"
at = "+5s"
run = "./cloud.py rpc reboot"              # shell; {var} placeholders from --var
# undo = "…"                               # pushed on success; run in reverse at the end
# cwd = "."  env = { KEY = "v" }  timeout = "60s"

[[step]]
kind = "cmd"
at = "+11s"
send = "mqtt_silent 600"                   # or: command = "<map name>", args = { k = "v" }
# secret = true                            # the line carries a --var secret: sent like `cmd --secret` (Section 4.8)
# expect = "…"  error = ["…"]  capture = true  timeout = "8s"

[[step]]
kind = "wait"
pattern = "held for the call"
since = "prev"                             # prev | step | trigger | boot | attach | <mark>
absent = "rst:"
window = "50s"
timeout = "60s"
# order = ["A", "B"]

[[step]]
kind = "reset"
# bootloader = false

[[step]]
kind = "flash"
artifacts = "build/debug"                  # or files = [{ addr = "0x220000", file = "app.bin" }, …]
# only = ["app", "otadata"]  after = "no-reset"

[[step]]
kind = "mark"
name = "after_flash"
```

Durations: `^\d+(\.\d+)?(ms|s|m|h)?$`; a bare number is seconds. `at` is `+<duration>`; the
`+` is required. Validation: at least one `[[step]]` (an empty scenario would pass by doing
nothing); `kind` in the set; kind-specific required keys present
(`human`: `prompt`, `confirm`; `external`: `run`; `cmd`: `send` xor `command`, `secret` a bool, `args` only
with `command`, `capture = true` exclusive with `expect`; `wait`: one of
`pattern`/`absent`/`order`, `pattern` exclusive with `order`, `window` only with `absent`;
`flash`: `artifacts` xor `files`, `only` only with `artifacts`; `mark`: `name`, which may not be a cursor keyword
(`attach`, `boot`, `prev`, `step`, `trigger`) or an integer); `since` resolvable (mark names
must be defined by an earlier `mark` step or be one of the keywords); every
`expect`/`error`/`pattern`/`absent`/`order`/`confirm` entry is a regex that compiles;
unknown keys are errors. Errors are `<file>: step[n].<key>: <message>` with `n` from 1,
the number `results.md` shows.

Loader output (what the runner receives): durations as float seconds; `Step.index` from 1;
`Step.trigger` defaults true for `human`/`wait`; `Step.secret` defaults false; `Step.timeout` is `None` when absent and
the runner applies the per-kind default — `cmd`: the step's, else the map entry's, else
`console.timeout`; `wait`: 60 s; `human`: 10 m; `external`: 60 s; `reset`: 5 s; `flash`:
5 m; `mark`: 0 — the same numbers `compute_budget` sums, plus each `at`; a `wait` counts
`max(timeout, window)`. A `cmd` step's `expect`/`capture` pair replaces the map entry's as
a unit (a step `expect` must not inherit the map's `capture`), a non-empty step `error`
replaces the map's, `reboots` is the map's and has no step key: a `cmd` step that reboots
the unit sets `expect_reset = true` and `capture = false`, and a following `wait` reads the
boot (`Command.expect_regex`/`error_regexes` for the map's, Section 4.3). `wait.window` is
sent only when declared (`None` otherwise): with
`absent` alone the daemon watches for `timeout`; with a pattern, `absent` is watched until
the match, and a declared `window` extends the watch past the match (Section 7.1). Relative
`artifacts`/`files` paths and `external.cwd` resolve against the cwd of `benchctl run`,
not the scenario file (scenarios live under `<config>`; builds live in the checkout).
`{var}` placeholders in `send`, `run` and `undo` are filled from `--var`; before step 1 the
runner checks every placeholder has a var and every var is used by some step (usage, exit
2), then renders each template with only its own placeholders (`render` rejects unused
args). Only those three keys are templates, and `{` has no escape (a `${VAR}` or awk
`{print}` in `run` is a placeholder error): a brace-bearing shell line goes in a script
file, `env` values, `cwd`, `artifacts` and `files` are literal, and an `external` step's
secrets come from the environment `benchctl run` inherited. A value seen in the log
(a message id) cannot be captured into a later step in v1.

---

## 8. Units

Eight units with non-overlapping file ownership (Section 2.3) and one hardware-free
acceptance criterion each. Import dependencies set the order:

| Wave | Units | Depends on |
|---|---|---|
| 0 | U1 scaffold | — |
| 1 | U2 text, U3 config | U1 |
| 2 | U4 session (U2, U3), U5 daemon/CLI (U3; U6/U7 via the stub signatures, Section 6.2), U6 flash/capture (U2 `linebuf`, U3 `artifacts`, `types.Transport`/`Session`, esptool), U7 runner (U3) | wave 1 |
| 3 | U8 Device profile | U5 (`validate`), U3 |

Units within a wave run in parallel. A unit's gate must pass with only its wave's
predecessors merged.

**U1 scaffold** — `pyproject.toml`, `uv.lock`, `.python-version`, `scripts/gates.sh`, ruff and
mypy config, `src/benchctl/{__init__,__main__,types,errors,paths,durations}.py`,
`tests/{conftest,fakedev,test_scaffold}.py`, a `cli.py` stub that only prints the version
(so `uv run benchctl --version` works; U5 replaces it wholesale), the `flash.py`,
`capture.py` and `runner.py` signature stubs of Section 4.13, `README.md` skeleton.
Acceptance: `scripts/gates.sh` passes, and `test_scaffold.py` proves the fixture: a
`FakeDevice` on a pty, opened through pyserial with `exclusive=True`, replays its boot
script on `reset()`, drops a command sent inside `blind_s`, answers `version` with the
prompt in the tail, after `vanish()` the slave read raises `SerialException`, and the
`ports_file` lists the fake, is empty while it is vanished, and lists the new path after
`reappear()`.

**U2 text layer** — `linebuf.py`, `idf.py`, `redact.py` + tests.
Acceptance: hail's tests pass ported one-to-one — `expect_returns_success_and_skips_noise`,
`expect_errors_on_error_marker` and `empty_success_marker_never_matches` as U3
`Command.expect_regex`/`error_regexes` cases (Section 4.3),
`read_until_returns_prefix_and_consumes_marker`, `next_line_reads_sequentially`,
`utf8_multibyte_split_across_reads`, `strip_ansi_removes_csi`,
`strip_ansi_handles_non_letter_finals`, `take_line_handles_lf_crlf_and_bare_cr`,
`take_line_holds_back_lone_trailing_cr` — plus IDF line parsing, reset/sha detection,
ms-regression inference, and redaction (one group kept, no group whole match,
idempotence). hail's port-level cases (`expect_times_out_on_a_quiet_live_port`,
`expect_errors_on_disconnect`) land as U4 session tests; its config cases
(`config_parses_with_defaults`, `config_parses_capture_command`,
`validate_command_rejects_conflicts`, `render_fills_and_validates`) land in U3.

**U3 config layer** — `profile.py`, `registry.py`, `scenario.py`, `artifacts.py`,
`examples/profile.toml`, `examples/scenarios/*.toml` + tests.
Acceptance: the three example scenarios and `examples/profile.toml` validate; each
validation rule in 7.3/7.4/7.8 has a failing-input test with a path-qualified error;
hail's config/render cases pass (see U2); `render` rejects missing and unused args;
`artifacts.resolve` handles build dir, `flasher_args.json`, and `addr=file` input
(hyphen-normalised flags, `--only` filtering, sha256 per file) and parses `AppDesc` from a
synthetic image; the registry picks exactly one port from a synthetic `comports()` list and
reports ambiguity; `list_ports()` reads `BENCHCTL_PORTS_FILE` when set and the `registry`
fixture's unit resolves to `fakedev.path` through it; `Command.expect_regex` turns a map
literal into an anchored escaped regex (a metacharacter in `expect` does not match as a
pattern); a broken `.benchctl/profile.toml` of another name does not block a named lookup;
an integer `vid`/`pid` in `units.toml` is a path-qualified error. U3 may delete the
`benchctl.profile`/`benchctl.scenario` mypy override in `pyproject.toml` once those modules
exist.

**U4 device session** — `transport.py`, `logstore.py`, `gate.py`, `session.py` + tests.
Acceptance: against `fakedev` through `PySerialTransport`: attach resets once and stores
boot lines with offsets 0..n and meta line 0; the reset event is classified unexpected
with context, and expected after `reset()`; gate goes booting→settling→ready and `cmd`
blocks until ready; a `cmd` after boot returns its tx offset and a capture ending at the
prompt; `wait --since <mark>` matches and `--absent` passes/fails correctly; `vanish()`
then `reappear()` produces `port_lost`, `port_back` with the new path, and a working `cmd`
afterwards; redaction is applied before storage (`sip_pass=***` in `serial.log`, never the
secret).

**U5 daemon, client, CLI, docs** — `daemon.py`, `lease.py`, `client.py`, `cli.py`,
`monitor.py`, `defaults.py`, `SKILL.md`, `README.md` body, `tests/fake_daemon.py` + tests.
Acceptance: with `BENCHCTL_DAEMON_ARGV` pointing at `fake_daemon.py` (real `daemon.py` over a
`FakeSession`): auto-attach spawns and connects; two clients racing attach yield one
daemon; owner mismatch gives exit 3 with `busy: held by … since …` for mutating ops and 0
for read-only ops; `--take` transfers; every verb in 7.1 produces its documented `--json`
shape and exit code (including `validate` exit 2 and `wait` timeout exit 1); two state
dirs run two daemons concurrently; identity mismatch leaves the daemon unverified
(mutating ops → exit 4, `status` → 0, `attach --no-verify` clears it); `status`/`units`/
`validate` never spawn a daemon; `test_skill_doc.py` asserts `SKILL.md` has Claude Code
frontmatter (`name`, `description`) and names every CLI verb.

**U6 flash and capture** — `flash.py`, `capture.py` + tests.
Acceptance: with a fake `esptool.main` and a `FakeSession`/`FakeTransport`: the argv for a
build dir, a `flasher_args.json`, and `addr=file` pairs matches 4.10 step 7 exactly (hyphen
flags, `--before no-reset`, `--after no-reset-stub`), `connect(before)` is called before `main`,
the reader is paused during `main` and resumed after, transport settings are restored on
success and on `FatalError` (which surfaces as exit 4), `## flash begin` and `## flash end`
go through `session.note`, after the pause exits
`session.reset(False, wait_rst=5.0, cause="flash")` is called for `hard-reset` and
`session.set_gate("bootloader", method="flash")` for `no-reset`, and the `FlashResult`
carries per-file sha256, the `AppDesc` taken from the `app` entry, and the `ResetInfo` the
session returned; direct-mode `capture --reset` on `fakedev` returns the boot lines with `t`
since reset.

**U7 runner, events, report** — `runner.py`, `events.py`, `report.py` + tests.
Acceptance: against a `FakeClient`: `at` offsets are measured from the last trigger match;
`since = "prev"` resolves to the previous step's start offset; a `human` step passes on its
confirm line without input; `undo` commands run in reverse order after success, after a
failed step, on SIGINT during a step, and on SIGINT between steps; a pattern wait with
`absent` and no `window` returns on its match, not at its timeout; the run dir contains the
six files of 7.6 with `results.md` matching 7.7; the budget line reports declared vs
computed; a failed precondition writes `precondition_failed` and exit 1. The `FakeClient`
keeps U4's `wait` contract (device lines only; a declared `window` holds a matched wait to
`start + window`), so a test cannot pass on behaviour the daemon does not have.

**U8 Device profile (outside the repo)** — writes a real device's
`~/.config/benchctl/profiles/<name>/profile.toml` and its acceptance scenarios, plus
`~/.config/benchctl/units.toml` for the bench unit. It carries the device's deny-list
entries, redaction for credentials and tokens, identity fields (version, MAC, cert CN via
an `openssl` filter), and the env gate (network up, services connected).
Acceptance: `benchctl validate ~/.config/benchctl/profiles/<name>` exits 0 for the profile
and every scenario, and nothing device-specific (hostnames, credentials) lands in this repo.

---

## 9. Bench validation (hardware)

- Attach resets exactly once; `status` shows the POWERON reset and the identity; a second
  `cmd` does not reset. Exactly one `reset` event before the first `cmd`, `expected: true,
  cause: "attach"`; a second one (`expected: false`) means the open-reset's `rst:` line beat
  the pulse (Section 4.5) and attach must stop consuming the arm for lines stored before the
  pulse returned.
- `cmd version` after attach is sent only after the gate (no vanished command, no 1.2–2.0 s
  config-window reboot); confirm with `log --since attach`.
- `flash build/debug --only app,otadata` at 921600 completes, the after-reset is classified
  expected, and the boot's `ELF file SHA256` prefix matches the recorded `app_elf_sha256`.
  No other `reset` event appears between `## flash begin` and the armed one, and the
  `flash_log` event count for a ~2 MB write is tolerable.
- `flash --after no-reset` leaves `status` in `bootloader` with no probe TX, and `cmd` is
  refused until `reset`.
- Identity mismatch (`expect.mac` wrong on purpose) fails attach with exit 4.
- `serial.log` of a debug build contains `sip_pass=***` and never the password.
- Two units (both FTDI channels, or a second adapter) driven from two shells concurrently
  for 10 minutes without cross-talk in either log.
- Native-USB unit (C5/P4): `reset` makes the port vanish and reappear; `port_lost`/`port_back`
  events appear and `cmd` works afterwards.
- The device profile's acceptance scenarios run end to end and produce a `results.md`
  that drops into the PR body.
- Review follow-ups (Section 4.7, 4.8, 4.4): the daemon's RSS after the longest soak,
  against the ~250 B/line figure; what the device console echoes for a `cmd --secret`
  line (a redrawn or truncated echo defeats the whole-line mask → ticket); whether
  `deferred_log` replays trigger inferred resets during a normal session (if so the
  profile sets `ms_regression = 0`), and with inference off, that a replug mid-boot still
  refreshes `status.last_reset.elf_sha` (else ticket); the gate reaches `ready` on the real
  prompt without a `## probe` stream.
- Open question from the spec stays open: whether a different adapter opens without
  resetting; `attach` must still behave (gate probe path) when it does not.

---

## 10. Decisions carried from the spec

- Attach-once replaces "open without reset" (unreachable on the bench FTDI).
- No MCP server, no backend client, no multi-unit scenarios in v1 (`unit` key reserved).
- `monitor` is line-oriented in v1.
- Memory holds the whole session log; paging to disk is a later change behind `LogStore`.
- Items 13–16 and 18 of the requirement map remain undecided and have no hooks here.
