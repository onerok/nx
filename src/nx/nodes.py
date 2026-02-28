"""Node management: list, add, and remove fleet nodes."""

import asyncio
import glob as globmod
import hashlib
import importlib.resources
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from nx.config import FleetConfig, save_config
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


def parse_ssh_config_hosts(config_path: Path | None = None) -> list[str]:
    """Parse ~/.ssh/config for Host entries, following Include directives.

    Reads the SSH config file, expands Include directives (with ~ and glob
    support), extracts Host entries (splitting multi-host lines), and filters
    out wildcard patterns containing *, ?, or !.

    Args:
        config_path: Path to SSH config file. Defaults to ~/.ssh/config.

    Returns:
        list[str]: Sorted, deduplicated list of concrete hostnames.
    """
    config_path = config_path or Path.home() / ".ssh" / "config"
    hosts: set[str] = set()
    _parse_ssh_config_file(config_path, hosts)
    return sorted(hosts)


def _parse_ssh_config_file(path: Path, hosts: set[str]) -> None:
    """Recursively parse a single SSH config file for Host/Include directives.

    Args:
        path: Path to the SSH config file to parse.
        hosts: Accumulator set to add discovered hostnames into.
    """
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Reason: SSH config keywords are case-insensitive.
        lower = stripped.lower()

        if lower.startswith("host "):
            # Extract everything after "Host " and split on whitespace
            # to handle multi-host lines like "Host foo bar baz".
            entries = stripped.split()[1:]
            for entry in entries:
                # Filter out wildcard patterns.
                if any(c in entry for c in ("*", "?", "!")):
                    continue
                hosts.add(entry)

        elif lower.startswith("include "):
            # Expand ~ and globs in Include directives.
            pattern = stripped.split(None, 1)[1]
            expanded = str(Path(pattern).expanduser())
            for match in globmod.glob(expanded):
                _parse_ssh_config_file(Path(match), hosts)


def discover_hosts(config: FleetConfig, ssh_config_path: Path | None = None) -> list[str]:
    """Discover SSH hosts not yet in the fleet.

    Parses ~/.ssh/config for all Host entries, then subtracts hosts already
    present in the fleet config and the special "local" entry.

    Args:
        config: Fleet configuration with current node list.
        ssh_config_path: Path to SSH config file. Defaults to ~/.ssh/config.

    Returns:
        list[str]: Sorted list of hostnames available to add.
    """
    all_hosts = parse_ssh_config_hosts(ssh_config_path)
    existing = set(config.nodes) | {"local"}
    return [h for h in all_hosts if h not in existing]


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


async def nodes_add(
    host: str,
    config: FleetConfig,
    ssh_config_path: Path | None = None,
    fleet_config_path: Path | None = None,
) -> list[str]:
    """Add a new node to the fleet.

    Verifies remote tmux version >= 3.0, creates SSH socket directory,
    pushes canonical tmux.conf, appends SSH config block, and persists
    the host to the fleet config.

    Args:
        host: Hostname to add.
        config: Fleet configuration.
        ssh_config_path: Path to SSH config file. Defaults to ~/.ssh/nexus_config.
        fleet_config_path: Path to fleet config file. Defaults to ~/.config/nexus/fleet.toml.

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

    # Step 5: Add host to fleet config and persist.
    if host not in config.nodes:
        config.nodes.append(host)
        save_config(config, fleet_config_path)
        log.append(f"Added {host} to fleet config")

    return log


def nodes_rm(
    host: str,
    config: FleetConfig,
    ssh_config_path: Path | None = None,
    fleet_config_path: Path | None = None,
) -> list[str]:
    """Remove a node from the fleet and its SSH config block.

    Args:
        host: Hostname to remove.
        config: Fleet configuration.
        ssh_config_path: Path to SSH config file. Defaults to ~/.ssh/nexus_config.
        fleet_config_path: Path to fleet config file. Defaults to ~/.config/nexus/fleet.toml.

    Returns:
        list[str]: Log messages describing actions taken.

    Raises:
        ValueError: If the host is not found in SSH config or fleet config.
    """
    config_path = ssh_config_path or NEXUS_SSH_CONFIG
    log: list[str] = []

    # Remove SSH config block if present.
    if config_path.exists():
        content = config_path.read_text()
        if f"Host {host}" in content:
            pattern = rf"\nHost {re.escape(host)}\n(?:    [^\n]*\n)*"
            new_content = re.sub(pattern, "", content)
            config_path.write_text(new_content)
            log.append(f"Removed SSH config for {host}")

    # Remove host from fleet config and persist.
    if host in config.nodes:
        config.nodes.remove(host)
        save_config(config, fleet_config_path)
        log.append(f"Removed {host} from fleet config")

    if not log:
        raise ValueError(f"Host '{host}' not found in SSH config or fleet config.")

    return log
