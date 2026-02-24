"""Tests for the `nx attach` CLI command.

Covers Milestone 6: `nx attach` — Session Attachment.

Tests mock resolve_session (async) so that fan-out resolution is not
exercised, and verify that the correct tmux/SSH command is constructed
based on the caller's TMUX environment variable.

Three scenarios are tested:
    A — bare terminal (no $TMUX): process is replaced via os.execvp.
    B — inside nexus tmux ("nexus" in $TMUX): switch-client or new-window.
    C — inside user's personal tmux ($TMUX set, no "nexus"): new-window
        that nests into the nexus session.
"""

import os
import subprocess as subprocess_mod

from typer.testing import CliRunner

from nx.cli import app
from nx.config import FleetConfig

runner = CliRunner()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_attach_bare_terminal(monkeypatch):
    """Bare terminal attaching to a remote session replaces the process via SSH.

    Scenario:
        - No $TMUX environment variable set.
        - resolve_session returns ("dev-server", "api") — a remote target.
    Expected:
        - os.execvp is called with "ssh" and the full SSH+tmux attach args.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.delenv("TMUX", raising=False)

    async def fake_resolve(name, config):
        """Return a remote node/session pair."""
        return ("dev-server", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    execvp_calls: list[tuple] = []

    def fake_execvp(file, args):
        """Record the execvp call instead of replacing the process."""
        execvp_calls.append((file, args))

    monkeypatch.setattr(os, "execvp", fake_execvp)

    result = runner.invoke(app, ["attach", "api"])

    assert result.exit_code == 0
    assert len(execvp_calls) == 1

    file, args = execvp_calls[0]
    # Reason: Remote attach from bare terminal uses SSH to reach the node.
    assert file == "ssh"
    assert args == [
        "ssh", "-t", "dev-server",
        "tmux", "-L", "nexus", "attach", "-t", "api",
    ]


def test_attach_bare_terminal_local(monkeypatch):
    """Bare terminal attaching to a local session replaces the process with tmux.

    Scenario:
        - No $TMUX environment variable set.
        - resolve_session returns ("local", "api") — a local target.
    Expected:
        - os.execvp is called with "tmux" and the local tmux attach args.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.delenv("TMUX", raising=False)

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    execvp_calls: list[tuple] = []

    def fake_execvp(file, args):
        """Record the execvp call instead of replacing the process."""
        execvp_calls.append((file, args))

    monkeypatch.setattr(os, "execvp", fake_execvp)

    result = runner.invoke(app, ["attach", "api"])

    assert result.exit_code == 0
    assert len(execvp_calls) == 1

    file, args = execvp_calls[0]
    # Reason: Local attach from bare terminal uses tmux directly.
    assert file == "tmux"
    assert args == ["tmux", "-L", "nexus", "attach", "-t", "api"]


