# Specification: Nexus (Distributed Terminal Orchestration)

## 1. Overview

Nexus is a stateless bash CLI that orchestrates a decentralized fleet of isolated `tmux` sockets over multiplexed SSH. It provides a frictionless interface to run, monitor, and interact with persistent terminal sessions (e.g., Claude Code, Aider, tailing logs, heavy bash scripts) across multiple physical machines, without duplicating terminal UI or relying on web-based proxy layers.

By combining `tmux`'s battle-tested session management with SSH `ControlMaster`'s zero-latency transport, Nexus makes a 10-node remote fleet feel like a local TUI.

## 2. System Requirements & Bootstrapping

**Dependencies:**

* `bash` 4.0+
* `tmux` 3.0+ (required on all nodes)
* `fzf` (required locally for interactive menus. *Fallback:* The script must gracefully degrade to a standard bash `select` numbered list if `fzf` is unavailable).
* `ssh` client/server

**Bootstrapping Requirements:**
The Nexus installer and the `nx nodes add` command **must** explicitly create the local multiplex socket directory: `mkdir -p ~/.ssh/sockets/`. Failure to do so will cause all SSH `ControlMaster` connections to fail silently or with cryptic socket errors.

## 3. Core Architecture

Nexus relies on **Stateless Fan-Out over Multiplexed SSH**.

* **No Daemon:** The source of truth for all sessions is the `tmux` daemon running on each respective node. There is no local database or cache to invalidate.
* **Isolated Sockets:** Nexus sessions run exclusively in `tmux -L nexus`. They do not pollute the user's personal `tmux ls` namespace.
* **Global Identification:** Every session is uniquely identified by `<node>/<session>` (e.g., `gpu-rig/data-pipeline`).

### 3.1 The Resolution Protocol

To prevent users from typing fully qualified names constantly, Nexus uses a stateless resolution protocol for all session-targeting commands (`peek`, `logs`, `send`, `attach`, `kill`):

1. Nexus performs a parallel fan-out SSH query to all configured nodes using a strict `ConnectTimeout=2`.
2. **0 matches:** Abort with `Error: Session not found.`
3. **1 match:** Instantly execute the command on the target node.
4. **>1 match (Collision):** Check execution context via `isatty`.
    * *Interactive Context (`-t 1`):* Open an `fzf` (or `select`) menu to allow the user to select the correct `<node>/<session>`. The entry matching `DEFAULT_NODE` (from config) should be pre-selected/sorted to the top.
    * *Piped/Scripted Context (`! -t 1`):* Abort deterministically with `Exit 1: Ambiguous session. Matches: local/api, dev-server/api. Use fully qualified name.`

## 4. Configuration (`~/.config/nexus/fleet.conf`)

```bash
# Nodes in the fleet (must match ~/.ssh/nexus_config Host entries)
# "local" is a reserved keyword for the executing machine.
NODES=("local" "dev-server" "gpu-rig")

# Default target for 'nx new' if --on is omitted
DEFAULT_NODE="local"

# Default tool to execute if no command is specified
DEFAULT_CMD="$SHELL"

# Automatically delete panes that exit with code 0.
# (Code >0 will always remain for crash inspection).
AUTO_REAP_CLEAN_EXIT=true
```

## 5. Command Surface

### Execution & Interaction

| Command | Description |
| :--- | :--- |
| `nx new [--on <node>] [--dir <path>] <name> [cmd]` | Spawns a new session. If `--on` is omitted, uses `DEFAULT_NODE`. If `--dir` is omitted, defaults to `$PWD` (local) or `$HOME` (remote). |
| `nx attach <name>` | Attaches to the session. *(Note: standard `tmux` resizes the session to the smallest attached client. Attaching from a phone will resize the view for desktop clients).* See **Section 6.1** for anti-nesting rules. |
| `nx send [--raw] <name> <keys...>` | Injects keystrokes. Default behavior automatically appends `Enter` to the final argument (optimized for LLMs). `--raw` passes arguments strictly 1:1 to `tmux send-keys` (e.g., `nx send --raw api C-c "npm start" Enter`). |
| `nx kill <name>` | Forcefully terminates a target session. |

### Observability

