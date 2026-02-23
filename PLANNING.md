# Nexus Implementation Plan

## Architecture Overview

Nexus is a stateless Python CLI that orchestrates tmux sessions across SSH-connected machines. The core insight: tmux is the database, SSH is the transport, Python is just the query/command layer.

### File Layout

```
nx/
  pyproject.toml              # uv-managed, entry point: nx
  src/
    nx/
      __init__.py             # Version
      cli.py                  # Typer CLI entry point + dependency checks
      config.py               # FleetConfig pydantic model, TOML loading
      ssh.py                  # SSH command execution, async fan-out
      tmux.py                 # tmux command builder & output parser
      resolve.py              # Session resolution protocol (0/1/N matches)
      dashboard.py            # nx dash: temporary tmux CCTV layout
      snapshot.py             # Snapshot save/restore (pydantic models)
      nodes.py                # Node management (add/rm/ls)
      data/
        tmux.conf             # Canonical tmux.conf pushed to remote nodes
  tests/
    conftest.py               # Shared fixtures: mock SSH, mock tmux, temp config
    test_config.py
    test_transport.py         # SSH + tmux (combined milestone)
    test_resolve.py
    test_cli_list.py
    test_cli_new.py
    test_cli_attach.py
    test_cli_peek_logs.py
    test_cli_send_kill.py
    test_cli_gc.py
    test_dashboard.py
    test_nodes.py
    test_snapshot.py
    integration/
      conftest.py             # Integration fixtures (real tmux on nx_test socket)
      test_local_workflow.py  # Full lifecycle against real local tmux
```

### Module Dependency Flow

```
cli.py → config.py → (pydantic, tomllib)
cli.py → resolve.py → ssh.py → (asyncio.subprocess)
cli.py → tmux.py → (string formatting)
cli.py → nodes.py → ssh.py
cli.py → snapshot.py → config.py
cli.py → dashboard.py → tmux.py, resolve.py
```

### Load-Bearing Abstractions

1. **`ssh.run_on_node(node, cmd)`** — The transport. Every feature depends on this executing commands locally or remotely and returning stdout/stderr/returncode.
2. **`tmux.build_cmd()` / `tmux.parse_list_output()`** — The command builder and parser. Translates between Python data structures and tmux CLI invocations.
3. **`resolve.resolve_session(name, config)`** — The resolution protocol. Every session-targeting command passes through this to go from a bare name to a `(node, session)` tuple.
4. **`ssh.fan_out(nodes, cmd)`** — Async parallel execution with semaphore. Powers `list`, `resolve`, `gc`, and `snapshot`.

### The tmux Format Contract

The format string is the contract between tmux output and every feature that reads session state. All parsing depends on this exact format:

```python
FIELD_SEPARATOR = "|"
FORMAT_STRING = (
    "#{session_name}"
    "|#{session_windows}"
    "|#{session_attached}"
    "|#{pane_current_path}"
    "|#{pane_current_command}"
    "|#{pane_pid}"
    "|#{pane_dead}"
    "|#{pane_dead_status}"
)
```

**Fields in order:** session_name, session_windows, session_attached, pane_current_path, pane_current_command, pane_pid, pane_dead (0/1), pane_dead_status (exit code or empty).

**Delimiter:** Pipe (`|`). Chosen because it cannot appear in session names (tmux rejects them) and is unlikely in paths or command names.

Every parser test uses this exact format. If the format changes, all parser tests must update — this is intentional, it's a breaking contract change.

### Canonical tmux.conf

The file `src/nx/data/tmux.conf` is the canonical tmux config pushed to remote nodes by `nx nodes add`. It is bundled as package data via `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/nx"]

[tool.hatch.build.targets.wheel.shared-data]
"src/nx/data" = "nx/data"
```

At runtime, located via `importlib.resources`:

```python
from importlib.resources import files
tmux_conf_path = files("nx.data").joinpath("tmux.conf")
```

### Duplicate Session Handling

When `nx new` creates a session whose name already exists on the target node, Nexus does **not** pre-check for duplicates. It lets tmux reject the command (`tmux new-session` returns exit code 1 with `"duplicate session: <name>"`). Nexus surfaces this as a user-facing error: `Error: Session '<name>' already exists on <node>.` This is simpler and race-free compared to a check-then-create approach.

---

## Harness Design

### Philosophy

All unit tests use **monkeypatched subprocess calls** — no real SSH, no real tmux. Integration tests use **real local tmux** on a dedicated socket (`tmux -L nx_test`) — no SSH required.

### Mock Strategy

**`mock_ssh` fixture:** Replaces `asyncio.create_subprocess_exec` with a configurable fake that returns predetermined stdout/stderr/returncode based on the command pattern. Tests register expected responses:

```python
mock_ssh.register("tmux -L nexus list-sessions ...", stdout="api: 1 windows\n")
mock_ssh.register("ssh dev-server ...", stdout="data: 1 windows\n")
```