def test_attach_from_nexus_local(monkeypatch):
    """Inside nexus tmux, local attach uses switch-client.

    Scenario:
        - $TMUX = "/tmp/tmux-1000/nexus,12345,0" (contains "nexus").
        - resolve_session returns ("local", "api").
    Expected:
        - subprocess.run is called with tmux switch-client args.
        - typer.Exit is raised (exit_code == 0).
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/nexus,12345,0")

    async def fake_resolve(name, config):
        """Return a local node/session pair."""
        return ("local", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    captured_runs: list[tuple] = []

    def fake_subprocess_run(*args, **kwargs):
        """Record subprocess.run call and return success."""
        captured_runs.append(args)
        return subprocess_mod.CompletedProcess(args=args[0], returncode=0)

    monkeypatch.setattr("nx.cli.subprocess.run", fake_subprocess_run)

    result = runner.invoke(app, ["attach", "api"])

    assert result.exit_code == 0
    assert len(captured_runs) == 1

    # Reason: Inside nexus tmux, local attach uses switch-client to stay
    # within the same nexus tmux server.
    assert captured_runs[0][0] == [
        "tmux", "-L", "nexus", "switch-client", "-t", "api",
    ]


def test_attach_from_nexus_remote(monkeypatch):
    """Inside nexus tmux, remote attach opens a new window with SSH.

    Scenario:
        - $TMUX = "/tmp/tmux-1000/nexus,12345,0" (contains "nexus").
        - resolve_session returns ("dev-server", "api").
    Expected:
        - subprocess.run is called with tmux new-window + SSH attach args.
        - typer.Exit is raised (exit_code == 0).
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/nexus,12345,0")

    async def fake_resolve(name, config):
        """Return a remote node/session pair."""
        return ("dev-server", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    captured_runs: list[tuple] = []

    def fake_subprocess_run(*args, **kwargs):
        """Record subprocess.run call and return success."""
        captured_runs.append(args)
        return subprocess_mod.CompletedProcess(args=args[0], returncode=0)

    monkeypatch.setattr("nx.cli.subprocess.run", fake_subprocess_run)

    result = runner.invoke(app, ["attach", "api"])

    assert result.exit_code == 0
    assert len(captured_runs) == 1

    # Reason: Inside nexus tmux, remote attach opens a new window that
    # SSHes to the remote node and attaches to the nexus session there.
    assert captured_runs[0][0] == [
        "tmux", "-L", "nexus", "new-window", "-n", "api",
        "ssh", "-t", "dev-server",
        "tmux", "-L", "nexus", "attach", "-t", "api",
    ]


def test_attach_from_user_tmux(monkeypatch):
    """Inside user's personal tmux, remote attach opens a nested window.

    Scenario:
        - $TMUX = "/tmp/tmux-1000/default,12345,0" (no "nexus" in value).
        - resolve_session returns ("dev-server", "api").
    Expected:
        - subprocess.run is called with tmux new-window + SSH + nexus attach.
        - typer.Exit is raised (exit_code == 0).
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")

    async def fake_resolve(name, config):
        """Return a remote node/session pair."""
        return ("dev-server", "api")

    monkeypatch.setattr("nx.cli.resolve_session", fake_resolve)

    captured_runs: list[tuple] = []

    def fake_subprocess_run(*args, **kwargs):
        """Record subprocess.run call and return success."""
        captured_runs.append(args)
        return subprocess_mod.CompletedProcess(args=args[0], returncode=0)

    monkeypatch.setattr("nx.cli.subprocess.run", fake_subprocess_run)

    result = runner.invoke(app, ["attach", "api"])

    assert result.exit_code == 0
    assert len(captured_runs) == 1

    # Reason: Inside user's personal tmux (no "nexus"), a new window is
    # opened in the user's tmux that nests into the nexus session via SSH.
    assert captured_runs[0][0] == [
        "tmux", "new-window", "-n", "api",
        "ssh", "-t", "dev-server",
        "tmux", "-L", "nexus", "attach", "-t", "api",
    ]


def test_attach_uses_resolution(monkeypatch):
    """Fully qualified name bypasses fan-out and uses resolved node/session.

    Scenario:
        - No $TMUX environment variable set.
        - Invoke: ["attach", "local/api"] (fully qualified name).
        - resolve_session for "local/api" splits on "/" and returns
          ("local", "api") without fan-out.
    Expected:
        - os.execvp is called with "tmux" and local attach args.
        - The command uses "local" as node and "api" as session.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.delenv("TMUX", raising=False)

    # Reason: We use the real resolve_session here to verify that fully
    # qualified names ("local/api") are split correctly without fan-out.
    # resolve_session returns ("local", "api") for "local/api" synchronously
    # (no network calls needed), so no additional mocking is required.
    # We keep the real resolve_session — no monkeypatch for it.

    execvp_calls: list[tuple] = []

    def fake_execvp(file, args):
        """Record the execvp call instead of replacing the process."""
        execvp_calls.append((file, args))

    monkeypatch.setattr(os, "execvp", fake_execvp)

    result = runner.invoke(app, ["attach", "local/api"])

    assert result.exit_code == 0
    assert len(execvp_calls) == 1

    file, args = execvp_calls[0]
    # Reason: "local/api" resolves to node="local", session="api",
    # so local tmux attach is used (no SSH).
    assert file == "tmux"
    assert args == ["tmux", "-L", "nexus", "attach", "-t", "api"]
