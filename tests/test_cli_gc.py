"""Tests for the `nx gc` CLI command.

Covers Milestone 9: `nx gc` — Garbage Collection.

Tests mock asyncio.create_subprocess_exec to control what fan_out and
run_on_node return. The gc command fans out a list-sessions query to all
nodes, filters for exited (is_dead=True) sessions, optionally confirms
with the user, and kills each exited session.

Unlike send/kill tests that mock resolve_session, gc uses fan_out directly,
so the subprocess mock must handle both list-sessions and kill-session calls.
"""

import asyncio
import sys
from types import SimpleNamespace

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


class _FakeSys:
    """Proxy for the sys module with controllable stdin.isatty().

    The gc command checks sys.stdin.isatty() to decide whether to prompt
    for confirmation. We replace the entire sys reference in nx.cli with
    this proxy so that stdin.isatty() returns the configured value while
    all other sys attributes are forwarded to the real sys module.

    Args:
        tty: Whether stdin.isatty() should return True.
    """

    def __init__(self, tty: bool = True):
        self._tty = tty

    @property
    def stdin(self):
        """Return a namespace whose isatty() returns the configured value."""
        return SimpleNamespace(isatty=lambda: self._tty)

    def __getattr__(self, name):
        """Forward all other attribute access to the real sys module."""
        return getattr(sys, name)


def _make_fake_exec_for_gc(
    calls: list,
    node_list_outputs: dict[str, bytes],
):
    """Create a fake create_subprocess_exec for gc tests.

    The gc command issues two kinds of subprocess calls:
    1. list-sessions (via fan_out) — returns pipe-delimited session info.
    2. kill-session (for each exited session) — returns empty success.

    This factory returns different FakeProcess results depending on whether
    the command contains "list-sessions" (list call) or not (kill call).
    For list calls on remote nodes, the node name appears in the SSH args
    and is used to select the correct output.

    Args:
        calls: List to append captured positional args to.
        node_list_outputs: Mapping of node name to raw tmux list bytes.
            Use "local" for local node output.

    Returns:
        Async callable matching the asyncio.create_subprocess_exec signature.
    """

    async def fake_exec(*args, **kwargs):
        """Route to list or kill response based on command args."""
        calls.append(args)

        # Determine if this is a list-sessions call by checking the args.
        # For local: args = ("tmux", "-L", "nexus", "list-sessions", ...)
        # For remote: args = ("ssh", "-o", "ConnectTimeout=2", "<node>", "tmux -L nexus list-sessions ...")
        is_list = any("list-sessions" in str(a) for a in args)

        if is_list:
            # Determine which node this call is for.
            if args[0] == "tmux":
                # Local call
                output = node_list_outputs.get("local", b"")
            else:
                # SSH call — node name is args[3] (after ssh, -o, ConnectTimeout=2)
                node_name = args[3]
                output = node_list_outputs.get(node_name, b"")
            return FakeProcess(stdout=output, returncode=0)
        else:
            # Kill call — return empty success.
            return FakeProcess(stdout=b"", returncode=0)

    return fake_exec


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_gc_fleet_wide(monkeypatch):
    """Fleet-wide gc reaps exited sessions on multiple nodes.

    Scenario:
        - Config: nodes=["local", "dev-server"].
        - local returns one exited session (old-api, exit code 0).
        - dev-server returns one exited session (crashed, exit code 1).
        - sys.stdin.isatty() returns False (piped, no confirmation needed).
        - Invoke: ["gc"]
    Expected:
        - exit_code == 0
        - Output contains "Reaped local/old-api" and "Reaped dev-server/crashed".
        - exec calls include kill-session for both sessions.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli.sys", _FakeSys(tty=False))

    calls: list[tuple] = []
    node_outputs = {
        "local": b"old-api|1|0|/home/u|bash|1234|1|0\n",
        "dev-server": b"crashed|1|0|/app|python|5678|1|1\n",
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_gc(calls, node_outputs),
    )

    result = runner.invoke(app, ["gc"])

    assert result.exit_code == 0
    assert "Reaped local/old-api" in result.output
    assert "Reaped dev-server/crashed" in result.output

    # Reason: Two list-sessions calls (fan_out) + two kill-session calls.
    kill_calls = [c for c in calls if any("kill-session" in str(a) for a in c)]
    assert len(kill_calls) == 2


def test_gc_by_name(monkeypatch):
    """Filtering by name reaps only the matching exited session.

    Scenario:
        - Config: nodes=["local"].
        - local returns TWO exited sessions: old-api and crashed.
        - sys.stdin.isatty() returns False (piped, no confirmation needed).
        - Invoke: ["gc", "old-api"] (only reap old-api).
    Expected:
        - exit_code == 0
        - Output contains "Reaped local/old-api".
        - Output does NOT contain "crashed".
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli.sys", _FakeSys(tty=False))

    calls: list[tuple] = []
    node_outputs = {
        "local": (
            b"old-api|1|0|/home/u|bash|1234|1|0\n"
            b"crashed|1|0|/app|python|5678|1|1\n"
        ),
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_gc(calls, node_outputs),
    )

    result = runner.invoke(app, ["gc", "old-api"])

    assert result.exit_code == 0
    assert "Reaped local/old-api" in result.output
    assert "crashed" not in result.output

    # Reason: Only one kill-session call should be made (for old-api only).
    kill_calls = [c for c in calls if any("kill-session" in str(a) for a in c)]
    assert len(kill_calls) == 1