**`tmp_config` fixture:** Creates a temporary `fleet.toml` with test-specific node lists and settings. Returns a `FleetConfig` object.

**`mock_fzf` fixture:** Replaces `subprocess.run(["fzf", ...])` with a fake that returns a preconfigured selection. Tests both the interactive and non-interactive code paths. For the unit→integration gap, see the `test_fzf_roundtrip` integration test (Milestone 12) which uses `fzf --filter` non-interactively to verify the format/parse contract.

**`mock_execvp` fixture:** Replaces `os.execvp` with a recorder that captures the command and args. Used by attach and dashboard tests to verify the correct process replacement command without actually replacing the process.

### Running Tests

```bash
# All unit tests (no external deps, runs in <5s)
uv run pytest tests/ -x -v --ignore=tests/integration

# Integration tests (requires tmux installed locally)
uv run pytest tests/integration/ -x -v

# Full suite
uv run pytest tests/ -x -v
```

### Adding a Test

1. Create a test function in the appropriate `test_*.py` file
2. Use the fixtures from `conftest.py` (`mock_ssh`, `tmp_config`, etc.)
3. Assert on specific outputs, return codes, or side effects
4. Run `uv run pytest tests/test_your_file.py -x -v` to verify

### What "Passing" Looks Like

```
tests/test_config.py::test_load_valid_config PASSED
tests/test_config.py::test_expand_env_vars PASSED
tests/test_config.py::test_missing_config_uses_defaults PASSED
...
====== N passed in X.XXs ======
```

---

## Milestone Sequence

### Dependency Map & Sequencing Rationale

```
M0 (skeleton) → M1 (config) → M2 (ssh+tmux) → M3 (list) → M4 (new)
                                                                 ↓
                                          M5 (resolve) → M6 (attach) → M7 (peek/logs)
                                                              ↓             ↓
                                                         M10 (dash)    M8 (send/kill)
                                                                            ↓
                                                                       M9 (gc)
                                                                            ↓
                                                                      M11 (nodes)
                                                                            ↓
                                                                     M12 (snapshot)
                                                                            ↓
                                                                   M13 (integration)
```

**Key sequencing decisions:**

- **M3 (`list`) and M4 (`new`) deliberately skip the resolution protocol.** `list` queries all nodes (no session targeting). `new` takes an explicit `--on <node>` flag and session name — no ambiguity to resolve. These are the only two commands that go directly from transport+tmux to user-facing output, bypassing resolution entirely.
- **M5 (resolve) must land before any session-targeting command.** Starting from M6, every command (attach, peek, logs, send, kill, gc) flows through `resolve_session()`.
- **M6 (attach) is front-loaded immediately after resolution** because it's the single most complex feature: three $TMUX scenarios, two process strategies (`execvp` vs `subprocess.run`), environment variable detection, socket-aware window targeting. Early placement maximizes iteration room for surprises in `$TMUX` parsing or `execvp` behavior.
- **M10 (dashboard) is pulled forward** because its Enter-key state machine (capture metadata → teardown → context shift → re-evaluate `$TMUX` → attach) has the most moving parts. It depends on attach (M6) working correctly, so it goes right after gc (M9).

---

### Milestone 0: Project Skeleton & Test Harness

> **Goal:** `uv run nx --help` works. `uv run pytest` runs and shows 0 failures (with 0 tests).

**Harness (B):**
- Create `tests/conftest.py` with placeholder fixtures (`mock_ssh`, `tmp_config`, `mock_fzf`, `mock_execvp`)
- Create `tests/__init__.py`
- Verify `uv run pytest tests/` runs cleanly

**Product (A):**
- Create `pyproject.toml` with:
  - `[project]` metadata (name=nx, python>=3.12)
  - `[project.scripts]` nx = "nx.cli:app"
  - `[build-system]` with hatchling
  - `[tool.pytest.ini_options]` testpaths = ["tests"]
  - Dependencies: typer, pydantic, rich
  - Dev dependencies: pytest, pytest-asyncio
  - Package data: `src/nx/data/tmux.conf`
- Create `src/nx/__init__.py` with `__version__`
- Create `src/nx/cli.py` with minimal Typer app (just `--help`)
- Create `src/nx/data/tmux.conf` (minimal canonical config)

**Verification:**
```bash
uv run nx --help        # Shows help text
uv run pytest tests/    # 0 tests, 0 failures
```

**Demo:**
```
$ uv run nx --help
Usage: nx [OPTIONS] COMMAND [ARGS]...
```

---

### Milestone 1: Config Loading & Validation

> **Goal:** `FleetConfig` loads from TOML, validates, expands env vars. All config tests pass.

**Harness (B):**
- `tests/test_config.py`:
  - `test_load_valid_config` — Parse a valid TOML, assert all fields match
  - `test_expand_env_vars` — `$SHELL` resolves to actual shell path
  - `test_missing_config_uses_defaults` — No file → sensible defaults
  - `test_invalid_config_raises` — Bad types → ValidationError
  - `test_local_always_in_nodes` — `"local"` is present even if not listed
