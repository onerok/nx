Here is the synthesized specification. It takes the multi-machine parallel orchestration and fuzzy-finding from **Approach 1**, combines it with the brilliant dedicated-socket isolation, custom config, and `peek`/`send` subcommands from **Approach 2**, and expands the scope to be **completely tool-agnostic**.

Let's call this spec **`Nexus`** (or whatever you prefer to alias it to—`nx`, `fleet`, `hub`).

---

# The `Nexus` Spec: Distributed Terminal Orchestration

**Any machine. Any tool. One frictionless interface. Zero UI duplication.**

## The Philosophy
Web wrappers for CLI tools (like Claude Code or Aider) are an anti-pattern. They constantly chase upstream features, introduce latency, and break terminal primitives. Standard `tmux` is powerful, but mixing long-running AI agents, heavy-scrollback logs, and ad-hoc terminal work into your personal `tmux ls` creates a chaotic namespace.

**The Solution:** A stateless bash CLI that orchestrates a decentralized fleet of isolated `tmux` sockets over multiplexed SSH. It marries the speed of a local TUI with the ubiquity of a cloud dashboard.

## Architecture: The "Distributed Hub"

Instead of forcing all sessions onto one physical machine, **every machine in your fleet is a potential node.** 

```text
┌──────────────────────────┐      SSH ControlMaster (0ms latency)
│      Local Machine       │ ◄─────────────────────────────────────┐
│                          │                                       │
│  [ nexus CLI ]           │      ┌──────────────────────────┐     │      ┌──────────────────────────┐
│   - fzf orchestrator     │      │       Dev Server         │     │      │        GPU Rig           │
│   - parallel ssh polling │      │                          │     │      │                          │
│                          │      │ [ tmux -L nexus ]        │     │      │ [ tmux -L nexus ]        │
│  [ tmux -L nexus ]       │      │  ├── web-ui (claude)     │     │      │  ├── ml-train (bash)     │
│   ├── local-api (bash)   │      │  ├── backend (aider)     │     │      │  └── data-prep (codex)   │
│   └── notes (vim)        │      │  └── cron-logs (tail)    │     │      │                            │
└──────────────────────────┘      └──────────────────────────┘     │      └──────────────────────────┘
                                                                   │
┌──────────────────────────┐                                       │
│       Phone (Blink)      │ ◄─────────────────────────────────────┘
│  [ nexus ssh commands ]  │
└──────────────────────────┘
```

