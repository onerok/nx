"""Tests for the `nx new` CLI command.

Covers Milestone 4: `nx new` — Session Creation.

Tests mock asyncio.create_subprocess_exec so that run_on_node returns
controlled results without running real tmux or SSH processes.
The Typer callback's load_config is also monkeypatched to inject
a controlled FleetConfig.

Each test captures the args passed to create_subprocess_exec to verify
that the correct tmux (and optionally SSH) command is constructed.
"""

import asyncio
import os

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
# Tests
# ---------------------------------------------------------------------------


def test_new_local_default(monkeypatch):
    """Local session with no flags uses default_node, cwd, and default_cmd.

    Scenario:
        - Config: default_node="local", default_cmd="/bin/bash"
        - Invoke: ["new", "api"] (no --on, no --dir, no cmd)
    Expected:
        - exit_code == 0
        - Output contains "Created session local/api"
        - Exec args contain: tmux -L nexus new-session -d -s api
        - Exec args contain: -c <cwd> (local default dir)
        - Exec args contain: /bin/bash (from default_cmd)
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    result = runner.invoke(app, ["new", "api"])

    assert result.exit_code == 0
    assert "Created session local/api" in result.output

    # Verify exactly one subprocess call was made.
    assert len(calls) == 1
    args = calls[0]

    # Reason: Local execution runs tmux directly (not via SSH).
    assert args[0] == "tmux"
    assert "-L" in args
    assert "nexus" in args
    assert "new-session" in args
    assert "-d" in args
    assert "-s" in args
    assert "api" in args

    # Reason: Local sessions default to the caller's cwd.
    assert "-c" in args
    cwd_index = args.index("-c") + 1
    assert args[cwd_index] == os.getcwd()

    # Reason: No cmd argument means default_cmd="/bin/bash" is used.
    assert "/bin/bash" in args


def test_new_remote(monkeypatch):
    """Remote session via --on sends the command through SSH.

    Scenario:
        - Config: nodes=["local", "dev-server"], default_node="local"
        - Invoke: ["new", "--on", "dev-server", "api"]
    Expected:
        - Output contains "Created session dev-server/api"
        - Exec call starts with "ssh" (remote execution)
        - SSH args contain "dev-server"
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    result = runner.invoke(app, ["new", "--on", "dev-server", "api"])

    assert result.exit_code == 0
    assert "Created session dev-server/api" in result.output

    assert len(calls) == 1
    args = calls[0]

    # Reason: Remote execution wraps the tmux command in an SSH call.
    assert args[0] == "ssh"
    assert "dev-server" in args

    # Reason: The last SSH arg is the shlex-joined tmux command.
    # It should contain the tmux new-session invocation as a single string.
    joined_cmd = args[-1]
    assert "tmux" in joined_cmd
    assert "new-session" in joined_cmd
    assert "api" in joined_cmd


def test_new_with_dir(monkeypatch):
    """Explicit --dir sets the working directory for the session.

    Scenario:
        - Config: default_node="local"
        - Invoke: ["new", "--dir", "/tmp", "api"]
    Expected:
        - Exec call args contain "-c" and "/tmp".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    result = runner.invoke(app, ["new", "--dir", "/tmp", "api"])

    assert result.exit_code == 0

    assert len(calls) == 1
    args = calls[0]

    # Reason: --dir /tmp should translate to -c /tmp in the tmux command.
    assert "-c" in args
    cwd_index = args.index("-c") + 1
    assert args[cwd_index] == "/tmp"


def test_new_default_dir_local(monkeypatch):
    """Local session without --dir defaults to os.getcwd().

    Scenario:
        - Config: default_node="local"
        - Invoke: ["new", "api"] (no --dir)
        - Monkeypatch os.getcwd to return "/home/test/projects"
    Expected:
        - Exec call args contain "-c" and "/home/test/projects".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr(os, "getcwd", lambda: "/home/test/projects")

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    result = runner.invoke(app, ["new", "api"])

    assert result.exit_code == 0

    assert len(calls) == 1
    args = calls[0]

    # Reason: Local sessions with no --dir should use the monkeypatched cwd.
    assert "-c" in args
    cwd_index = args.index("-c") + 1
    assert args[cwd_index] == "/home/test/projects"


def test_new_default_dir_remote(monkeypatch):
    """Remote session without --dir omits the -c flag (tmux defaults to $HOME).

    Scenario:
        - Config: default_node="local"
        - Invoke: ["new", "--on", "dev-server", "api"] (no --dir)
    Expected:
        - The tmux command (embedded in the SSH call) does NOT contain "-c".
        - Reason: On remote nodes, omitting -c lets tmux use the remote
          user's $HOME, since the local cwd is unlikely to exist remotely.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    result = runner.invoke(app, ["new", "--on", "dev-server", "api"])

    assert result.exit_code == 0

    assert len(calls) == 1
    args = calls[0]

    # Reason: For remote, the tmux command is shlex-joined as the last SSH arg.
    # It should NOT contain "-c" because no directory was specified and
    # the remote default is to let tmux use $HOME.
    assert args[0] == "ssh"
    joined_cmd = args[-1]
    assert "-c" not in joined_cmd


def test_new_default_cmd(monkeypatch):
    """No cmd argument uses config.default_cmd.

    Scenario:
        - Config: default_cmd="/usr/bin/zsh"
        - Invoke: ["new", "api"] (no cmd argument)
    Expected:
        - Exec call args contain "/usr/bin/zsh".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/usr/bin/zsh"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    result = runner.invoke(app, ["new", "api"])

    assert result.exit_code == 0

    assert len(calls) == 1
    args = calls[0]

    # Reason: default_cmd="/usr/bin/zsh" should appear in the tmux command.
    assert "/usr/bin/zsh" in args


def test_new_duplicate_name(monkeypatch):
    """Duplicate session name returns a user-friendly error.

    Scenario:
        - Config: default_node="local"
        - Mock: FakeProcess with returncode=1 and stderr containing
          "duplicate session: api"
        - Invoke: ["new", "api"]
    Expected:
        - exit_code == 1
        - Output contains "Error: Session 'api' already exists on local."
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    error_proc = FakeProcess(
        stderr=b"duplicate session: api", returncode=1
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, error_proc)
    )

    result = runner.invoke(app, ["new", "api"])

    assert result.exit_code == 1
    # Reason: Rich console adds ANSI escape codes around 'api', so we
    # assert on the plain-text fragments that bracket the styled name.
    assert "Error: Session" in result.output
    assert "already exists on local." in result.output
