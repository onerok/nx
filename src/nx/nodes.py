"""Node management: list, add, and remove fleet nodes."""

import asyncio
import hashlib
import importlib.resources
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from nx.config import FleetConfig
from nx.ssh import run_on_node


# SSH config file path for nexus-managed connections.
NEXUS_SSH_CONFIG = Path.home() / ".ssh" / "nexus_config"

# SSH config block template for new nodes.
SSH_CONFIG_TEMPLATE = """
Host {host}
    ControlMaster auto
    ControlPath ~/.ssh/sockets/nx-%r@%h:%p
    ControlPersist 10m
    ServerAliveInterval 30
"""


@dataclass
class NodeStatus:
    """Status of a fleet node.

    Attributes:
        node: Node hostname.
        reachable: Whether the node responded to SSH.
        tmux_version: Remote tmux version string, or None if unreachable.
        config_drift: True if remote tmux.conf differs from canonical.
    """

    node: str
    reachable: bool
    tmux_version: str | None
    config_drift: bool


def _local_tmux_conf_hash() -> str:
    """Compute MD5 hash of the canonical tmux.conf shipped with nexus.

    Returns:
        str: Hex digest of the canonical tmux.conf.
    """
    ref = importlib.resources.files("nx.data").joinpath("tmux.conf")
    content = ref.read_bytes()
    return hashlib.md5(content).hexdigest()


async def _check_node(node: str, local_hash: str) -> NodeStatus:
    """Check a single node's reachability, tmux version, and config drift.

    Args:
        node: Node hostname.
        local_hash: MD5 hash of the canonical tmux.conf.

    Returns:
        NodeStatus: Status of the node.
    """
    if node == "local":
        # Local node is always reachable; check tmux version.
        result = await run_on_node("local", ["tmux", "-V"])
        version = result.stdout.strip() if result.returncode == 0 else None
        # Check local tmux.conf drift.
        conf_path = Path.home() / ".config" / "nexus" / "tmux.conf"
        if conf_path.exists():
            remote_hash = hashlib.md5(conf_path.read_bytes()).hexdigest()
            drift = remote_hash != local_hash
        else:
            drift = False
        return NodeStatus(node="local", reachable=True, tmux_version=version, config_drift=drift)

    # Remote node: check reachability + tmux version.
    result = await run_on_node(node, ["tmux", "-V"])
    if result.returncode != 0:
        return NodeStatus(node=node, reachable=False, tmux_version=None, config_drift=False)

    version = result.stdout.strip()

    # Check remote tmux.conf hash.
    hash_result = await run_on_node(node, ["md5sum", str(Path("~/.config/nexus/tmux.conf"))])
    if hash_result.returncode == 0:
        remote_hash = hash_result.stdout.split()[0]
        drift = remote_hash != local_hash
    else:
        drift = False

    return NodeStatus(node=node, reachable=True, tmux_version=version, config_drift=drift)


async def nodes_ls(config: FleetConfig) -> list[NodeStatus]:
    """List all fleet nodes with their status.

    Checks each node's reachability, tmux version, and config drift
    concurrently.

    Args:
        config: Fleet configuration.

    Returns:
        list[NodeStatus]: Status of each node in the fleet.
    """
    local_hash = _local_tmux_conf_hash()
    tasks = [_check_node(node, local_hash) for node in config.nodes]
    return list(await asyncio.gather(*tasks))


async def nodes_add(host: str, config: FleetConfig, ssh_config_path: Path | None = None) -> list[str]:
    """Add a new node to the fleet.

    Verifies remote tmux version >= 3.0, creates SSH socket directory,
    pushes canonical tmux.conf, and appends SSH config block.

    Args:
        host: Hostname to add.
        config: Fleet configuration.
        ssh_config_path: Path to SSH config file. Defaults to ~/.ssh/nexus_config.

    Returns:
        list[str]: Log messages describing actions taken.

    Raises:
        RuntimeError: If remote tmux is missing or version < 3.0.
    """
    log: list[str] = []
    config_path = ssh_config_path or NEXUS_SSH_CONFIG

    # Step 1: Verify remote tmux version.
    result = await run_on_node(host, ["tmux", "-V"])
    if result.returncode != 0:
        raise RuntimeError(f"Cannot reach {host} or tmux not installed.")

    version_str = result.stdout.strip()
    # Parse version number from "tmux X.Y" format.
    match = re.search(r"(\d+\.\d+)", version_str)
    if not match or float(match.group(1)) < 3.0:
        raise RuntimeError(f"tmux >= 3.0 required on {host}, found: {version_str}")

    log.append(f"Verified {version_str} on {host}")

    # Step 2: Create SSH socket directory on local machine.
    socket_dir = Path.home() / ".ssh" / "sockets"
    socket_dir.mkdir(parents=True, exist_ok=True)
    log.append(f"Ensured {socket_dir} exists")

    # Step 3: Push canonical tmux.conf to remote.
    ref = importlib.resources.files("nx.data").joinpath("tmux.conf")
    content = ref.read_text()
    # Reason: We use a heredoc via bash to write the file on the remote side.
    # This avoids needing scp and makes the operation testable with mock SSH.
    await run_on_node(host, ["mkdir", "-p", "~/.config/nexus"])
    await run_on_node(
        host,
        ["bash", "-c", f"mkdir -p ~/.config/nexus && cat > ~/.config/nexus/tmux.conf << 'NXEOF'\n{content}\nNXEOF"],
    )
    log.append(f"Pushed tmux.conf to {host}")

    # Step 4: Append SSH config block (idempotent).
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        existing = config_path.read_text()
    else:
        existing = ""

    # Check if host block already exists.
    if f"Host {host}" not in existing:
        block = SSH_CONFIG_TEMPLATE.format(host=host)
        with open(config_path, "a") as f:
            f.write(block)
        log.append(f"Added SSH config for {host}")
    else:
        log.append(f"SSH config for {host} already exists")

    return log


def nodes_rm(host: str, ssh_config_path: Path | None = None) -> list[str]:
    """Remove a node's SSH config block.

    Args:
        host: Hostname to remove.
        ssh_config_path: Path to SSH config file. Defaults to ~/.ssh/nexus_config.

    Returns:
        list[str]: Log messages describing actions taken.

    Raises:
        ValueError: If the host is not found in the SSH config.
    """
    config_path = ssh_config_path or NEXUS_SSH_CONFIG
    log: list[str] = []

    if not config_path.exists():
        raise ValueError(f"Host '{host}' not found in SSH config.")

    content = config_path.read_text()

    if f"Host {host}" not in content:
        raise ValueError(f"Host '{host}' not found in SSH config.")

    # Remove the Host block (from "Host <host>" to next "Host " or EOF).
    # Reason: Each block starts with "\nHost <name>\n" and includes indented
    # lines until the next "\nHost " or end of file.
    pattern = rf"\nHost {re.escape(host)}\n(?:    [^\n]*\n)*"
    new_content = re.sub(pattern, "", content)

    config_path.write_text(new_content)
    log.append(f"Removed SSH config for {host}")

    return log
