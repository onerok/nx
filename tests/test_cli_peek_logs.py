"""Tests for the `nx peek` and `nx logs` CLI commands.

Covers Milestone 7: `nx peek` & `nx logs` — Observability.

Tests mock both resolve_session (async) and asyncio.create_subprocess_exec
so that the full resolve → build tmux cmd → execute on node → display output
pipeline is verified without real tmux or SSH processes.

Peek shows the last 30 lines of capture-pane output.
Logs is context-aware: 100 lines in an interactive terminal, full scrollback
when piped, and an explicit --lines override.
"""

import asyncio
import sys
from types import SimpleNamespace

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


class _FakeSys:
    """Proxy for the sys module with a controllable stdout.isatty().

    The CliRunner replaces sys.stdout during invoke(), overriding any
    monkeypatch on sys.stdout directly. To work around this, we replace
    the entire sys reference in nx.cli with this proxy. Attribute lookups
    other than ``stdout`` are forwarded to the real sys module, so
    everything except isatty() behaves identically.

    Args:
        tty: Whether stdout.isatty() should return True.
    """

    def __init__(self, tty: bool = True):
        self._tty = tty

    @property
    def stdout(self):
        """Return a namespace whose isatty() returns the configured value."""
        return SimpleNamespace(isatty=lambda: self._tty)

    def __getattr__(self, name):
        """Forward all other attribute access to the real sys module."""
        return getattr(sys, name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_peek_output(monkeypatch):
    """Peek captures the last 30 lines and prints them to stdout.

    Scenario:
        - resolve_session returns ("local", "api").
        - create_subprocess_exec returns FakeProcess with 3 lines of stdout.
        - Invoke: ["peek", "local/api"]
    Expected:
        - exit_code == 0
        - Output contains all three lines.
        - The exec args contain "capture-pane" and "-S", "-30" (30-line default).
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
    process = FakeProcess(stdout=b"line1\nline2\nline3\n", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["peek", "local/api"])

    assert result.exit_code == 0
    assert "line1" in result.output
    assert "line2" in result.output
    assert "line3" in result.output

    # Verify the tmux capture-pane command was built correctly.
    assert len(calls) == 1
    args = calls[0]
    assert "capture-pane" in args
    assert "-S" in args
    s_index = args.index("-S")
    assert args[s_index + 1] == "-30"


def test_peek_uses_resolution(monkeypatch):
    """Bare name triggers resolution; remote target wraps in SSH.

    Scenario:
        - resolve_session returns ("dev-server", "api") for a bare name.
        - Invoke: ["peek", "api"] (bare name, not fully qualified).
    Expected:
        - exit_code == 0
        - The exec args start with "ssh" and include "dev-server"
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
    process = FakeProcess(stdout=b"output\n", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["peek", "api"])

    assert result.exit_code == 0

    # Reason: Remote execution wraps the tmux command in an SSH call.
    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "ssh"
    assert "dev-server" in args


def test_logs_interactive_default(monkeypatch):
    """Interactive terminal defaults to 100 lines of scrollback.

    Scenario:
        - resolve_session returns ("local", "api").
        - sys.stdout.isatty() returns True (interactive terminal).
        - Invoke: ["logs", "local/api"] (no --lines flag).
    Expected:
        - The exec args contain "-S", "-100" (interactive default).
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    # Reason: Replace the sys module reference in nx.cli with a proxy
    # whose stdout.isatty() returns True. We must replace the entire sys
    # reference because the CliRunner overwrites sys.stdout during invoke(),
    # clobbering any direct monkeypatch on sys.stdout.
    monkeypatch.setattr("nx.cli.sys", _FakeSys(tty=True))

    calls: list[tuple] = []
    process = FakeProcess(stdout=b"log output\n", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["logs", "local/api"])

    assert result.exit_code == 0

    # Verify the tmux capture-pane command uses 100-line default.
    assert len(calls) == 1
    args = calls[0]
    assert "-S" in args
    s_index = args.index("-S")
    assert args[s_index + 1] == "-100"


def test_logs_piped_default(monkeypatch):
    """Piped output defaults to full scrollback (no line limit).

    Scenario:
        - resolve_session returns ("local", "api").
        - sys.stdout.isatty() returns False (piped/non-interactive).
        - Invoke: ["logs", "local/api"] (no --lines flag).
    Expected:
        - The exec args contain "-S", "-" (full scrollback).
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    # Reason: Replace the sys module reference in nx.cli with a proxy
    # whose stdout.isatty() returns False (piped/non-interactive).
    monkeypatch.setattr("nx.cli.sys", _FakeSys(tty=False))

    calls: list[tuple] = []
    process = FakeProcess(stdout=b"full log\n", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["logs", "local/api"])

    assert result.exit_code == 0

    # Verify the tmux capture-pane command uses full scrollback.
    assert len(calls) == 1
    args = calls[0]
    assert "-S" in args
    s_index = args.index("-S")
    assert args[s_index + 1] == "-"


def test_logs_custom_lines(monkeypatch):
    """Explicit --lines flag overrides the default line count.

    Scenario:
        - resolve_session returns ("local", "api").
        - Invoke: ["logs", "--lines", "50", "local/api"]
    Expected:
        - The exec args contain "-S", "-50" (user-specified override).
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
    process = FakeProcess(stdout=b"custom\n", returncode=0)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls, process)
    )

    result = runner.invoke(app, ["logs", "--lines", "50", "local/api"])

    assert result.exit_code == 0

    # Verify the tmux capture-pane command uses the user-specified line count.
    assert len(calls) == 1
    args = calls[0]
    assert "-S" in args
    s_index = args.index("-S")
    assert args[s_index + 1] == "-50"


def test_peek_nonexistent_session(monkeypatch):
    """Peek on a nonexistent session prints an error and exits with code 1.

    Scenario:
        - resolve_session raises SessionNotFound.
        - Invoke: ["peek", "ghost"]
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

    result = runner.invoke(app, ["peek", "ghost"])

    assert result.exit_code == 1
    assert "Session" in result.output
    assert "not found" in result.output