def test_gc_running_session_skipped(monkeypatch):
    """Running sessions are not reaped — gc only targets exited sessions.

    Scenario:
        - Config: nodes=["local"].
        - local returns one RUNNING session (api, pane_dead=0).
        - Invoke: ["gc"]
    Expected:
        - exit_code == 0
        - Output contains "No exited sessions".
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    node_outputs = {
        "local": b"api|1|0|/home/u|bash|1234|0|\n",
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_gc(calls, node_outputs),
    )

    result = runner.invoke(app, ["gc"])

    assert result.exit_code == 0
    assert "No exited sessions" in result.output


def test_gc_dry_run(monkeypatch):
    """Dry-run mode lists exited sessions but does not kill them.

    Scenario:
        - Config: nodes=["local"].
        - local returns one exited session (old-api, exit code 0).
        - Invoke: ["gc", "--dry-run"]
    Expected:
        - exit_code == 0
        - Output contains "Would reap" and "old-api".
        - NO kill-session calls were made (only the list fan-out call).
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    node_outputs = {
        "local": b"old-api|1|0|/home/u|bash|1234|1|0\n",
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_gc(calls, node_outputs),
    )

    result = runner.invoke(app, ["gc", "--dry-run"])

    assert result.exit_code == 0
    assert "Would reap" in result.output
    assert "old-api" in result.output

    # Reason: Dry-run should NOT issue any kill-session calls.
    kill_calls = [c for c in calls if any("kill-session" in str(a) for a in c)]
    assert len(kill_calls) == 0


def test_gc_interactive_confirmation(monkeypatch):
    """Interactive stdin prompts for confirmation before reaping.

    Scenario:
        - Config: nodes=["local"].
        - local returns one exited session (old-api, exit code 0).
        - sys.stdin.isatty() returns True (interactive terminal).
        - typer.confirm is mocked to NOT raise (user says yes).
        - Invoke: ["gc"]
    Expected:
        - exit_code == 0
        - Output contains "Reaped".
        - typer.confirm was called.
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli.sys", _FakeSys(tty=True))

    calls: list[tuple] = []
    node_outputs = {
        "local": b"old-api|1|0|/home/u|bash|1234|1|0\n",
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_gc(calls, node_outputs),
    )

    # Reason: Track whether typer.confirm was invoked, and allow it to
    # pass (user confirms "yes") by not raising an exception.
    confirm_called = []

    def fake_confirm(message, abort=False):
        """Record the call without raising (user says yes)."""
        confirm_called.append(message)

    monkeypatch.setattr("nx.cli.typer.confirm", fake_confirm)

    result = runner.invoke(app, ["gc"])

    assert result.exit_code == 0
    assert "Reaped" in result.output
    # Reason: Interactive mode must prompt the user before reaping.
    assert len(confirm_called) == 1


def test_gc_piped_no_confirmation(monkeypatch):
    """Piped stdin auto-proceeds without prompting for confirmation.

    Scenario:
        - Config: nodes=["local"].
        - local returns one exited session (old-api, exit code 0).
        - sys.stdin.isatty() returns False (piped, non-interactive).
        - Invoke: ["gc"]
    Expected:
        - exit_code == 0
        - Output contains "Reaped".
        - typer.confirm was NOT called.
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli.sys", _FakeSys(tty=False))

    calls: list[tuple] = []
    node_outputs = {
        "local": b"old-api|1|0|/home/u|bash|1234|1|0\n",
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_gc(calls, node_outputs),
    )

    # Reason: Track whether typer.confirm was invoked — it should NOT be.
    confirm_called = []

    def fake_confirm(message, abort=False):
        """Record the call (should not be reached in piped mode)."""
        confirm_called.append(message)

    monkeypatch.setattr("nx.cli.typer.confirm", fake_confirm)

    result = runner.invoke(app, ["gc"])

    assert result.exit_code == 0
    assert "Reaped" in result.output
    # Reason: Piped mode must NOT prompt the user.
    assert len(confirm_called) == 0
