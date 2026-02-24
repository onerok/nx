"""Tests for session resolution protocol (resolve.py).

Covers Milestone 5: Resolution Protocol.

Tests mock asyncio.create_subprocess_exec for fan_out calls and
subprocess.run for fzf disambiguation. sys.stdin.isatty is mocked
to control interactive vs piped behaviour.
"""

import asyncio
import subprocess as subprocess_mod

import pytest

from nx.resolve import AmbiguousSession, SessionNotFound, resolve_session
from nx.config import FleetConfig


# ---------------------------------------------------------------------------
# Helpers for subprocess mocking
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
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_node_config() -> FleetConfig:
    """FleetConfig with local and dev-server nodes.

    Returns:
        FleetConfig: Config with two nodes and local as default.
    """
    return FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )


@pytest.fixture
def three_node_config() -> FleetConfig:
    """FleetConfig with alpha, beta, gamma nodes; gamma is default.

    Returns:
        FleetConfig: Config with three nodes and gamma as default.
    """
    return FleetConfig(
        nodes=["alpha", "beta", "gamma"],
        default_node="gamma",
        default_cmd="/bin/bash",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_fully_qualified(two_node_config):
    """Fully qualified name (node/session) bypasses fan-out and returns directly."""
    node, session = await resolve_session("local/api", two_node_config)

    assert node == "local"
    assert session == "api"


@pytest.mark.asyncio
async def test_resolve_unique_match(monkeypatch, two_node_config):
    """Bare name matching exactly one session across all nodes returns it."""

    async def fake_exec(*args, **kwargs):
        """Return different sessions per node.

        local has 'api', dev-server has 'data'.
        """
        # Reason: Local calls start with "tmux"; remote calls start with "ssh".
        if args[0] == "tmux":
            return FakeProcess(
                stdout=b"api|1|0|/home/u|bash|1234|0|\n", returncode=0
            )
        elif "dev-server" in args:
            return FakeProcess(
                stdout=b"data|1|0|/app|python|5678|0|\n", returncode=0
            )
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    node, session = await resolve_session("api", two_node_config)

    assert node == "local"
    assert session == "api"


@pytest.mark.asyncio
async def test_resolve_no_match(monkeypatch, two_node_config):
    """Bare name matching no sessions raises SessionNotFound."""

    async def fake_exec(*args, **kwargs):
        """Return only 'api' on local; nothing on dev-server."""
        if args[0] == "tmux":
            return FakeProcess(
                stdout=b"api|1|0|/home/u|bash|1234|0|\n", returncode=0
            )
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(SessionNotFound):
        await resolve_session("nonexistent", two_node_config)


@pytest.mark.asyncio
async def test_resolve_ambiguous_interactive(monkeypatch, two_node_config):
    """Ambiguous match with interactive tty launches fzf and returns selection."""
    two_node_config.default_node = "local"

    async def fake_exec(*args, **kwargs):
        """Both nodes have a session named 'api'."""
        if args[0] == "tmux":
            return FakeProcess(
                stdout=b"api|1|0|/home/u|bash|1234|0|\n", returncode=0
            )
        elif "dev-server" in args:
            return FakeProcess(
                stdout=b"api|1|0|/app|python|5678|0|\n", returncode=0
            )
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # Reason: Mock isatty to simulate interactive terminal.
    monkeypatch.setattr("nx.resolve.sys.stdin", type("FakeStdin", (), {"isatty": lambda self: True})())

    # Capture subprocess.run calls for fzf.
    captured: list[dict] = []

    def fake_fzf(*args, **kwargs):
        """Record kwargs and return a fake CompletedProcess."""
        captured.append({"args": args, "kwargs": kwargs})
        return subprocess_mod.CompletedProcess(
            args=args[0], returncode=0, stdout="local/api\n", stderr=""
        )

    monkeypatch.setattr("nx.resolve.subprocess.run", fake_fzf)

    node, session = await resolve_session("api", two_node_config)

    # Verify fzf was called with correct args.
    assert len(captured) == 1
    call_args = captured[0]["args"][0]
    assert call_args == ["fzf", "--prompt", "Select session: "]

    # Verify the input kwarg contains both matches.
    fzf_input = captured[0]["kwargs"]["input"]
    assert "local/api" in fzf_input
    assert "dev-server/api" in fzf_input

    assert node == "local"
    assert session == "api"


@pytest.mark.asyncio
async def test_resolve_ambiguous_piped(monkeypatch, two_node_config):
    """Ambiguous match with non-interactive stdin raises AmbiguousSession."""

    async def fake_exec(*args, **kwargs):
        """Both nodes have a session named 'api'."""
        if args[0] == "tmux":
            return FakeProcess(
                stdout=b"api|1|0|/home/u|bash|1234|0|\n", returncode=0
            )
        elif "dev-server" in args:
            return FakeProcess(
                stdout=b"api|1|0|/app|python|5678|0|\n", returncode=0
            )
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # Reason: Mock isatty to simulate piped/non-interactive stdin.
    monkeypatch.setattr("nx.resolve.sys.stdin", type("FakeStdin", (), {"isatty": lambda self: False})())

    with pytest.raises(AmbiguousSession) as exc_info:
        await resolve_session("api", two_node_config)

    error_msg = str(exc_info.value)
    assert "Ambiguous" in error_msg
    assert "local/api" in error_msg
    assert "dev-server/api" in error_msg


@pytest.mark.asyncio
async def test_resolve_fzf_default_node_first(monkeypatch, three_node_config):
    """Default node matches are sorted first in fzf input."""

    async def fake_exec(*args, **kwargs):
        """All three nodes have a session named 'api'."""
        # Reason: All calls return the same session "api".
        # Local (alpha) uses tmux directly; beta and gamma go through ssh.
        return FakeProcess(
            stdout=b"api|1|0|/home/u|bash|1234|0|\n", returncode=0
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # Interactive tty.
    monkeypatch.setattr("nx.resolve.sys.stdin", type("FakeStdin", (), {"isatty": lambda self: True})())

    captured: list[dict] = []

    def fake_fzf(*args, **kwargs):
        """Capture input and return gamma/api as selection."""
        captured.append({"args": args, "kwargs": kwargs})
        return subprocess_mod.CompletedProcess(
            args=args[0], returncode=0, stdout="gamma/api\n", stderr=""
        )

    monkeypatch.setattr("nx.resolve.subprocess.run", fake_fzf)

    node, session = await resolve_session("api", three_node_config)

    # Verify gamma/api (default node) is the FIRST line in fzf input.
    fzf_input = captured[0]["kwargs"]["input"]
    lines = fzf_input.strip().split("\n")
    assert lines[0] == "gamma/api"

    # Reason: The remaining lines should be alpha and beta, sorted alphabetically.
    assert "alpha/api" in lines
    assert "beta/api" in lines

    assert node == "gamma"
    assert session == "api"


@pytest.mark.asyncio
async def test_resolve_fzf_output_parsed(monkeypatch, two_node_config):
    """Fzf output is correctly parsed into (node, session) tuple."""

    async def fake_exec(*args, **kwargs):
        """Both nodes have a session named 'api'."""
        if args[0] == "tmux":
            return FakeProcess(
                stdout=b"api|1|0|/home/u|bash|1234|0|\n", returncode=0
            )
        elif "dev-server" in args:
            return FakeProcess(
                stdout=b"api|1|0|/app|python|5678|0|\n", returncode=0
            )
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # Interactive tty.
    monkeypatch.setattr("nx.resolve.sys.stdin", type("FakeStdin", (), {"isatty": lambda self: True})())

    def fake_fzf(*args, **kwargs):
        """Return dev-server/api as the user's selection."""
        return subprocess_mod.CompletedProcess(
            args=args[0], returncode=0, stdout="dev-server/api\n", stderr=""
        )

    monkeypatch.setattr("nx.resolve.subprocess.run", fake_fzf)

    node, session = await resolve_session("api", two_node_config)

    assert node == "dev-server"
    assert session == "api"
