"""Tests for the Milestone 12: Snapshot & Restore feature.

Covers save_snapshot, restore_snapshot, and the corresponding CLI commands
(nx snapshot, nx restore). Tests mock asyncio.create_subprocess_exec to
control what fan_out and run_on_node return without real tmux or SSH.

The Typer callback's load_config is monkeypatched to inject a controlled
FleetConfig. For direct function tests, save_snapshot and restore_snapshot
accept a snapshot_path parameter to use a temp file instead of the default.
"""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from nx.cli import app
from nx.config import FleetConfig
from nx.snapshot import FleetSnapshot, SessionSnapshot, save_snapshot, restore_snapshot


runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProcess:
    """Fake asyncio subprocess that returns predetermined output.

    Attributes:
        stdout: Bytes to return as stdout.
        stderr: Bytes to return as stderr.
        returncode: Exit code to return.
    """

    def __init__(
        self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        """Return stored stdout and stderr."""
        return self.stdout, self.stderr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_snapshot_saves_state(monkeypatch, tmp_path):
    """save_snapshot captures all sessions from all nodes to a JSON file.

    Scenario:
        - Config: nodes=["local", "dev-server"]
        - local returns 2 sessions: api and worker
        - dev-server returns 1 session: pipeline
    Expected:
        - Snapshot file is created at the given path.
        - File contains 3 sessions total.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    local_output = (
        b"api|1|0|/home/u|bash|1234|0|\n"
        b"worker|1|0|/app|node|5678|0|\n"
    )
    remote_output = b"pipeline|1|0|/data|python|9012|0|\n"

    async def fake_exec(*args, **kwargs):
        """Return different sessions based on whether this is local or SSH."""
        if args[0] == "tmux":
            return FakeProcess(stdout=local_output, returncode=0)
        elif "dev-server" in args:
            return FakeProcess(stdout=remote_output, returncode=0)
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    snapshot_file = tmp_path / "snapshot.json"
    path = asyncio.run(save_snapshot(config, snapshot_path=snapshot_file))

    # Verify file was created.
    assert path.exists()
    assert path == snapshot_file

    # Verify it contains 3 sessions.
    data = json.loads(path.read_text())
    assert len(data["sessions"]) == 3


def test_snapshot_schema_valid(monkeypatch, tmp_path):
    """Snapshot output validates against the FleetSnapshot pydantic model.

    Scenario:
        - Same setup as test_snapshot_saves_state.
    Expected:
        - JSON file round-trips through FleetSnapshot(**data) without error.
        - timestamp field is present and non-empty.
        - Each session has correct fields: node, name, directory, command.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    local_output = (
        b"api|1|0|/home/u|bash|1234|0|\n"
        b"worker|1|0|/app|node|5678|0|\n"
    )
    remote_output = b"pipeline|1|0|/data|python|9012|0|\n"

    async def fake_exec(*args, **kwargs):
        """Return different sessions based on whether this is local or SSH."""
        if args[0] == "tmux":
            return FakeProcess(stdout=local_output, returncode=0)
        elif "dev-server" in args:
            return FakeProcess(stdout=remote_output, returncode=0)
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    snapshot_file = tmp_path / "snapshot.json"
    asyncio.run(save_snapshot(config, snapshot_path=snapshot_file))

    # Read back and validate with pydantic.
    data = json.loads(snapshot_file.read_text())
    fleet = FleetSnapshot(**data)

    # Verify timestamp is present.
    assert fleet.timestamp is not None

    # Verify session fields.
    assert len(fleet.sessions) == 3
    session_names = {s.name for s in fleet.sessions}
    assert session_names == {"api", "worker", "pipeline"}

    # Verify each session has the expected fields populated.
    for session in fleet.sessions:
        assert session.node in ("local", "dev-server")
        assert session.name
        assert session.directory
        assert session.command


def test_restore_creates_sessions(monkeypatch, tmp_path):
    """restore_snapshot reads JSON and creates sessions via new-session calls.

    Scenario:
        - Write a valid snapshot JSON with 2 sessions.
        - Mock create_subprocess_exec to return success for new-session calls.
    Expected:
        - new-session calls were made (captured args contain "new-session").
        - Log messages contain "OK" for each session.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    # Write a snapshot file.
    snapshot_data = FleetSnapshot(
        timestamp="2026-02-24T12:00:00Z",
        sessions=[
            SessionSnapshot(node="local", name="api", directory="/home/u", command="bash"),
            SessionSnapshot(node="dev-server", name="pipeline", directory="/data", command="python"),
        ],
    )
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(snapshot_data.model_dump_json(indent=2))

    # Track all calls to create_subprocess_exec.
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        """Record calls and return success."""
        calls.append(args)
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    log = asyncio.run(restore_snapshot(config, snapshot_path=snapshot_file))

    # Verify new-session calls were made.
    assert len(calls) == 2
    for call_args in calls:
        # Reason: Each call should contain "new-session" either directly
        # (local) or within the shlex-joined SSH arg (remote).
        joined = " ".join(str(a) for a in call_args)
        assert "new-session" in joined

    # Verify log messages contain "OK".
    assert len(log) == 2
    for msg in log:
        assert "OK" in msg


def test_restore_specific_node(monkeypatch, tmp_path):
    """restore_snapshot with node_filter only restores matching sessions.

    Scenario:
        - Snapshot contains sessions on "local" and "dev-server".
        - Call restore_snapshot with node_filter="dev-server".
    Expected:
        - Only 1 new-session call (for dev-server's session).
        - Log has exactly 1 entry.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    snapshot_data = FleetSnapshot(
        timestamp="2026-02-24T12:00:00Z",
        sessions=[
            SessionSnapshot(node="local", name="api", directory="/home/u", command="bash"),
            SessionSnapshot(node="local", name="worker", directory="/app", command="node"),
            SessionSnapshot(node="dev-server", name="pipeline", directory="/data", command="python"),
        ],
    )
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(snapshot_data.model_dump_json(indent=2))

    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        """Record calls and return success."""
        calls.append(args)
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    log = asyncio.run(
        restore_snapshot(config, node_filter="dev-server", snapshot_path=snapshot_file)
    )

    # Reason: Only the dev-server session should be restored.
    assert len(calls) == 1
    assert len(log) == 1
    assert "dev-server/pipeline" in log[0]
    assert "OK" in log[0]


def test_restore_logs_output(monkeypatch):
    """CLI 'nx restore' prints each log message and a summary line.

    Scenario:
        - Monkeypatch restore_snapshot to return a list of log strings.
    Expected:
        - Each log message appears in the CLI output.
        - "Restored {count} sessions" appears in output.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    fake_logs = [
        "Restoring local/api... OK",
        "Restoring local/worker... OK",
        "Restoring dev-server/pipeline... OK",
    ]

    async def fake_restore(cfg, node_filter=None, snapshot_path=None):
        """Return predetermined log messages."""
        return fake_logs

    monkeypatch.setattr("nx.cli.restore_snapshot", fake_restore)

    result = runner.invoke(app, ["restore"])

    assert result.exit_code == 0

    # Reason: Rich console.print injects ANSI escape codes around certain
    # tokens (e.g. "..." becomes styled). Assert on fragments that bracket
    # the styled portions instead of exact string matching.
    assert "Restoring local/api" in result.output
    assert "Restoring local/worker" in result.output
    assert "Restoring dev-server/pipeline" in result.output
    assert "OK" in result.output

    # Verify summary line (count may be styled by Rich).
    assert "Restored" in result.output
    assert "3" in result.output
    assert "sessions" in result.output


def test_restore_empty_snapshot(monkeypatch):
    """CLI 'nx restore' with no sessions prints 'No sessions to restore.'

    Scenario:
        - Monkeypatch restore_snapshot to return an empty list
          (simulates missing or empty snapshot file).
    Expected:
        - Exit code 0.
        - Output contains "No sessions to restore".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_restore(cfg, node_filter=None, snapshot_path=None):
        """Return empty list (no sessions to restore)."""
        return []

    monkeypatch.setattr("nx.cli.restore_snapshot", fake_restore)

    result = runner.invoke(app, ["restore"])

    assert result.exit_code == 0
    assert "No sessions to restore" in result.output