- `tmp_config` fixture: creates a temp dir with `fleet.toml`, yields `FleetConfig`

**Product (A):**
- `src/nx/config.py`:
  - `FleetConfig(BaseModel)` with fields: nodes, default_node, default_cmd, max_concurrent_ssh, auto_reap_clean_exit
  - `field_validator` for env var expansion
  - `load_config(path=None) -> FleetConfig` — loads from `~/.config/nexus/fleet.toml` or returns defaults
- Wire into `cli.py` as a Typer callback that loads config into `typer.Context`

**Verification:**
```bash
uv run pytest tests/test_config.py -x -v   # 5/5 passing
```

**Demo:**
```python
>>> from nx.config import load_config
>>> c = load_config()
>>> c.default_node
'local'
>>> c.default_cmd
'/usr/bin/fish'  # expanded from $SHELL
```

---

### Milestone 2: SSH Transport + tmux Command Builder (merged)

> **Goal:** Can execute commands on local/remote nodes via SSH, fan-out across N nodes, build all tmux commands, and parse tmux output into structured data. These two layers are co-tested because neither is useful alone and both are low-risk, well-understood abstractions.

**Harness (B):**

`tests/test_transport.py` (SSH):
- `test_run_local_command` — `run_on_node("local", ["echo", "hi"])` returns stdout="hi\n"
- `test_run_remote_command` — `run_on_node("dev-server", ["echo", "hi"])` builds correct SSH command with `-o ConnectTimeout=2`
- `test_fan_out_parallel` — `fan_out(["local","dev"], cmd)` returns dict of {node: result}
- `test_fan_out_timeout` — node that exceeds ConnectTimeout returns error result, doesn't block others
- `test_fan_out_semaphore_created` — verify `asyncio.Semaphore` is instantiated with the configured `max_concurrent_ssh` value and that all tasks are dispatched through it. **Do not assert on timing or concurrency ordering** — asyncio's semaphore implementation is trusted. Test that we wire it correctly, not that it works.

`tests/test_transport.py` (tmux):
- `test_build_list_cmd` — returns correct `tmux -L nexus list-sessions -F "<FORMAT_STRING>"` command
- `test_parse_list_output` — parses multi-line pipe-delimited tmux output into `list[SessionInfo]`
- `test_parse_empty_output` — no sessions → empty list (not error)
- `test_parse_dead_pane` — `pane_dead=1` + `pane_dead_status=1` → `SessionInfo.exit_status == 1`
- `test_build_new_cmd` — correct `tmux -L nexus new-session -d -s <name> -c <dir> <cmd>`
- `test_build_capture_cmd` — correct `tmux -L nexus capture-pane -p -t <session> -S -30`
- `test_build_send_keys_cmd` — with and without `--raw`; default mode appends `Enter`
- `test_build_kill_cmd` — correct `tmux -L nexus kill-session -t <session>`

**Product (A):**

`src/nx/ssh.py`:
- `@dataclass NodeResult: stdout, stderr, returncode, node`
- `async run_on_node(node: str, cmd: list[str], timeout: int = 2) -> NodeResult`
  - If node == "local": run cmd directly via `asyncio.create_subprocess_exec`
  - Else: run `ssh -o ConnectTimeout={timeout} {node} {shlex.join(cmd)}`
- `async fan_out(nodes: list[str], cmd: list[str], max_concurrent: int = 16) -> dict[str, NodeResult]`
  - `asyncio.Semaphore(max_concurrent)` + `asyncio.gather()`

`src/nx/tmux.py`:
- `FIELD_SEPARATOR = "|"`
- `FORMAT_STRING` — the pinned format string from Architecture Overview
- `@dataclass SessionInfo: name, windows, attached, pane_path, pane_cmd, pane_pid, is_dead, exit_status`
- `build_list_cmd() -> list[str]`
- `parse_list_output(raw: str) -> list[SessionInfo]`
- `build_new_cmd(name: str, cmd: str | None, directory: str | None) -> list[str]`
- `build_capture_cmd(session: str, lines: int | str) -> list[str]`
- `build_send_keys_cmd(session: str, keys: list[str], raw: bool) -> list[str]`
- `build_kill_cmd(session: str) -> list[str]`

**Verification:**
```bash
uv run pytest tests/test_transport.py -x -v   # 13/13 passing
```

**Demo:**
```python
>>> from nx.tmux import build_new_cmd, parse_list_output, FORMAT_STRING
>>> build_new_cmd("api", "python serve.py", "/app")
['tmux', '-L', 'nexus', 'new-session', '-d', '-s', 'api', '-c', '/app', 'python', 'serve.py']
>>> parse_list_output("api|1|0|/home/u/app|python|1234|0|\n")
[SessionInfo(name='api', windows=1, attached=0, pane_path='/home/u/app', pane_cmd='python', pane_pid=1234, is_dead=False, exit_status=None)]
```

