# Specification: Nexus (Distributed Terminal Orchestration)

## 1. Overview

Nexus is a stateless Python CLI that orchestrates a decentralized fleet of isolated `tmux` sockets over multiplexed SSH. It provides a frictionless interface to run, monitor, and interact with persistent terminal sessions (e.g., Claude Code, Aider, tailing logs, heavy scripts) across multiple physical machines, without duplicating terminal UI or relying on web-based proxy layers.

By combining `tmux`'s battle-tested session management with SSH `ControlMaster`'s zero-latency transport, Nexus makes a 10-node remote fleet feel like a local TUI.

## 2. System Requirements & Bootstrapping

**Runtime:**

* Python 3.12+
* `uv` (package manager, virtual environment, and script runner)
* `tmux` 3.0+ (required on all nodes)
* `ssh` client/server

* `fzf` (required locally for interactive menus)

**Project Structure:**

```
nx/
  pyproject.toml          # uv-managed project, entry point: nx
  src/
    nx/
      __init__.py
      cli.py              # Typer/Click CLI entry point
      config.py           # Fleet config loading (pydantic models)
      ssh.py              # SSH multiplexing, async fan-out
      tmux.py             # tmux command builder & parser
      resolve.py          # Session resolution protocol
      dashboard.py        # nx dash implementation
      snapshot.py         # Snapshot save/restore
      nodes.py            # Node management (add/rm/ls)
  tests/
    ...
```

**Installation:**

```bash
# Install from source (editable dev mode)
uv pip install -e .

# Or run directly without installing
uv run nx --help
```

The `pyproject.toml` declares `nx` as a console script entry point, so after install the `nx` command is available system-wide within the venv.

**Bootstrapping Requirements:**
The `nx nodes add` command **must** explicitly create the local multiplex socket directory: `mkdir -p ~/.ssh/sockets/`. Failure to do so will cause all SSH `ControlMaster` connections to fail silently or with cryptic socket errors.

## 3. Core Architecture

Nexus relies on **Stateless Fan-Out over Multiplexed SSH**.

* **No Daemon:** The source of truth for all sessions is the `tmux` daemon running on each respective node. There is no local database or cache to invalidate.
* **Isolated Sockets:** Nexus sessions run exclusively in `tmux -L nexus`. They do not pollute the user's personal `tmux ls` namespace.
* **Global Identification:** Every session is uniquely identified by `<node>/<session>` (e.g., `gpu-rig/data-pipeline`).
* **Async Fan-Out:** All multi-node queries use `asyncio.gather()` with `asyncio.subprocess` for concurrent SSH execution, bounded by `asyncio.Semaphore(16)` to prevent fd exhaustion on large fleets. The default limit of 16 concurrent SSH connections balances throughput against system fd limits and SSH handshake overhead; sufficient for most fleets and configurable via `max_concurrent_ssh` in the fleet config if needed.

### 3.1 The Resolution Protocol

To prevent users from typing fully qualified names constantly, Nexus uses a stateless resolution protocol for all session-targeting commands (`peek`, `logs`, `send`, `attach`, `kill`).

**Dependency validation:** At CLI init (the top-level Typer callback), Nexus checks `shutil.which("fzf")` and aborts with a clear error (`fzf is required but not found on $PATH`) if missing. This fail-fast check runs once before any command dispatch, so users never hit a cryptic runtime error mid-resolution.

1. Nexus performs a parallel async fan-out SSH query to all configured nodes using a strict `ConnectTimeout=2`.
2. **0 matches:** Abort with `Error: Session not found.`
3. **1 match:** Instantly execute the command on the target node.
4. **>1 match (Collision):** Check execution context via `sys.stdin.isatty()`.
    * *Interactive Context:* Pipe matches into `fzf` for the user to select the correct `<node>/<session>`. The entry matching `default_node` (from config) should be pre-selected/sorted to the top via fzf's `--query` or ordering.
    * *Non-interactive Context:* Abort deterministically with `sys.exit(1)` and message: `Ambiguous session. Matches: local/api, dev-server/api. Use fully qualified name.`

### 3.2 Key Libraries

| Library | Purpose |
| :--- | :--- |
| `typer` | CLI framework with auto-generated help, completion |
| `pydantic` | Config validation and snapshot schema |
| `asyncio` | Concurrent SSH fan-out (stdlib) |
| `tomllib` | TOML config parsing (stdlib since 3.12) |
| `rich` | Terminal output formatting, tables, spinners |

## 4. Configuration (`~/.config/nexus/fleet.toml`)

```toml
# Nodes in the fleet (must match ~/.ssh/nexus_config Host entries)
# "local" is a reserved keyword for the executing machine.
nodes = ["local", "dev-server", "gpu-rig"]

# Default target for 'nx new' if --on is omitted
default_node = "local"

# Default tool to execute if no command is specified.
# Supports environment variable references — expanded at load time via os.path.expandvars().
default_cmd = "$SHELL"

# Max concurrent SSH connections during fan-out queries (default: 16).
max_concurrent_ssh = 16

# Automatically delete panes that exit with code 0.
# (Code >0 will always remain for crash inspection).
auto_reap_clean_exit = true
```