| Command | Description |
| :--- | :--- |
| `nx list` / `l` | Parallel fleet query. Groups by node. Flags degraded nodes as `[⚠️ UNREACHABLE]`. Displays name, **working directory**, **child command**, and status. Distinguishes `[RUNNING]`, `[EXITED 0]`, and `[EXITED 1]`. |
| `nx peek <name>` | Prints the last 30 lines to `stdout` (`tmux capture-pane -p -S -30`). |
| `nx logs <name> [--lines N]` | Dumps session scrollback. **Context-aware:** If interactive (`isatty`), defaults to the last 100 lines to prevent terminal flooding. If piped (`nx logs api > out.txt`), defaults to the full 100k line history (`capture-pane -p -S -`). |
| `nx dash` | Spawns a read-only CCTV dashboard of all active sessions. *(Note: Because this attaches with `-r` read-only mode, it bypasses terminal size negotiation. You can view a 4K desktop session from a phone via dashboard without resizing the desktop).* See **Section 6.2**. |

### Fleet & Lifecycle Management

| Command | Description |
| :--- | :--- |
| `nx gc [--dry-run] [name]` | Reaps `[EXITED]` panes. If `[name]` is omitted, performs a fleet-wide sweep. **Must prompt for confirmation** interactively unless piped. |
| `nx snapshot` | Saves current fleet state (node, session name, pwd, command) to `~/.config/nexus/snapshot.json`. *Recommendation: Users should add this to a daily cron job or systemd hook.* |
| `nx restore [--node <node>]` | Reads snapshot and automatically executes `nx new` commands to recreate the layout. Explicitly logs each restored session to `stdout` so the user has a record, as short-lived restored scripts may be instantly destroyed if `AUTO_REAP_CLEAN_EXIT=true`. |
| `nx nodes ls` | Lists nodes, pings multiplex sockets, and hashes the remote `tmux.conf` comparing it to the local version, flagging `[OK]` or `[DRIFT]`. |
| `nx nodes add <host>` | Verifies remote tmux 3.0+, idempotently pushes `tmux.conf`, and appends scoped SSH configs. |
| `nx nodes rm <host>` | Removes the host block from `~/.ssh/nexus_config`. |

## 6. Implementation Details

### 6.1 The `nx attach` Anti-Nesting Strategy

Attaching to a remote `tmux` session from inside an existing `tmux` session creates a broken nested environment. `nx attach` must intercept this via the `$TMUX` environment variable and correctly target the outer wrapper socket.

**Scenario A: Executing from a bare terminal (No `$TMUX`)**

* Execute standard attach: `ssh -t <node> "tmux -L nexus attach -t <session>"`

**Scenario B: Executing from inside `tmux -L nexus` (Already in a Nexus session)**

* *If target is local:* Use tmux native client switching to swap the session without nesting:
    `tmux -L nexus switch-client -t <session>`
* *If target is remote:* Open a self-closing wrapper window explicitly on the `nexus` socket to prevent nesting while jumping nodes:
    `tmux -L nexus new-window -n "<session>" "ssh -t <node> 'tmux -L nexus attach -t <session>'"`

**Scenario C: Executing from a user's personal `tmux` (Different socket)**

* Cannot use `switch-client` across sockets.
* Must open a new window on the user's current `$TMUX` socket:
    `tmux new-window -n "<session>" "ssh -t <node> 'tmux -L nexus attach -t <session>'"`

**Zombie Prevention (Scenarios B & C):** Do not configure `remain-on-exit` for this specific new wrapper window. When the user detaches from the remote Nexus session, the SSH command exits, and the local window must automatically close, returning the user seamlessly to their previous local tab.

### 6.2 The Dashboard (`nx dash`) Dual Code Path

The dashboard operates by creating a temporary local tmux session (`tmux -L nx_dash`), splitting it into read-only panes (`ssh ... attach -r`), and embedding the target session name into pane metadata.

When the user presses `Enter` on a pane to jump into it interactively, implementers must handle the state transition perfectly:

1. **Tag Metadata:** When building the dash, tag each pane: `tmux -L nx_dash set-option -p @nx_target "<node>/<session>"`
2. **Bind Enter:** Map `Enter` in the `nx_dash` session to a shell script that performs steps 3-5.
3. **Capture Target:** `TARGET=$(tmux -L nx_dash display-message -p '#{@nx_target}')`
4. **Teardown Dash:** `tmux -L nx_dash detach-client && tmux -L nx_dash kill-session`
5. **Context Shift & Execute:** At this exact moment, the dashboard is gone and the user is dropped back into their *original* terminal context. The script executes `nx attach "$TARGET"`.

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
