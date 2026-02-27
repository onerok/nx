"""Tests for the `nx dash` dashboard feature.

Covers Milestone 10: Dashboard (`nx dash`) — CCTV-style read-only view.

Tests mock asyncio.create_subprocess_exec so that fan_out and run_on_node
return controlled results without running real tmux or SSH processes.
A factory function records all subprocess invocations and routes them
based on whether the args contain "list-sessions" or another tmux
sub-command.

The dashboard creates a temporary tmux session (`tmux -L nx_dash`) with
split panes, each showing a read-only attach to a nexus session. The
Enter key tears down the dashboard and attaches to the selected session.
"""

import asyncio
import os

import pytest
from typer.testing import CliRunner

from nx.cli import app
from nx.config import FleetConfig
from nx.dashboard import build_dashboard

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


def make_subprocess_factory(tmux_output: bytes):
    """Create a fake create_subprocess_exec that records calls and routes responses.

    List-sessions calls (args containing "list-sessions") return the provided
    tmux_output. All other calls (new-session, set-option, split-window,
    set-environment, bind-key, select-layout, select-pane) return success.

    Args:
        tmux_output: Raw bytes to return for list-sessions calls.

    Returns:
        tuple: (async factory function, list of recorded arg tuples).
    """
    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        """Record the call and return the appropriate FakeProcess."""
        calls.append(args)
        if "list-sessions" in args:
            return FakeProcess(stdout=tmux_output, returncode=0)
        # Reason: All non-list-sessions calls (new-session, set-option,
        # split-window, set-environment, bind-key, select-layout, select-pane)
        # succeed silently.
        return FakeProcess(stdout=b"", returncode=0)

    return fake_exec, calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dash_creates_temporary_session(monkeypatch):
    """Dashboard creates a tmux session on the nx_dash socket.

    Scenario:
        - Single node "local" with one running session "api".
    Expected:
        - Subprocess calls include `new-session -d -s dashboard` on the
          nx_dash socket.
        - build_dashboard returns the execvp args for attaching.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )

    tmux_output = b"api|1|0|/home/u|bash|1234|0|\n"
    fake_exec, calls = make_subprocess_factory(tmux_output)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = asyncio.run(build_dashboard(config))

    # Verify the return value is the execvp args for attaching.
    assert result == ["tmux", "-L", "nx_dash", "attach", "-t", "dashboard"]

    # Verify that a new-session call was made on the nx_dash socket.
    new_session_calls = [
        c for c in calls
        if "new-session" in c and "-s" in c and "dashboard" in c
    ]
    assert len(new_session_calls) >= 1, (
        f"Expected a new-session call for dashboard, got calls: {calls}"
    )

    # Verify the new-session call includes the nx_dash socket.
    ns_call = new_session_calls[0]
    assert "nx_dash" in ns_call
    assert "-d" in ns_call


def test_dash_pane_metadata(monkeypatch):
    """Each pane is tagged with @nx_target metadata.

    Scenario:
        - Single node "local" with two running sessions: "api" and "worker".
    Expected:
        - Subprocess calls include `set-option -p` with `@nx_target local/api`
          and `@nx_target local/worker`.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )

    tmux_output = (
        b"api|1|0|/home/u|bash|1234|0|\n"
        b"worker|1|0|/home/u|celery|5678|0|\n"
    )
    fake_exec, calls = make_subprocess_factory(tmux_output)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(build_dashboard(config))

    # Collect all set-option calls that tag panes with @nx_target.
    set_option_calls = [c for c in calls if "set-option" in c and "@nx_target" in c]

    # Reason: Two sessions means two set-option calls, one per pane.
    assert len(set_option_calls) == 2

    # Extract the target values from the set-option calls.
    targets = []
    for call in set_option_calls:
        # @nx_target is followed by the target value in the args tuple.
        idx = call.index("@nx_target")
        targets.append(call[idx + 1])

    assert "local/api" in targets
    assert "local/worker" in targets