---

### Milestone 3: `nx list` — First End-to-End Command

> **Goal:** `nx list` queries all nodes in parallel, formats output as a rich table grouped by node, flags unreachable nodes. **This command does not use the resolution protocol** — it queries all nodes unconditionally and displays everything.

**Harness (B):**
- `tests/test_cli_list.py`:
  - `test_list_empty_fleet` — no sessions on any node → "No active sessions"
  - `test_list_single_node` — one node with 2 sessions → table with 2 rows
  - `test_list_multi_node` — 3 nodes, mixed sessions → grouped table
  - `test_list_unreachable_node` — one node times out → `[UNREACHABLE]` in output
  - `test_list_shows_status` — running vs exited sessions display `[RUNNING]` / `[EXITED 0]` / `[EXITED 1]`
- Wire `mock_ssh` to return appropriate pipe-delimited tmux output per node

**Product (A):**
- Add `list` command to `cli.py` (alias `l`)
- Implement: `fan_out` all nodes with `build_list_cmd()`, parse results with `parse_list_output()`, render `rich.Table`
- Group by node, show columns: Session, Working Dir, Command, Status
- Handle unreachable nodes gracefully (show `[UNREACHABLE]` row)

**Verification:**
```bash
uv run pytest tests/test_cli_list.py -x -v   # 5/5 passing
```

**Demo:**
```
$ uv run nx list
┌─────────────┬──────────┬─────────────┬───────────┬───────────┐
│ Node        │ Session  │ Directory   │ Command   │ Status    │
├─────────────┼──────────┼─────────────┼───────────┼───────────┤
│ local       │ api      │ /home/u/app │ python    │ [RUNNING] │
│ local       │ worker   │ /home/u/app │ celery    │ [RUNNING] │
├─────────────┼──────────┼─────────────┼───────────┼───────────┤
│ dev-server  │ api      │ /app        │ node      │ [EXITED 0]│
├─────────────┼──────────┼─────────────┼───────────┼───────────┤
│ gpu-rig     │          │             │           │[UNREACHABLE]│
└─────────────┴──────────┴─────────────┴───────────┴───────────┘
```

---

### Milestone 4: `nx new` — Session Creation

> **Goal:** `nx new [--on <node>] [--dir <path>] <name> [cmd]` spawns a tmux session on the target node. **This command does not use the resolution protocol** — it takes an explicit node via `--on` (defaulting to `default_node`) and a user-provided session name. No ambiguity to resolve.

**Harness (B):**
- `tests/test_cli_new.py`:
  - `test_new_local_default` — no `--on` → uses default_node, session created locally
  - `test_new_remote` — `--on dev-server` → SSH command sent to dev-server
  - `test_new_with_dir` — `--dir /tmp` → `-c /tmp` in tmux command
  - `test_new_default_dir_local` — local without `--dir` → `$PWD`
  - `test_new_default_dir_remote` — remote without `--dir` → `$HOME`
  - `test_new_default_cmd` — no cmd arg → uses config.default_cmd
  - `test_new_duplicate_name` — tmux returns exit 1 with "duplicate session" → Nexus surfaces `Error: Session 'api' already exists on local.` (tmux rejects, Nexus wraps the error — no proactive check)

**Product (A):**
- Add `new` command to `cli.py`
- Build tmux new-session command via `tmux.build_new_cmd()`
- Execute via `ssh.run_on_node()`
- Handle default_node, default_cmd, default directory logic
- Surface tmux errors (including duplicate session) as clean user-facing messages

**Verification:**
```bash
uv run pytest tests/test_cli_new.py -x -v   # 7/7 passing
```

**Demo:**
```
$ uv run nx new --on local api "python serve.py"
Created session local/api

$ uv run nx new --on local api "python serve.py"
Error: Session 'api' already exists on local.
```

---

### Milestone 5: Resolution Protocol

> **Goal:** Bare session names resolve to `(node, session)` via parallel query. Handles 0/1/N matches with fzf disambiguation. This is the gateway to all session-targeting commands (M6 onward).

**Harness (B):**
- `tests/test_resolve.py`:
  - `test_resolve_fully_qualified` — `"local/api"` bypasses fan-out, returns directly
  - `test_resolve_unique_match` — `"api"` exists on exactly one node → returns it
  - `test_resolve_no_match` — `"nonexistent"` → raises `SessionNotFound`
  - `test_resolve_ambiguous_interactive` — `"api"` on 2 nodes + isatty → calls fzf with correctly formatted input (one `node/session` per line)
  - `test_resolve_ambiguous_piped` — `"api"` on 2 nodes + !isatty → raises `AmbiguousSession` with message listing all matches
  - `test_resolve_fzf_default_node_first` — default_node match sorted to top of fzf input
  - `test_resolve_fzf_output_parsed` — fzf returns `"dev-server/api\n"` → resolve returns `("dev-server", "api")`
