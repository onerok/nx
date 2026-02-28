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
import subprocess

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

    result = runner.invoke(app, ["new", "-D", "api"])

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

    result = runner.invoke(app, ["new", "-D", "--on", "dev-server", "api"])

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

    result = runner.invoke(app, ["new", "-D", "--dir", "/tmp", "api"])

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

    result = runner.invoke(app, ["new", "-D", "api"])

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

    result = runner.invoke(app, ["new", "-D", "--on", "dev-server", "api"])

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

    result = runner.invoke(app, ["new", "-D", "api"])

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

    result = runner.invoke(app, ["new", "-D", "api"])

    assert result.exit_code == 1
    # Reason: Rich console adds ANSI escape codes around 'api', so we
    # assert on the plain-text fragments that bracket the styled name.
    assert "Error: Session" in result.output
    assert "already exists on local." in result.output


def test_new_auto_name(monkeypatch):
    """Omitting the session name auto-generates a coolname slug.

    Scenario:
        - Config: default_node="local"
        - Invoke: ["new"] (no name argument)
        - Monkeypatch generate_slug to return "brave-penguin"
    Expected:
        - exit_code == 0
        - Output contains "Created session local/brave-penguin"
        - Exec args contain "brave-penguin" as the session name
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli.generate_slug", lambda n: "brave-penguin")

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    result = runner.invoke(app, ["new", "-D"])

    assert result.exit_code == 0
    assert "Created session local/brave-penguin" in result.output

    assert len(calls) == 1
    args = calls[0]
    assert "brave-penguin" in args


def test_new_fzf_node_picker(monkeypatch):
    """fzf picker is invoked when multiple nodes and no --on.

    Scenario:
        - Config: nodes=["local", "dev-server", "staging"], default_node="local"
        - stdin is a tty
        - fzf returns "dev-server"
        - Invoke: ["new", "api"] (no --on)
    Expected:
        - subprocess.run is called with fzf and the node list
        - default_node appears first in the fzf input
        - Session is created on fzf-selected node "dev-server"
    """
    config = FleetConfig(
        nodes=["local", "dev-server", "staging"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli._stdin_is_tty", lambda: True)

    # Track fzf calls and return "dev-server" as the selection.
    fzf_calls: list[tuple] = []
    real_subprocess_run = subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        """Intercept fzf calls; pass through others."""
        if cmd[0] == "fzf":
            fzf_calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="dev-server\n", stderr=""
            )
        return real_subprocess_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    # Mock create_subprocess_exec for the tmux call.
    exec_calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(exec_calls)
    )

    result = runner.invoke(app, ["new", "-D", "api"])

    assert result.exit_code == 0
    assert "Created session dev-server/api" in result.output

    # Verify fzf was called with correct prompt.
    assert len(fzf_calls) == 1
    fzf_cmd, fzf_kwargs = fzf_calls[0]
    assert fzf_cmd == ["fzf", "--prompt", "Select node: "]

    # Verify default_node "local" appears first in the fzf input.
    fzf_input = fzf_kwargs["input"]
    lines = fzf_input.strip().split("\n")
    assert lines[0] == "local"

    # Verify the tmux command targeted dev-server (via SSH).
    assert len(exec_calls) == 1
    assert exec_calls[0][0] == "ssh"
    assert "dev-server" in exec_calls[0]


def test_new_on_flag_triggers_picker(monkeypatch):
    """--on without a value triggers fzf node picker.

    Scenario:
        - Config: nodes=["local", "dev-server"], default_node="local"
        - Invoke: ["new", "--on"] (--on as last arg, no value)
        - fzf returns "dev-server"
    Expected:
        - fzf is invoked to pick a node
        - Session name is auto-generated (no name arg)
        - Session is created on fzf-selected node "dev-server"
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli.generate_slug", lambda n: "brave-penguin")

    fzf_calls: list[tuple] = []
    real_subprocess_run = subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        """Intercept fzf calls; pass through others."""
        if cmd[0] == "fzf":
            fzf_calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="dev-server\n", stderr=""
            )
        return real_subprocess_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    exec_calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(exec_calls)
    )

    result = runner.invoke(app, ["new", "-D", "--on"])

    assert result.exit_code == 0
    assert "Created session dev-server/brave-penguin" in result.output
    assert len(fzf_calls) == 1