def test_dash_stores_nx_bin(monkeypatch):
    """NX_BIN environment variable is stored in the dashboard session.

    Scenario:
        - Single node "local" with one running session.
        - shutil.which("nx") returns "/usr/local/bin/nx".
    Expected:
        - Subprocess calls include `set-environment NX_BIN /usr/local/bin/nx`.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )

    tmux_output = b"api|1|0|/home/u|bash|1234|0|\n"
    fake_exec, calls = make_subprocess_factory(tmux_output)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("nx.dashboard.shutil.which", lambda name: "/usr/local/bin/nx")

    asyncio.run(build_dashboard(config))

    # Find the set-environment call.
    set_env_calls = [
        c for c in calls if "set-environment" in c and "NX_BIN" in c
    ]
    assert len(set_env_calls) == 1

    call = set_env_calls[0]
    # Reason: The NX_BIN value should be the resolved path from shutil.which.
    idx = call.index("NX_BIN")
    assert call[idx + 1] == "/usr/local/bin/nx"


def test_dash_enter_binding(monkeypatch):
    """Enter key is bound to a shim that tears down the dashboard and attaches.

    Scenario:
        - Single node "local" with one running session.
    Expected:
        - Subprocess calls include `bind-key -n Enter run-shell` followed by
          a shim string.
        - The shim contains: show-environment, display-message, detach-client,
          kill-session, and exec.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )

    tmux_output = b"api|1|0|/home/u|bash|1234|0|\n"
    fake_exec, calls = make_subprocess_factory(tmux_output)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(build_dashboard(config))

    # Find the bind-key call.
    bind_calls = [c for c in calls if "bind-key" in c and "Enter" in c]
    assert len(bind_calls) == 1

    bind_call = bind_calls[0]
    # Verify the bind-key structure.
    assert "-n" in bind_call
    assert "run-shell" in bind_call

    # The shim is the last argument in the bind-key call.
    shim_idx = bind_call.index("run-shell") + 1
    shim = bind_call[shim_idx]

    # Reason: The shim must contain these tmux operations for the
    # tear-down-and-attach state machine to work correctly.
    assert "show-environment" in shim
    assert "display-message" in shim
    assert "detach-client" in shim
    assert "kill-session" in shim
    assert "exec" in shim


def test_dash_read_only(monkeypatch):
    """All panes are attached in read-only mode with -r flag.

    Scenario:
        - Single node "local" with two running sessions: "api" and "worker".
    Expected:
        - The new-session command includes -r for the initial pane.
        - The split-window command also includes -r for additional panes.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )

    tmux_output = (
        b"api|1|0|/home/u|bash|1234|0|\n"
        b"worker|1|0|/home/u|celery|5678|0|\n"
    )
    fake_exec, calls = make_subprocess_factory(tmux_output)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(build_dashboard(config))

    # Find the new-session call (initial pane) — it should contain -r.
    new_session_calls = [c for c in calls if "new-session" in c]
    assert len(new_session_calls) == 1
    # Reason: The initial pane's attach command must include -r for read-only.
    assert "-r" in new_session_calls[0]

    # Find the split-window calls (additional panes) — each should contain -r.
    split_calls = [c for c in calls if "split-window" in c]
    assert len(split_calls) >= 1
    for sc in split_calls:
        # Reason: Every additional pane must also be read-only.
        assert "-r" in sc


def test_dash_empty_fleet(monkeypatch):
    """Empty fleet prints a message and does not call execvp.

    Scenario:
        - Single node "local" with no running sessions (empty output).
    Expected:
        - Exit code 0.
        - Output contains "No active sessions to display".
        - os.execvp is NOT called.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    async def fake_exec(*args, **kwargs):
        """Return empty stdout for local tmux list-sessions."""
        return FakeProcess(stdout=b"", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    execvp_calls: list[tuple] = []

    def fake_execvp(file, args):
        """Record any execvp call."""
        execvp_calls.append((file, args))

    monkeypatch.setattr(os, "execvp", fake_execvp)

    result = runner.invoke(app, ["dash"])

    assert result.exit_code == 0
    assert "No active sessions to display" in result.output
    # Reason: With no sessions, the dashboard should not attempt to replace
    # the process with tmux attach.
    assert len(execvp_calls) == 0


def test_dash_uses_execvp(monkeypatch):
    """Dashboard CLI command uses os.execvp to attach to the dashboard session.

    Scenario:
        - Single node "local" with one running session.
        - build_dashboard is monkeypatched to return known exec args.
    Expected:
        - os.execvp is called with ("tmux", ["tmux", "-L", "nx_dash",
          "attach", "-t", "dashboard"]).
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    # Monkeypatch build_dashboard to return known args without subprocess calls.
    async def fake_build_dashboard(cfg):
        """Return the expected execvp args directly."""
        return ["tmux", "-L", "nx_dash", "attach", "-t", "dashboard"]

    monkeypatch.setattr("nx.cli.build_dashboard", fake_build_dashboard)

    execvp_calls: list[tuple] = []

    def fake_execvp(file, args):
        """Record the execvp call instead of replacing the process."""
        execvp_calls.append((file, args))

    monkeypatch.setattr(os, "execvp", fake_execvp)

    result = runner.invoke(app, ["dash"])

    assert result.exit_code == 0
    assert len(execvp_calls) == 1

    file, args = execvp_calls[0]
    # Reason: The CLI command should pass the first element as the executable
    # and the full list as the argv to os.execvp.
    assert file == "tmux"
    assert args == ["tmux", "-L", "nx_dash", "attach", "-t", "dashboard"]