Parsed with `tomllib` (stdlib) and loaded into a `pydantic.BaseModel`. String fields undergo `os.path.expandvars()` expansion at load time so that `$SHELL`, `$HOME`, `$HOSTNAME`, etc. resolve to their runtime values:

```python
import os
import tomllib
from pydantic import BaseModel, field_validator

class FleetConfig(BaseModel):
    nodes: list[str] = ["local"]
    default_node: str = "local"
    default_cmd: str = "$SHELL"
    max_concurrent_ssh: int = 16
    auto_reap_clean_exit: bool = True

    @field_validator("default_node", "default_cmd", mode="before")
    @classmethod
    def expand_env_vars(cls, v: str) -> str:
        return os.path.expandvars(v)
```

## 5. Command Surface

### Execution & Interaction

| Command | Description |
| :--- | :--- |
| `nx new [--on <node>] [--dir <path>] <name> [cmd]` | Spawns a new session. If `--on` is omitted, uses `default_node`. If `--dir` is omitted, defaults to `$PWD` (local) or `$HOME` (remote). |
| `nx attach <name>` | Attaches to the session. *(Note: standard `tmux` resizes the session to the smallest attached client. Attaching from a phone will resize the view for desktop clients).* See **Section 6.1** for anti-nesting rules. |
| `nx send [--raw] <name> <keys...>` | Injects keystrokes. Default behavior automatically appends `Enter` to the final argument (optimized for LLMs). `--raw` passes arguments strictly 1:1 to `tmux send-keys` (e.g., `nx send --raw api C-c "npm start" Enter`). |
| `nx kill <name>` | Forcefully terminates a target session. |

### Observability

| Command | Description |
| :--- | :--- |
| `nx list` / `nx l` | Async parallel fleet query. Groups by node using `rich` tables. Flags degraded nodes as `[UNREACHABLE]`. Displays name, **working directory**, **child command**, and status. Distinguishes `[RUNNING]`, `[EXITED 0]`, and `[EXITED 1]`. |
| `nx peek <name>` | Prints the last 30 lines to `stdout` (`tmux capture-pane -p -S -30`). |
| `nx logs <name> [--lines N]` | Dumps session scrollback. **Context-aware:** If interactive (`isatty`), defaults to the last 100 lines to prevent terminal flooding. If piped (`nx logs api > out.txt`), defaults to the full 100k line history (`capture-pane -p -S -`). |
| `nx dash` | Spawns a read-only CCTV dashboard of all active sessions. *(Note: Because this attaches with `-r` read-only mode, it bypasses terminal size negotiation. You can view a 4K desktop session from a phone via dashboard without resizing the desktop).* See **Section 6.2**. |

### Fleet & Lifecycle Management

| Command | Description |
| :--- | :--- |
| `nx gc [--dry-run] [name]` | Reaps `[EXITED]` panes. If `[name]` is omitted, performs a fleet-wide sweep. **Must prompt for confirmation** interactively (via `typer.confirm()`) unless piped. |
| `nx snapshot` | Saves current fleet state (node, session name, pwd, command) to `~/.config/nexus/snapshot.json` using a pydantic model. *Recommendation: Users should add this to a daily cron job or systemd hook.* |
| `nx restore [--node <node>]` | Reads snapshot and automatically executes `nx new` commands to recreate the layout. Explicitly logs each restored session to `stdout` so the user has a record, as short-lived restored scripts may be instantly destroyed if `auto_reap_clean_exit=true`. |
| `nx nodes ls` | Lists nodes, pings multiplex sockets, and hashes the remote `tmux.conf` comparing it to the local version, flagging `[OK]` or `[DRIFT]`. |
| `nx nodes add <host>` | Verifies remote tmux 3.0+, idempotently pushes `tmux.conf`, creates `~/.ssh/sockets/`, and appends scoped SSH configs. |
| `nx nodes rm <host>` | Removes the host block from `~/.ssh/nexus_config`. |

## 6. Implementation Details

### 6.1 The `nx attach` Anti-Nesting Strategy

Attaching to a remote `tmux` session from inside an existing `tmux` session creates a broken nested environment. `nx attach` must intercept this via the `$TMUX` environment variable (`os.environ.get("TMUX")`) and correctly target the outer wrapper socket.

**Scenario A: Executing from a bare terminal (No `$TMUX`)**

* Use `os.execvp()` to replace the Python process for clean signal handling.
* *If target is local:* `os.execvp("tmux", ["tmux", "-L", "nexus", "attach", "-t", "<session>"])`
* *If target is remote:* `os.execvp("ssh", ["ssh", "-t", "<node>", "tmux", "-L", "nexus", "attach", "-t", "<session>"])`

**Scenario B: Executing from inside `tmux -L nexus` (Already in a Nexus session)**

