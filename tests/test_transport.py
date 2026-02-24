"""Tests for SSH transport (ssh.py) and tmux command builder (tmux.py).

Covers Milestone 2: SSH Transport + tmux Command Builder.

SSH tests mock asyncio.create_subprocess_exec via monkeypatch.
tmux tests are pure unit tests — no mocking needed.
"""

import asyncio

import pytest

from nx.ssh import NodeResult, fan_out, run_on_node
from nx.tmux import (
    FORMAT_STRING,
    SessionInfo,
    build_capture_cmd,
    build_kill_cmd,
    build_list_cmd,
    build_new_cmd,
    build_send_keys_cmd,
    parse_list_output,
)


# ---------------------------------------------------------------------------
# Helpers for SSH mocking
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


# ===========================================================================
# SSH Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_run_local_command(monkeypatch):
    """Local node runs command directly without SSH wrapper."""
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        """Record the call args and return a FakeProcess."""
        calls.append(args)
        return FakeProcess(stdout=b"hi\n", stderr=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await run_on_node("local", ["echo", "hi"])

    assert result.stdout == "hi\n"
    assert result.stderr == ""
    assert result.returncode == 0
    assert result.node == "local"

    # Reason: local execution should NOT invoke ssh — just echo, hi directly.
    assert calls[0] == ("echo", "hi")


@pytest.mark.asyncio
async def test_run_remote_command(monkeypatch):
    """Remote node wraps the command in an SSH call with ConnectTimeout."""
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        """Record the call args and return a FakeProcess."""
        calls.append(args)
        return FakeProcess(stdout=b"hi\n", stderr=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await run_on_node("dev-server", ["echo", "hi"])

    # Reason: remote commands must be wrapped in ssh with ConnectTimeout
    # and the original command joined via shlex.join.
    assert calls[0] == ("ssh", "-o", "ConnectTimeout=2", "dev-server", "echo hi")
    assert result.node == "dev-server"
    assert result.stdout == "hi\n"
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_fan_out_parallel(monkeypatch):
    """fan_out dispatches to multiple nodes and returns results keyed by node."""
    call_count = 0

    async def fake_exec(*args, **kwargs):
        """Return different stdout per node based on the call args."""
        nonlocal call_count
        call_count += 1
        # Reason: The first positional arg distinguishes local vs ssh calls.
        # For local, args[0] is the command itself; for remote, args[0] is "ssh".
        if args[0] == "echo":
            # local call
            return FakeProcess(stdout=b"local-output\n", returncode=0)
        else:
            # ssh call — remote
            return FakeProcess(stdout=b"remote-output\n", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    results = await fan_out(["local", "dev-server"], ["echo", "hi"])

    assert isinstance(results, dict)
    assert set(results.keys()) == {"local", "dev-server"}
    assert results["local"].stdout == "local-output\n"
    assert results["local"].node == "local"
    assert results["dev-server"].stdout == "remote-output\n"
    assert results["dev-server"].node == "dev-server"
    assert call_count == 2


@pytest.mark.asyncio
async def test_fan_out_timeout(monkeypatch):
    """A failing node returns error NodeResult; other nodes still succeed."""

    async def fake_exec(*args, **kwargs):
        """Raise OSError for the bad node, succeed for local."""
        if args[0] == "ssh":
            raise OSError("connection refused")
        return FakeProcess(stdout=b"ok\n", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    results = await fan_out(["local", "bad-node"], ["echo", "hi"])

    # Reason: Successful node should still return normally even when one fails.
    assert results["local"].returncode == 0
    assert results["local"].stdout == "ok\n"

    # Reason: Failed node gets wrapped into a NodeResult with returncode=1.
    assert results["bad-node"].returncode == 1
    assert "connection refused" in results["bad-node"].stderr
    assert results["bad-node"].stdout == ""


@pytest.mark.asyncio
async def test_fan_out_semaphore_created(monkeypatch):
    """fan_out creates asyncio.Semaphore with the given max_concurrent value."""
    captured_semaphore_values: list[int] = []
    _real_semaphore = asyncio.Semaphore

    class TrackingSemaphore(_real_semaphore):
        """Wrapper that records the value passed to Semaphore.__init__."""

        def __init__(self, value=1):
            captured_semaphore_values.append(value)
            super().__init__(value)

    monkeypatch.setattr(asyncio, "Semaphore", TrackingSemaphore)

    # Reason: We still need a working subprocess mock so fan_out completes.
    async def fake_exec(*args, **kwargs):
        return FakeProcess(stdout=b"ok\n", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await fan_out(["local"], ["echo"], max_concurrent=8)

    assert 8 in captured_semaphore_values


# ===========================================================================
# tmux Tests
# ===========================================================================


def test_build_list_cmd():
    """build_list_cmd returns the correct tmux list-sessions command."""
    expected = ["tmux", "-L", "nexus", "list-sessions", "-F", FORMAT_STRING]
    assert build_list_cmd() == expected


def test_parse_list_output():
    """parse_list_output converts pipe-delimited lines into SessionInfo objects."""
    raw = "api|1|0|/home/u/app|python|1234|0|\n"
    result = parse_list_output(raw)

    assert len(result) == 1
    s = result[0]
    assert s == SessionInfo(
        name="api",
        windows=1,
        attached=0,
        pane_path="/home/u/app",
        pane_cmd="python",
        pane_pid=1234,
        is_dead=False,
        exit_status=None,
    )


def test_parse_empty_output():
    """Empty or whitespace-only input returns an empty list, not an error."""
    assert parse_list_output("") == []
    assert parse_list_output("\n") == []


def test_parse_dead_pane():
    """A pane with pane_dead=1 is parsed with is_dead=True and exit_status set."""
    raw = "crashed|1|0|/home/u|bash|5678|1|1\n"
    result = parse_list_output(raw)

    assert len(result) == 1
    s = result[0]
    assert s.is_dead is True
    assert s.exit_status == 1
    assert s.name == "crashed"
    assert s.pane_pid == 5678


def test_build_new_cmd():
    """build_new_cmd builds correct tmux new-session commands."""
    # With command and directory
    result = build_new_cmd("api", "python serve.py", "/app")
    assert result == [
        "tmux", "-L", "nexus", "new-session", "-d", "-s", "api",
        "-c", "/app", "python", "serve.py",
    ]

    # Without command or directory — minimal form
    result_minimal = build_new_cmd("api")
    assert result_minimal == [
        "tmux", "-L", "nexus", "new-session", "-d", "-s", "api",
    ]


def test_build_capture_cmd():
    """build_capture_cmd builds correct capture-pane commands."""
    # Numeric lines — -S -30
    result = build_capture_cmd("api", 30)
    assert result == [
        "tmux", "-L", "nexus", "capture-pane", "-p", "-t", "api", "-S", "-30",
    ]

    # Full scrollback — -S -
    result_full = build_capture_cmd("api", "-")
    assert result_full == [
        "tmux", "-L", "nexus", "capture-pane", "-p", "-t", "api", "-S", "-",
    ]


def test_build_send_keys_cmd():
    """build_send_keys_cmd appends Enter by default, omits it in raw mode."""
    # Default mode — Enter appended
    result = build_send_keys_cmd("api", ["hello"])
    assert result == [
        "tmux", "-L", "nexus", "send-keys", "-t", "api", "hello", "Enter",
    ]

    # Raw mode — no Enter appended
    result_raw = build_send_keys_cmd("api", ["C-c"], raw=True)
    assert result_raw == [
        "tmux", "-L", "nexus", "send-keys", "-t", "api", "C-c",
    ]


def test_build_kill_cmd():
    """build_kill_cmd builds correct kill-session command."""
    result = build_kill_cmd("api")
    assert result == [
        "tmux", "-L", "nexus", "kill-session", "-t", "api",
    ]
