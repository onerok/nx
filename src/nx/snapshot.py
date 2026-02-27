"""Snapshot and restore fleet state."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from nx.config import FleetConfig
from nx.ssh import fan_out, run_on_node
from nx.tmux import build_list_cmd, build_new_cmd, parse_list_output


SNAPSHOT_PATH = Path.home() / ".config" / "nexus" / "snapshot.json"


class SessionSnapshot(BaseModel):
    """A single session's state for snapshot/restore.

    Attributes:
        node: Node hostname.
        name: Session name.
        directory: Working directory.
        command: Command running in the session.
    """

    node: str
    name: str
    directory: str
    command: str


class FleetSnapshot(BaseModel):
    """Full fleet state snapshot.

    Attributes:
        timestamp: When the snapshot was taken.
        sessions: List of session snapshots.
    """

    timestamp: datetime
    sessions: list[SessionSnapshot]


async def save_snapshot(config: FleetConfig, snapshot_path: Path | None = None) -> Path:
    """Save fleet state to a JSON snapshot file.

    Fan-outs a list-sessions query to all nodes, collects running sessions,
    and serializes them to JSON.

    Args:
        config: Fleet configuration.
        snapshot_path: Path to write snapshot. Defaults to ~/.config/nexus/snapshot.json.

    Returns:
        Path: The path where the snapshot was written.
    """
    path = snapshot_path or SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    results = await fan_out(
        config.nodes, build_list_cmd(), max_concurrent=config.max_concurrent_ssh
    )

    sessions: list[SessionSnapshot] = []
    for node in config.nodes:
        result = results[node]
        if result.returncode != 0:
            continue
        for info in parse_list_output(result.stdout):
            sessions.append(
                SessionSnapshot(
                    node=node,
                    name=info.name,
                    directory=info.pane_path,
                    command=info.pane_cmd,
                )
            )

    snapshot = FleetSnapshot(
        timestamp=datetime.now(timezone.utc),
        sessions=sessions,
    )

    path.write_text(snapshot.model_dump_json(indent=2))
    return path


async def restore_snapshot(
    config: FleetConfig,
    node_filter: str | None = None,
    snapshot_path: Path | None = None,
) -> list[str]:
    """Restore fleet state from a JSON snapshot file.

    Reads the snapshot and creates sessions via run_on_node + build_new_cmd.

    Args:
        config: Fleet configuration.
        node_filter: If set, only restore sessions on this node.
        snapshot_path: Path to read snapshot from. Defaults to ~/.config/nexus/snapshot.json.

    Returns:
        list[str]: Log messages for each restored session.
    """
    path = snapshot_path or SNAPSHOT_PATH
    log: list[str] = []

    if not path.exists():
        return log

    data = json.loads(path.read_text())
    snapshot = FleetSnapshot(**data)

    for session in snapshot.sessions:
        if node_filter and session.node != node_filter:
            continue

        cmd = build_new_cmd(session.name, cmd=session.command, directory=session.directory)
        result = await run_on_node(session.node, cmd)

        if result.returncode == 0:
            log.append(f"Restoring {session.node}/{session.name}... OK")
        else:
            log.append(f"Restoring {session.node}/{session.name}... FAILED: {result.stderr}")

    return log
