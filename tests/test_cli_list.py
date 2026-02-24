"""Tests for the `nx list` CLI command.

Covers Milestone 3: `nx list` — First End-to-End Command.

Tests mock asyncio.create_subprocess_exec so that fan_out returns
controlled results without running real tmux or SSH processes.
The Typer callback's load_config is also monkeypatched to inject
a controlled FleetConfig.
"""

import asyncio

import pytest
from typer.testing import CliRunner

from nx.cli import app
from nx.config import FleetConfig

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


def test_list_empty_fleet(monkeypatch):
    """No sessions on any node prints 'No active sessions.' message.

    Scenario:
        - Single node "local" with no tmux sessions (empty stdout).
    Expected:
        - Exit code 0.
        - Output contains "No active sessions".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_exec(*args, **kwargs):
        """Return empty stdout for local tmux list-sessions."""
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "No active sessions" in result.output


def test_list_single_node(monkeypatch):
    """Single node with two sessions renders a table with both sessions.

    Scenario:
        - Single node "local" with two sessions: api (python) and worker (celery).
    Expected:
        - Exit code 0.
        - Output contains session names, commands, and [RUNNING] status.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    tmux_output = (
        b"api|1|0|/home/u/app|python|1234|0|\n"
        b"worker|2|0|/home/u/app|celery|5678|0|\n"
    )

    async def fake_exec(*args, **kwargs):
        """Return two sessions for local node."""
        return FakeProcess(stdout=tmux_output, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "api" in result.output
    assert "worker" in result.output
    assert "python" in result.output
    assert "celery" in result.output
    assert "[RUNNING]" in result.output
    # Reason: Table columns should be present in the Rich table output.
    assert "Node" in result.output
    assert "Session" in result.output


def test_list_multi_node(monkeypatch):
    """Multiple nodes each with sessions renders all nodes and sessions.

    Scenario:
        - Three nodes: local, dev-server, gpu-rig.
        - Each has one session with a unique name.
    Expected:
        - Exit code 0.
        - Output contains all three node names and session names.
    """
    config = FleetConfig(
        nodes=["local", "dev-server", "gpu-rig"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_exec(*args, **kwargs):
        """Return different sessions based on whether this is local or SSH.

        Local calls: args[0] == "tmux"
        SSH calls: args[0] == "ssh", node name appears in args.
        """
        if args[0] == "tmux":
            # Local node
            return FakeProcess(
                stdout=b"web|1|0|/home/u/web|node|1001|0|\n",
                returncode=0,
            )
        elif "dev-server" in args:
            return FakeProcess(
                stdout=b"api|1|0|/home/u/api|python|2001|0|\n",
                returncode=0,
            )
        elif "gpu-rig" in args:
            return FakeProcess(
                stdout=b"train|1|0|/home/u/ml|python|3001|0|\n",
                returncode=0,
            )
        # Fallback
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    # Reason: All three node names should appear in the table.
    assert "local" in result.output
    assert "dev-server" in result.output
    assert "gpu-rig" in result.output
    # Reason: Each node's session name should appear.
    assert "web" in result.output
    assert "api" in result.output
    assert "train" in result.output


def test_list_unreachable_node(monkeypatch):
    """Unreachable node shows [UNREACHABLE] in the table.

    Scenario:
        - Two nodes: local (reachable, one session) and bad-node (unreachable).
        - bad-node's SSH command returns returncode=1.
    Expected:
        - Exit code 0.
        - Output contains [UNREACHABLE] and "bad-node".
        - Local session still shows normally.
    """
    config = FleetConfig(
        nodes=["local", "bad-node"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_exec(*args, **kwargs):
        """Return success for local, failure for bad-node (SSH)."""
        if args[0] == "tmux":
            # Local node — one running session
            return FakeProcess(
                stdout=b"api|1|0|/home/u/app|python|1234|0|\n",
                returncode=0,
            )
        else:
            # SSH call to bad-node — connection failure
            return FakeProcess(
                stdout=b"",
                stderr=b"ssh: connect to host bad-node port 22: Connection refused",
                returncode=1,
            )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "[UNREACHABLE]" in result.output
    assert "bad-node" in result.output
    # Reason: The reachable node's session should still render normally.
    assert "api" in result.output
    assert "[RUNNING]" in result.output


def test_list_shows_status(monkeypatch):
    """Sessions display correct status: [RUNNING], [EXITED 0], [EXITED 1].

    Scenario:
        - Single node "local" with three sessions:
            1. running — alive (pane_dead=0)
            2. exited_ok — dead with exit code 0 (pane_dead=1, pane_dead_status=0)
            3. exited_bad — dead with exit code 1 (pane_dead=1, pane_dead_status=1)
    Expected:
        - Exit code 0.
        - Output contains [RUNNING], [EXITED 0], and [EXITED 1].
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    tmux_output = (
        b"running|1|0|/home/u|bash|1234|0|\n"
        b"exited_ok|1|0|/home/u|python|2345|1|0\n"
        b"exited_bad|1|0|/home/u|node|3456|1|1\n"
    )

    async def fake_exec(*args, **kwargs):
        """Return three sessions with different statuses."""
        return FakeProcess(stdout=tmux_output, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "[RUNNING]" in result.output
    assert "[EXITED 0]" in result.output
    assert "[EXITED 1]" in result.output
    # Reason: All three session names should be in the output.
    assert "running" in result.output
    assert "exited_ok" in result.output
    assert "exited_bad" in result.output