def test_new_single_node_no_fzf(monkeypatch):
    """Single-node fleet skips fzf and uses the only node directly.

    Scenario:
        - Config: nodes=["local"], default_node="local"
        - Invoke: ["new", "api"] (no --on)
    Expected:
        - No fzf call; session created on "local".
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    fzf_called = False
    real_subprocess_run = subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        """Fail if fzf is invoked."""
        nonlocal fzf_called
        if cmd[0] == "fzf":
            fzf_called = True
        return real_subprocess_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    exec_calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(exec_calls)
    )

    result = runner.invoke(app, ["new", "-D", "api"])

    assert result.exit_code == 0
    assert not fzf_called
    assert "Created session local/api" in result.output


def test_new_multi_node_not_tty_uses_default(monkeypatch):
    """Non-interactive multi-node fleet falls back to default_node.

    Scenario:
        - Config: nodes=["local", "dev-server"], default_node="local"
        - stdin is NOT a tty (piped)
        - Invoke: ["new", "api"] (no --on)
    Expected:
        - No fzf call; session created on default_node "local".
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.setattr("nx.cli._stdin_is_tty", lambda: False)

    fzf_called = False
    real_subprocess_run = subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        """Fail if fzf is invoked."""
        nonlocal fzf_called
        if cmd[0] == "fzf":
            fzf_called = True
        return real_subprocess_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    exec_calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(exec_calls)
    )

    result = runner.invoke(app, ["new", "-D", "api"])

    assert result.exit_code == 0
    assert not fzf_called
    assert "Created session local/api" in result.output


def test_new_auto_attach(monkeypatch):
    """nx new auto-attaches to the created session by default.

    Scenario:
        - Config: default_node="local"
        - No TMUX env (bare terminal -> execvp path)
        - Invoke: ["new", "api"] (no -D flag)
    Expected:
        - Session is created successfully
        - os.execvp is called with tmux attach args
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)
    monkeypatch.delenv("TMUX", raising=False)

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    # Track execvp calls instead of letting it replace the process.
    execvp_calls: list[tuple] = []

    def fake_execvp(file, args):
        """Record execvp call."""
        execvp_calls.append((file, args))

    monkeypatch.setattr(os, "execvp", fake_execvp)

    result = runner.invoke(app, ["new", "api"])

    assert result.exit_code == 0
    assert "Created session local/api" in result.output

    # Reason: Auto-attach calls execvp with tmux attach.
    assert len(execvp_calls) == 1
    file, args = execvp_calls[0]
    assert file == "tmux"
    assert "-L" in args
    assert "nexus" in args
    assert "attach" in args
    assert "-t" in args
    assert "api" in args


def test_new_detach_skips_attach(monkeypatch):
    """--detach / -D skips auto-attach.

    Scenario:
        - Config: default_node="local"
        - Invoke: ["new", "-D", "api"]
    Expected:
        - Session is created but os.execvp is NOT called.
    """
    config = FleetConfig(
        nodes=["local"], default_node="local", default_cmd="/bin/bash"
    )
    monkeypatch.setattr("nx.cli.load_config", lambda path=None: config)

    calls: list[tuple] = []
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _make_fake_exec(calls)
    )

    execvp_calls: list[tuple] = []
    monkeypatch.setattr(os, "execvp", lambda f, a: execvp_calls.append((f, a)))

    result = runner.invoke(app, ["new", "-D", "api"])

    assert result.exit_code == 0
    assert "Created session local/api" in result.output
    assert len(execvp_calls) == 0