- `mock_fzf` fixture: intercepts subprocess.run for fzf calls, validates stdin format, returns configured selection

**Product (A):**
- `src/nx/resolve.py`:
  - `class SessionNotFound(Exception)`
  - `class AmbiguousSession(Exception)`
  - `async resolve_session(name: str, config: FleetConfig) -> tuple[str, str]`
    - If `/` in name: split and return
    - Fan out `tmux list-sessions` to all nodes
    - Filter matches by session name
    - 0 → raise SessionNotFound
    - 1 → return match
    - N → check `sys.stdin.isatty()`
      - Interactive: pipe matches to `fzf` (default_node match first), parse selection
      - Non-interactive: raise AmbiguousSession
- Add `fzf` check to CLI init callback (`shutil.which("fzf")`) — abort with clear error if missing

**Verification:**
```bash
uv run pytest tests/test_resolve.py -x -v   # 7/7 passing
```

**Demo:**
```
# Unique match — instant
$ uv run nx peek api
[output from local/api]

# Ambiguous — fzf disambiguation
$ uv run nx peek api
> local/api
  dev-server/api

# Ambiguous — piped context
$ echo "api" | uv run nx peek api
Ambiguous session. Matches: local/api, dev-server/api. Use fully qualified name.
```

---

### Milestone 6: `nx attach` — Anti-Nesting Logic (front-loaded for risk)

> **Goal:** Attach correctly handles all three nesting scenarios. **This is the highest-risk feature** — three $TMUX scenarios, two process strategies (`execvp` vs `subprocess.run`), environment variable detection, socket-aware window targeting. Front-loaded to maximize iteration room.

**Harness (B):**
- `tests/test_cli_attach.py`:
  - `test_attach_bare_terminal` — no `$TMUX` → `os.execvp("ssh", ["ssh", "-t", node, "tmux", "-L", "nexus", "attach", "-t", session])`
  - `test_attach_bare_terminal_local` — no `$TMUX` + local target → `os.execvp("tmux", ["tmux", "-L", "nexus", "attach", "-t", session])`
  - `test_attach_from_nexus_local` — `$TMUX` contains `/tmp/tmux-*/nexus` + local target → `subprocess.run(["tmux", "-L", "nexus", "switch-client", "-t", session])`
  - `test_attach_from_nexus_remote` — `$TMUX` contains nexus socket + remote target → `subprocess.run(["tmux", "-L", "nexus", "new-window", "-n", session, "ssh -t node 'tmux -L nexus attach -t session'"])`
  - `test_attach_from_user_tmux` — `$TMUX` set (non-nexus socket) → `subprocess.run(["tmux", "new-window", "-n", session, "ssh -t node 'tmux -L nexus attach -t session'"])`
  - `test_attach_uses_resolution` — bare name triggers resolution protocol
- `mock_execvp` fixture: records `(executable, args)` instead of replacing the process
- Monkeypatch `os.environ` for each `$TMUX` scenario

**Product (A):**
- Add `attach` command to `cli.py`: resolve session → detect `$TMUX` → execute appropriate strategy per Section 6.1 of spec
- **Scenario A (bare terminal):**
  - Local: `os.execvp("tmux", ["tmux", "-L", "nexus", "attach", "-t", session])`
  - Remote: `os.execvp("ssh", ["ssh", "-t", node, "tmux", "-L", "nexus", "attach", "-t", session])`
- **Scenario B (inside `tmux -L nexus`):** detect via `"nexus"` in `$TMUX` socket path
  - Local: `subprocess.run(["tmux", "-L", "nexus", "switch-client", "-t", session])` + `sys.exit(0)`
  - Remote: `subprocess.run(["tmux", "-L", "nexus", "new-window", "-n", session, ...])` + `sys.exit(0)`