* *If target is local:* Use tmux native client switching to swap the session without nesting:
    `tmux -L nexus switch-client -t <session>`
    *(Note: `switch-client` operates at the client level — it changes the active session for the entire tmux client, not just the current window. If the user has multiple nexus windows open, all windows switch context. This is acceptable since nexus sessions are typically one-window-per-session. If multi-window workflows become common, the fix is to use `new-window` for local targets as well.)*
* *If target is remote:* Open a self-closing wrapper window explicitly on the `nexus` socket to prevent nesting while jumping nodes:
    `tmux -L nexus new-window -n "<session>" "ssh -t <node> 'tmux -L nexus attach -t <session>'"`

**Scenario C: Executing from a user's personal `tmux` (Different socket)**

* Cannot use `switch-client` across sockets.
* Must open a new window on the user's current `$TMUX` socket:
    `tmux new-window -n "<session>" "ssh -t <node> 'tmux -L nexus attach -t <session>'"`

**Zombie Prevention (Scenarios B & C):** Do not configure `remain-on-exit` for this specific new wrapper window. When the user detaches from the remote Nexus session, the SSH command exits, and the local window must automatically close, returning the user seamlessly to their previous local tab.

### 6.2 The Dashboard (`nx dash`) Dual Code Path

The dashboard operates by creating a temporary local tmux session (`tmux -L nx_dash`), splitting it into read-only panes (`ssh ... attach -r`), and embedding the target session name into pane metadata.

When the user presses `Enter` on a pane to jump into it interactively, the bound handler must manage the state transition perfectly:

1. **Tag Metadata:** When building the dash, tag each pane: `tmux -L nx_dash set-option -p @nx_target "<node>/<session>"`
2. **Capture Binary Path:** At dashboard creation time, resolve the absolute path to the `nx` binary via `shutil.which("nx")` or `sys.argv[0]`. Store it as a tmux variable: `tmux -L nx_dash set-environment NX_BIN "<resolved_path>"`. This ensures the shim works regardless of whether the user's tmux shell has the venv activated.
3. **Bind Enter:** Map `Enter` in the `nx_dash` session to a small shell shim that performs steps 4-6.
4. **Capture Target:** `NX_BIN=$(tmux -L nx_dash show-environment -h NX_BIN | cut -d= -f2); TARGET=$(tmux -L nx_dash display-message -p '#{@nx_target}')`
5. **Teardown Dash:** `tmux -L nx_dash detach-client && tmux -L nx_dash kill-session`
6. **Context Shift & Execute:** At this exact moment, the dashboard is gone and the user is dropped back into their *original* terminal context. The shim executes `"$NX_BIN" attach "$TARGET"`.

Because Step 5 occurs after teardown, `nx attach` correctly evaluates `$TMUX`. If they launched `nx dash` from inside their personal tmux, `nx attach` detects `$TMUX` and fires the appropriate `new-window` logic. If launched from a bare terminal, it performs a clean SSH attach.

### 6.3 Safe SSH Config (`~/.ssh/nexus_config`)

Nexus **must not** programmatically edit `~/.ssh/config` directly. Programmatic editing of the primary SSH config risks locking users out of their machines.

Instead, the installer asks the user to manually add exactly one line to the very top of `~/.ssh/config`:
`Include ~/.ssh/nexus_config`

`nx nodes add <host>` idempotently appends multiplexing blocks exclusively to `nexus_config`:

```ssh-config
Host dev-server
  ControlMaster auto
  ControlPath ~/.ssh/sockets/nx-%r@%h:%p
  ControlPersist 10m
  ServerAliveInterval 30
```

### 6.4 Subprocess & Process Replacement Strategy

The attach command uses different process strategies depending on the tmux nesting scenario (see Section 6.1):

| Scenario | Context | Strategy | Reason |
| :--- | :--- | :--- | :--- |
| **A** | Bare terminal, no `$TMUX` | `os.execvp("ssh", ...)` | Replaces Python process. Clean signal propagation (Ctrl-C, SIGWINCH), proper terminal ownership, no zombie process. |
| **B** | Inside `tmux -L nexus` (local target) | `subprocess.run(["tmux", "-L", "nexus", "switch-client", ...])` + `sys.exit(0)` | `switch-client` returns immediately after swapping the session. No process to own. |
| **B** | Inside `tmux -L nexus` (remote target) | `subprocess.run(["tmux", "-L", "nexus", "new-window", ...])` + `sys.exit(0)` | `new-window` spawns the window and returns immediately. The SSH session lives inside the new tmux window, not in the Python process. |
| **C** | Inside user's personal tmux | `subprocess.run(["tmux", "new-window", ...])` + `sys.exit(0)` | Same as Scenario B remote — `new-window` returns immediately. |

The `nx dash` command always uses `os.execvp()` because it attaches to the `nx_dash` tmux session, which is a long-lived interactive process.

For fire-and-forget commands (`new`, `send`, `kill`) and query commands (`list`, `peek`, `logs`), use `asyncio.create_subprocess_exec()` for structured output capture and error handling.
