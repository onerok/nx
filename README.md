# nx

Stateless CLI for distributed tmux session orchestration. Manage tmux sessions across SSH-connected machines from a single command line.

## Overview

nx treats tmux as the database, SSH as the transport, and Python as the query layer. There is no daemon, no server process, no state files — every command fans out over SSH, queries tmux directly, and reports back.

## Requirements

- Python >= 3.12
- tmux >= 3.0
- [fzf](https://github.com/junegunn/fzf) (for interactive session/node picking)
- SSH access to remote nodes (for multi-machine usage)

## Installation

```bash
git clone https://github.com/onerok/nx.git
cd nx
uv pip install -e .
nx --help
```

## Configuration

nx reads its fleet config from `~/.config/nexus/fleet.toml`:

```toml
nodes = ["local", "dev-server", "gpu-rig"]
default_node = "local"
default_cmd = "$SHELL"           # expands env vars
max_concurrent_ssh = 16
auto_reap_clean_exit = false
```

If the file is missing, nx defaults to a single `local` node using your `$SHELL`.

## Commands

### Session management

| Command | Description |
|---------|-------------|
| `nx new [name] [cmd]` | Create a session (auto-generates a name if omitted, auto-attaches) |
| `nx attach [name]` | Attach to a session (fzf picker if name omitted) |
| `nx kill <name>` | Kill a session |
| `nx list` | List all sessions across the fleet |

### Observability

| Command | Description |
|---------|-------------|
| `nx peek <name>` | Show last 30 lines of a session's output |
| `nx logs <name>` | Capture scrollback (100 lines interactive, full when piped) |
| `nx dash` | CCTV-style read-only dashboard of all sessions |

### Interaction

| Command | Description |
|---------|-------------|
| `nx send <name> <keys...>` | Send keystrokes (auto-appends Enter) |
| `nx send --raw <name> <keys...>` | Send keys verbatim (e.g. `C-c`) |

### Fleet operations

| Command | Description |
|---------|-------------|
| `nx gc` | Reap all exited sessions (with confirmation) |
| `nx gc --dry-run` | List exited sessions without killing |
| `nx snapshot` | Save fleet state to `~/.config/nexus/snapshot.json` |
| `nx restore` | Recreate sessions from a snapshot |

### Node management

| Command | Description |
|---------|-------------|
| `nx nodes ls` | List nodes with reachability and config drift status |
| `nx nodes add [host]` | Add a node (discovers from `~/.ssh/config` if no host given) |
| `nx nodes rm <host>` | Remove a node |

## Session resolution

Sessions can be referenced by bare name (`api`) or fully qualified (`dev-server/api`). When a bare name matches sessions on multiple nodes, nx launches fzf for disambiguation. In non-interactive (piped) contexts, it errors with the list of matches.

## Attach anti-nesting

`nx attach` detects your terminal context and does the right thing:

- **Bare terminal** — replaces the process (`execvp`) to attach directly
- **Inside nexus tmux** — switches client or opens a new window
- **Inside your personal tmux** — opens a new window that nests into the nexus session

## Aliases

- `nx l` — `nx list`
- `nx a` — `nx attach`

## Development

```bash
uv pip install -e ".[dev]"

# Unit tests (no external deps)
uv run pytest tests/ -x -v --ignore=tests/integration

# Integration tests (requires local tmux)
uv run pytest tests/integration/ -x -v

# Full suite
uv run pytest tests/ -x -v
```

## License

MIT