### The 3 Core Pillars
1. **Dedicated Sockets (`tmux -L nexus`)**: Nexus sessions are completely invisible to your normal `tmux`. They have their own configuration optimized for AI and ad-hoc work: 100k scrollback, `remain-on-exit on` (so you can read an AI agent's crash log), and activity monitoring.
2. **Tool Agnosticism**: A session is just a PTY. You can run `claude`, `aider`, `codex`, a tailing log, or just an ad-hoc `bash` shell.
3. **Zero-Latency Transport**: `Nexus` strictly relies on SSH `ControlMaster`. All subcommands (`peek`, `send`, `list`) reuse a single persistent Unix socket. Pinging 5 remote machines to list sessions takes milliseconds.

---

## The DX: A Day in the Life

### 1. Spin up the workday
You start your morning by allocating tasks across your fleet.
```bash
# Start an ad-hoc bash session locally
nx new local webapp bash

# Start Claude Code on the dev server
nx new dev-server api claude

# Start a heavy data script on the GPU rig
nx new gpu-rig training-pipeline "./train.sh"
```

### 2. Monitor without interrupting (`peek`)
You want to see how the dev server is doing, but attaching and detaching is too much context switching.
```bash
nx peek api
```
*(Instantly fetches the last 30 lines of the remote `api` pane via `capture-pane` over multiplexed SSH).*

### 3. Fire-and-forget commands (`send`)
Your boss asks you to run the integration tests on the dev server. You don't need to switch contexts. You just pipe keystrokes directly into the remote session:
```bash
nx send api "npm run test:e2e" ENTER
```

### 4. The Global Tiled Dashboard (`dash`)
You want to see everything at once.
```bash
nx dash
```
*Nexus creates a temporary, read-only local tmux window, splitting it into panes. Each pane SSHes into a different machine and runs `tmux attach -r`. You get a literal CCTV dashboard of Claude typing, logs scrolling, and scripts running across 3 different physical machines, all on your laptop screen.*

### 5. Flow State (`attach`)
Time to actually talk to Claude. 
```bash
nx attach api
```
*(If you just type `nx a`, it pops up a beautiful `fzf` menu across your entire fleet. Hitting Enter drops you instantly into the session).*

---

## Command Specification

| Command | Args | Description |
| :--- | :--- | :--- |
| `nx list` / `l` | | Parallel query of all nodes. Returns a formatted, grouped list of active sessions, uptime, and the running command. |
| `nx new` / `n` | `<node> <name> [cmd]` | Spawns a new session on a specific node. `[cmd]` defaults to `$SHELL`. |
| `nx attach` / `a` | `[name]` | Attaches to a session. If omitted, opens the global `fzf` selector. |
| `nx peek` / `p` | `<name>` | Prints the last 30 lines of a session to stdout (read-only). |
| `nx send` / `s` | `<name> <keys>` | Injects keystrokes into a session without attaching. |
| `nx kill` / `k` | `<name>` | Terminates a session. |
| `nx dash` / `d` | | Spawns a local split-pane dashboard viewing all active fleet sessions in read-only mode. |

> **Note on Fuzzy Matching:** Everywhere `<name>` is expected, `nx` uses fuzzy finding. If multiple sessions match, it falls back to an interactive `fzf` prompt.

---

## Technical Implementation Details

### 1. Configuration (`~/.config/nexus/fleet.conf`)
The only required configuration is defining your nodes.
```bash
# Nodes to query (must match ~/.ssh/config aliases)
# "local" is reserved for the current machine
NODES=("local" "dev-server" "gpu-rig")

# Default tool if none is specified
DEFAULT_CMD="$SHELL"
```

### 2. The Isolated Tmux Config (`~/.config/nexus/tmux.conf`)
Injected automatically via `-f` when `nx` starts a server.
```tmux
# 100k scrollback for massive LLM context windows
set -g history-limit 100000

# Keep the pane alive if Claude/Script crashes so we can read the error
set -g remain-on-exit on

# Don't kill the session when the last client disconnects
set -g destroy-unattached off

# Minimal status bar (just session name and active command)
set -g status-right "#(ps -t #{pane_tty} -o args= | tail -n 1)"
```

### 3. How `peek` works multi-node
When you type `nx peek api`:
1. The script checks a local cache or does a lightning-fast parallel SSH query to find which node owns `api`.
2. It executes: `ssh dev-server "tmux -L nexus capture-pane -t api -p -S -30"`
3. The result is printed locally. Total execution time: ~15ms (thanks to `ControlMaster`).

### 4. How `dash` works
The most "magical" feature. It leverages tmux's ability to nest cleanly if you use read-only mode.
1. `nx` queries all active sessions across the fleet.
2. It creates a temporary local tmux session: `tmux -L nexus_dash`
3. For each active remote session, it runs `tmux split-window` locally, passing the command: 
   `ssh -t <node> "tmux -L nexus attach-session -t <session> -r"` (The `-r` flag is crucial: read-only).
4. It runs `tmux select-layout tiled`.

### 5. SSH Multiplexing (Strict Requirement)
The installer enforces this in `~/.ssh/config`. Without it, `nx list` takes 2 seconds. With it, it takes 0.02 seconds.
```ssh-config
Host *
  ControlMaster auto
  ControlPath ~/.ssh/sockets/nx-%r@%h:%p
  ControlPersist 10m
```

## Why this is the ultimate spec

By taking the **isolation and structured API** of a dedicated Hub (Approach 2) and combining it with the **parallel orchestration** of a Fleet (Approach 1), you get:
* **True Centralization:** One CLI to rule all terminal tasks across all machines.
* **TUI Fidelity:** Every tool runs in its native environment. TUI spinners, streaming markdown, and colors work perfectly.
* **Extensibility:** Anthropic updates Claude Code? OpenAI updates Codex? You change absolutely nothing. Nexus just pipes the PTY.
* **Unmatched Speed:** No WebSockets, no Node.js proxies, no React rendering. Just native Unix domain sockets over multiplexed TCP.
