# Container Transport Support

## Problem

`nx nodes add` and `run_on_node` assume SSH as the only transport. Users with Docker containers, devcontainers, or LXC need to manage tmux sessions inside containers without requiring sshd.

## Proposal

Add a transport abstraction so each node declares its type. `run_on_node` dispatches to the right command prefix based on node transport.

### Node config in `fleet.toml`

```toml
[[nodes]]
name = "local"
transport = "local"

[[nodes]]
name = "dev-server"
transport = "ssh"

[[nodes]]
name = "my-container"
transport = "docker"
target = "container_name_or_id"  # docker exec target

[[nodes]]
name = "my-devcontainer"
transport = "docker-compose"
target = "service_name"
compose_file = "/path/to/docker-compose.yml"  # optional
```

### Transport dispatch in `ssh.py`

Currently `run_on_node` does:
- `local` → `asyncio.create_subprocess_exec(*cmd)`
- anything else → `asyncio.create_subprocess_exec("ssh", node, *cmd)`

Proposed:
- `local` → `exec(*cmd)`
- `ssh` → `exec("ssh", "-o", "ConnectTimeout=2", node, *cmd)`
- `docker` → `exec("docker", "exec", target, *cmd)`
- `docker-compose` → `exec("docker", "compose", "exec", "-T", target, *cmd)`

### What stays the same

Everything above the transport layer is unchanged:
- tmux command building
- session resolution / fan-out
- list, attach, peek, send, kill, gc, snapshot, dash

### Attach considerations

`nx attach` currently uses `os.execvp("ssh", ...)` for remote nodes. Container attach would need:
- `os.execvp("docker", ["docker", "exec", "-it", target, "tmux", "-L", "nexus", "attach", "-t", session])`

### Open questions

- Should `nx nodes add` for containers verify tmux is installed inside the container?
- Should we support `podman` as a transport too? (trivially aliased to docker)
- LXC/Incus transport (`lxc exec <name> -- <cmd>`)?
- How to handle containers that get recreated (name stays, ID changes)?
- Config format: flat `nodes = [...]` list vs structured `[[nodes]]` table — this is a breaking change to fleet.toml schema

### Effort estimate

Small change to `ssh.py` and `config.py`. The rest of the stack is transport-agnostic already.