- **Scenario C (inside user's personal tmux):** `$TMUX` set but not nexus socket
  - `subprocess.run(["tmux", "new-window", "-n", session, ...])` + `sys.exit(0)`

**Verification:**
```bash
uv run pytest tests/test_cli_attach.py -x -v   # 6/6 passing
```

**Demo:**
```
$ uv run nx attach api              # from bare terminal → SSH replaces process
[now inside remote tmux session]
```

---

### Milestone 7: `nx peek` & `nx logs` — Observability

> **Goal:** Peek shows last 30 lines. Logs is context-aware: 100 lines interactive, full scrollback when piped. These are the first commands exercising the full **resolve → build tmux cmd → execute on node → display output** pipeline.

**Harness (B):**
- `tests/test_cli_peek_logs.py`:
  - `test_peek_output` — returns last 30 lines of capture-pane
  - `test_peek_uses_resolution` — bare name triggers resolution
  - `test_logs_interactive_default` — isatty → 100 lines
  - `test_logs_piped_default` — !isatty → full scrollback (`-S -`)
  - `test_logs_custom_lines` — `--lines 50` overrides default
  - `test_peek_nonexistent_session` — "Session not found" error

**Product (A):**
- Add `peek` command to `cli.py`: resolve session → `build_capture_cmd(session, 30)` → `run_on_node` → print stdout
- Add `logs` command to `cli.py`: resolve session → detect isatty → `build_capture_cmd(session, lines)` → `run_on_node` → print stdout
- `--lines` option with context-aware default

**Verification:**
```bash
uv run pytest tests/test_cli_peek_logs.py -x -v   # 6/6 passing
```

**Demo:**
```
$ uv run nx peek api
[last 30 lines of terminal output]

$ uv run nx logs api > /tmp/full.txt    # piped → full scrollback
$ wc -l /tmp/full.txt
98234 /tmp/full.txt
```

---

### Milestone 8: `nx send` & `nx kill` — Interaction

> **Goal:** Send injects keystrokes (auto-Enter by default, `--raw` for literal). Kill terminates sessions.

**Harness (B):**
- `tests/test_cli_send_kill.py`:
  - `test_send_auto_enter` — `nx send api "hello"` → `send-keys "hello" Enter`
  - `test_send_raw_mode` — `nx send --raw api C-c` → `send-keys C-c` (no Enter)
  - `test_send_multiple_args` — `nx send api "cd /app" "npm start"` → sends both, Enter after last
  - `test_send_uses_resolution` — bare name triggers resolution
  - `test_kill_session` — `nx kill api` → `tmux kill-session -t api` on correct node
  - `test_kill_nonexistent` — error message

**Product (A):**
- Add `send` command: resolve → `build_send_keys_cmd` → `run_on_node`
  - Default: append `Enter` to final argument
  - `--raw`: pass args verbatim to `tmux send-keys`
- Add `kill` command: resolve → `build_kill_cmd` → `run_on_node`

**Verification:**
```bash
uv run pytest tests/test_cli_send_kill.py -x -v   # 6/6 passing
```

**Demo:**
```
$ uv run nx send api "npm start"     # types "npm start" + Enter
Sent to local/api

$ uv run nx kill api
Killed session local/api
```

---

### Milestone 9: `nx gc` — Garbage Collection

> **Goal:** Reap exited panes fleet-wide or by name. Interactive confirmation required unless piped.

**Harness (B):**
- `tests/test_cli_gc.py`:
  - `test_gc_fleet_wide` — kills all EXITED sessions across nodes
  - `test_gc_by_name` — kills only the named session if exited
  - `test_gc_running_session_skipped` — running sessions untouched
  - `test_gc_dry_run` — `--dry-run` lists but doesn't kill
  - `test_gc_interactive_confirmation` — prompts before killing (mock typer.confirm)
  - `test_gc_piped_no_confirmation` — piped → no prompt, proceeds automatically

**Product (A):**
- Add `gc` command: fan_out list → filter exited (is_dead=True) → confirm → kill each
- `--dry-run` flag: list without action
- `typer.confirm()` when interactive, auto-proceed when piped

**Verification:**
```bash
uv run pytest tests/test_cli_gc.py -x -v   # 6/6 passing
```

**Demo:**
```
$ uv run nx gc --dry-run
Would reap: local/old-api [EXITED 0], dev-server/crashed [EXITED 1]

$ uv run nx gc
Reap 2 exited sessions? [y/N]: y
Reaped local/old-api
Reaped dev-server/crashed
```

---

### Milestone 10: Dashboard (`nx dash`) — pulled forward for risk

> **Goal:** CCTV-style read-only dashboard of all sessions. Enter key tears down dash and attaches to selected session. **The Enter-key state machine is the second most complex feature** (after attach) — it depends on attach working correctly, which is why it follows M6.

**Harness (B):**
- `tests/test_dashboard.py`:
  - `test_dash_creates_temporary_session` — creates `tmux -L nx_dash` session
  - `test_dash_pane_metadata` — each pane tagged with `@nx_target "<node>/<session>"`
  - `test_dash_stores_nx_bin` — `NX_BIN` env var set in nx_dash session via `set-environment`
  - `test_dash_enter_binding` — Enter key bound to shim that: captures target, kills dash, execs `nx attach`
  - `test_dash_read_only` — panes attached with `-r` flag
  - `test_dash_empty_fleet` — no sessions → "No active sessions to display"
  - `test_dash_uses_execvp` — final attach to nx_dash session uses `os.execvp`

**Product (A):**
- `src/nx/dashboard.py`:
  - `async build_dashboard(config)`:
    1. Fan-out list all sessions
    2. Create `tmux -L nx_dash new-session -d -s dashboard`
    3. For each session: split-window with `ssh -t <node> "tmux -L nexus attach -t <session> -r"`
    4. Tag each pane: `tmux -L nx_dash set-option -p @nx_target "<node>/<session>"`
    5. Store NX_BIN: `tmux -L nx_dash set-environment NX_BIN "<resolved_path>"`
    6. Bind Enter to shim script (Steps 4-6 from spec Section 6.2)
    7. `os.execvp("tmux", ["tmux", "-L", "nx_dash", "attach", "-t", "dashboard"])`
- Add `dash` command to `cli.py`

**Verification:**
```bash
uv run pytest tests/test_dashboard.py -x -v   # 7/7 passing
```

**Demo:**
```
$ uv run nx dash
[tmux dashboard with split panes showing all sessions read-only]
[press Enter → dashboard closes, attaches to selected session]
```

---

### Milestone 11: Node Management (`nx nodes`)

> **Goal:** Add/remove/list nodes with SSH config management, tmux version verification, and config drift detection.

**Harness (B):**
- `tests/test_nodes.py`:
  - `test_nodes_ls` — lists nodes with reachability and config drift status
  - `test_nodes_add_verifies_tmux` — checks remote tmux version ≥ 3.0
  - `test_nodes_add_creates_socket_dir` — `mkdir -p ~/.ssh/sockets/` is called
  - `test_nodes_add_pushes_tmux_conf` — idempotent scp of `src/nx/data/tmux.conf` to `~/.config/nexus/tmux.conf` on remote (located via `importlib.resources`)
  - `test_nodes_add_appends_ssh_config` — appends Host block to `~/.ssh/nexus_config` with ControlMaster/ControlPath/ControlPersist/ServerAliveInterval
  - `test_nodes_add_idempotent` — adding existing host doesn't duplicate the Host block
  - `test_nodes_rm` — removes Host block from nexus_config
  - `test_nodes_rm_nonexistent` — error message for unknown host
- `tmp_ssh_config` fixture: temp directory with mock nexus_config file

**Product (A):**
- `src/nx/nodes.py`:
  - `async nodes_ls(config) -> list[NodeStatus]` — ping + tmux.conf hash comparison
  - `async nodes_add(host, config)` — verify remote tmux ≥ 3.0, push `data/tmux.conf`, create `~/.ssh/sockets/`, append SSH config block
  - `nodes_rm(host, config)` — remove Host block from nexus_config
- `src/nx/data/tmux.conf` — canonical config (created in M0)
- Add `nodes` subcommand group to `cli.py` (`nodes ls`, `nodes add`, `nodes rm`)

**Verification:**
```bash
uv run pytest tests/test_nodes.py -x -v   # 8/8 passing
```

**Demo:**
```
$ uv run nx nodes ls
┌─────────────┬────────────┬──────────────┐
│ Node        │ Status     │ tmux.conf    │
├─────────────┼────────────┼──────────────┤
│ local       │ [OK]       │ [OK]         │
│ dev-server  │ [OK]       │ [DRIFT]      │
│ gpu-rig     │ [UNREACHABLE] │ -         │
└─────────────┴────────────┴──────────────┘

$ uv run nx nodes add new-server
Verified tmux 3.4 on new-server
Pushed tmux.conf to new-server
Added SSH config for new-server
```

---

### Milestone 12: Snapshot & Restore

> **Goal:** Save fleet state to JSON, restore by replaying `nx new` commands.

**Harness (B):**
- `tests/test_snapshot.py`:
  - `test_snapshot_saves_state` — captures all sessions to JSON at `~/.config/nexus/snapshot.json`
  - `test_snapshot_schema_valid` — output validates against `FleetSnapshot` pydantic model
  - `test_restore_creates_sessions` — reads snapshot, executes `nx new` for each entry
  - `test_restore_specific_node` — `--node dev-server` filters to one node's sessions
  - `test_restore_logs_output` — each restored session logged to stdout
  - `test_restore_empty_snapshot` — empty/missing file → "No sessions to restore"

**Product (A):**
- `src/nx/snapshot.py`:
  - `class SessionSnapshot(BaseModel): node, name, directory, command`
  - `class FleetSnapshot(BaseModel): timestamp: datetime, sessions: list[SessionSnapshot]`
  - `async save_snapshot(config) -> Path` — fan_out list → serialize to JSON
  - `async restore_snapshot(config, node_filter=None)` — read JSON → execute `nx new` per entry → log each to stdout
- Add `snapshot` and `restore` commands to `cli.py`

**Verification:**
```bash
uv run pytest tests/test_snapshot.py -x -v   # 6/6 passing
```

**Demo:**
```
$ uv run nx snapshot
Saved 4 sessions to ~/.config/nexus/snapshot.json

$ uv run nx restore
Restoring local/api... OK
Restoring local/worker... OK
Restoring dev-server/api... OK
Restoring dev-server/pipeline... OK
Restored 4 sessions
```

---

### Milestone 13: Integration Tests & Polish

> **Goal:** End-to-end workflows against real local tmux. Full regression. Clean-state guarantee.

**Harness (B):**

`tests/integration/conftest.py`:
- `clean_tmux` fixture (autouse, session-scoped): kills all sessions on `tmux -L nx_test` before and after the test session. Guarantees no leaked state between test runs.
- `nx_test_config` fixture: returns a `FleetConfig` with `nodes=["local"]` and the tmux socket overridden to `nx_test`.

`tests/integration/test_local_workflow.py`:
- `test_full_lifecycle` — `new → list → peek → send → logs → kill`: create a session running `cat`, verify it appears in list, peek shows output, send "hello", peek again shows "hello", logs captures it, kill removes it.
- `test_send_peek_roundtrip` — Create a session running `cat`. Send `"hello world"`. Wait 0.5s. Peek and verify `"hello world"` appears in output. **This is the critical test** that proves auto-Enter behavior works against real tmux, not just that the command string is correctly built.
- `test_snapshot_restore_cycle` — Create 2 sessions → snapshot → kill both → restore → list shows both recreated.
- `test_gc_cleans_exited` — Create a session with `"exit 0"` → wait for exit → gc → verify session is gone.
- `test_resolution_fully_qualified` — `peek "local/name"` works without fan-out.
- `test_new_duplicate_rejected` — Create session "foo", try creating "foo" again → error message.
- `test_fzf_roundtrip` — Verify the format/parse contract between resolution and fzf. Create two sessions with the same name on "local" (not possible — same node). Instead: create two sessions with different names, format them as fzf input (`local/sess1\nlocal/sess2`), pipe through `fzf --filter "sess1"`, verify output is `"local/sess1"` and that `resolve` can parse it back to `("local", "sess1")`. Uses `fzf --filter` which is non-interactive.
- `test_list_empty_fleet` — No sessions → "No active sessions" (not an error).

**Product (A):**
- Polish all CLI help text and error messages
- Ensure `--help` on every command is accurate
- Add any missing edge case handling discovered during integration testing
- Run full test suite as regression check: `uv run pytest tests/ -x -v`

**Verification:**
```bash
uv run pytest tests/ -x -v   # ALL tests passing (unit + integration)
```

**Demo:**
```
$ uv run nx new test-session "cat"
Created session local/test-session

$ uv run nx send test-session "hello world"
Sent to local/test-session

$ uv run nx peek test-session
hello world

$ uv run nx kill test-session
Killed session local/test-session

$ uv run nx list
No active sessions.
```

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **tmux output format varies across versions** | Medium | High | Pin format string with pipe delimiter (see Architecture). Test with tmux 3.0 minimum. Parse defensively — handle missing fields gracefully. |
| **SSH ControlMaster socket race conditions** | Medium | Medium | `nx nodes add` creates `~/.ssh/sockets/` explicitly. Bootstrap test in integration suite verifies. |
| **`os.execvp` in attach makes testing hard** | High | Medium | `mock_execvp` fixture records `(executable, args)`. Integration tests verify command correctness without actually exec'ing. |
| **fzf version differences** | Low | Low | Use only basic fzf features (`--query`, `--select-1`, `--filter`). No exotic flags. Integration test verifies round-trip with `--filter`. |
| **Dashboard tmux layout fragile for large fleets** | Medium | Medium | Cap pane count (e.g., 16). Warn for very large fleets. |
| **`$TMUX` parsing for anti-nesting** | Medium | High | Six explicit test cases covering all scenarios. Integration test for bare-terminal path. |
| **asyncio subprocess on WSL2** | Low | Medium | Primary dev environment is WSL2. Integration tests catch platform-specific issues early. |
| **`importlib.resources` path for tmux.conf** | Low | Low | Fallback to `Path(__file__).parent / "data" / "tmux.conf"` if resources API fails. |

---

## Definition of Done

- [ ] All unit tests pass (`uv run pytest tests/ --ignore=tests/integration -x -v`)
- [ ] All integration tests pass on local tmux (`uv run pytest tests/integration/ -x -v`)
- [ ] **All tests pass with a clean tmux state** — no pre-existing nexus sessions. The `clean_tmux` integration fixture enforces this; no test may depend on state leaked from another test.
- [ ] `uv run nx --help` shows all commands with accurate descriptions
- [ ] Fresh install works: `git clone → uv pip install -e . → nx --help`
- [ ] Every command in the spec (Section 5) is implemented and tested
- [ ] Resolution protocol handles all three cases (0/1/N matches)
- [ ] Attach anti-nesting handles all three scenarios (A/B/C)
- [ ] Dashboard Enter-key flow works end-to-end
- [ ] Config loading with env var expansion works
- [ ] Node management creates proper SSH config blocks
- [ ] Snapshot/restore round-trips correctly
- [ ] No hardcoded paths — all paths derived from config or env vars
- [ ] `rich` tables display correctly in terminal
- [ ] Error messages are clear and actionable (no raw tracebacks for user errors)
- [ ] Full test suite runs in CI without interactive dependencies
