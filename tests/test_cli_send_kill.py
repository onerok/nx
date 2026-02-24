"""Tests for the `nx send` and `nx kill` CLI commands.

Covers Milestone 8: `nx send` & `nx kill` — Interaction.

Tests mock both resolve_session (async) and asyncio.create_subprocess_exec
so that the full resolve -> build tmux cmd -> execute on node -> display output
pipeline is verified without real tmux or SSH processes.

Send injects keystrokes with auto-Enter by default, or verbatim in --raw mode.
Kill terminates sessions via tmux kill-session on the resolved node.
"""

import asyncio

from typer.testing import CliRunner

from nx.cli import app
from nx.config import FleetConfig
from nx.resolve import SessionNotFound

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


def _make_fake_exec(calls: list, process: FakeProcess | None = None):
    """Create a fake create_subprocess_exec that records calls.

    Args:
        calls: List to append captured positional args to.
        process: FakeProcess to return. Defaults to a successful no-op process.

    Returns:
        Async callable matching the asyncio.create_subprocess_exec signature.
    """
    if process is None:
        process = FakeProcess(stdout=b"", returncode=0)

    async def fake_exec(*args, **kwargs):
        """Record args and return the fake process."""
        calls.append(args)
        return process

    return fake_exec


# ---------------------------------------------------------------------------
# Tests — nx send
# ---------------------------------------------------------------------------


def test_send_auto_enter(monkeypatch):
    """Send appends Enter by default after the last key argument.

    Scenario:
        - Config: local node only.
        - resolve_session returns ("local", "api").
        - Invoke: ["send", "local/api", "hello"]
    Expected:
        - exit_code == 0
        - Output contains "Sent to local/api".
        - exec args contain "send-keys", "-t", "api", "hello", "Enter".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    calls: list[tuple] = []
    process = FakeProcess(stdout=b"", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["send", "local/api", "hello"])

    assert result.exit_code == 0
    assert "Sent to local/api" in result.output

    # Verify the tmux send-keys command was built correctly.
    assert len(calls) == 1
    args = calls[0]
    assert "send-keys" in args
    assert "-t" in args
    t_index = args.index("-t")
    assert args[t_index + 1] == "api"
    assert "hello" in args
    # Reason: Default mode appends Enter after the last key.
    assert args[-1] == "Enter"


def test_send_raw_mode(monkeypatch):
    """Raw mode sends keys verbatim without appending Enter.

    Scenario:
        - Config: local node only.
        - resolve_session returns ("local", "api").
        - Invoke: ["send", "--raw", "local/api", "C-c"]
    Expected:
        - exit_code == 0
        - Output contains "Sent to local/api".
        - exec args contain "send-keys", "-t", "api", "C-c".
        - "Enter" is NOT in exec args (raw mode skips it).
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    calls: list[tuple] = []
    process = FakeProcess(stdout=b"", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["send", "--raw", "local/api", "C-c"])

    assert result.exit_code == 0
    assert "Sent to local/api" in result.output

    # Verify the tmux send-keys command was built correctly.
    assert len(calls) == 1
    args = calls[0]
    assert "send-keys" in args
    assert "-t" in args
    t_index = args.index("-t")
    assert args[t_index + 1] == "api"
    assert "C-c" in args
    # Reason: Raw mode must NOT append Enter after the keys.
    assert "Enter" not in args


def test_send_multiple_args(monkeypatch):
    """Multiple key arguments are all included, with Enter after the last.

    Scenario:
        - Config: local node only.
        - resolve_session returns ("local", "api").
        - Invoke: ["send", "local/api", "cd /app", "npm start"]
    Expected:
        - exit_code == 0
        - exec args contain both "cd /app" and "npm start".
        - exec args end with "Enter" (auto-Enter after last key).
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    calls: list[tuple] = []
    process = FakeProcess(stdout=b"", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["send", "local/api", "cd /app", "npm start"])

    assert result.exit_code == 0

    # Verify the tmux send-keys command includes both key arguments.
    assert len(calls) == 1
    args = calls[0]
    assert "cd /app" in args
    assert "npm start" in args
    # Reason: Default mode appends Enter after the last key.
    assert args[-1] == "Enter"


def test_send_uses_resolution(monkeypatch):
    """Bare name triggers resolution; remote target wraps in SSH.

    Scenario:
        - Config: local + dev-server nodes.
        - resolve_session returns ("dev-server", "api") for a bare name.
        - Invoke: ["send", "api", "hello"] (bare name, not fully qualified).
    Expected:
        - exit_code == 0
        - exec args start with "ssh" and contain "dev-server"
          (remote execution via SSH).
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Return a remote node/session pair."""
        return ("dev-server", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    calls: list[tuple] = []
    process = FakeProcess(stdout=b"", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["send", "api", "hello"])

    assert result.exit_code == 0

    # Reason: Remote execution wraps the tmux command in an SSH call.
    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "ssh"
    assert "dev-server" in args


# ---------------------------------------------------------------------------
# Tests — nx kill
# ---------------------------------------------------------------------------


def test_kill_session(monkeypatch):
    """Kill sends tmux kill-session to the resolved node.

    Scenario:
        - Config: local node only.
        - resolve_session returns ("local", "api").
        - Invoke: ["kill", "local/api"]
    Expected:
        - exit_code == 0
        - Output contains "Killed session local/api".
        - exec args contain "kill-session", "-t", "api".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    calls: list[tuple] = []
    process = FakeProcess(stdout=b"", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["kill", "local/api"])

    assert result.exit_code == 0
    assert "Killed session local/api" in result.output

    # Verify the tmux kill-session command was built correctly.
    assert len(calls) == 1
    args = calls[0]
    assert "kill-session" in args
    assert "-t" in args
    t_index = args.index("-t")
    assert args[t_index + 1] == "api"


def test_kill_nonexistent(monkeypatch):
    """Kill on a nonexistent session prints an error and exits with code 1.

    Scenario:
        - Config: local node only.
        - resolve_session raises SessionNotFound.
        - Invoke: ["kill", "ghost"]
    Expected:
        - exit_code == 1
        - Output contains "Session" and "not found".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Raise SessionNotFound for any name."""
        raise SessionNotFound(f"Session '{name}' not found on any node.")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    result = runner.invoke(app, ["kill", "ghost"])

    assert result.exit_code == 1
    assert "Session" in result.output
    assert "not found" in result.output
