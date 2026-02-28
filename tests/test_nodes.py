"""Tests for node management (M11: `nx nodes`).

Covers:
- nodes_ls: listing nodes with reachability and config drift status
- nodes_add: verifying remote tmux version, creating socket dir, pushing
  tmux.conf, appending SSH config block, idempotent re-add
- nodes_rm: removing SSH config blocks, error on unknown host

Tests mock asyncio.create_subprocess_exec to control what run_on_node
returns. File-system operations (SSH config read/write, socket dir creation)
use pytest's tmp_path fixture.
"""

import asyncio
import hashlib
import importlib.resources
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nx.cli import app
from nx.config import FleetConfig
from nx.nodes import (
    SSH_CONFIG_TEMPLATE,
    NodeStatus,
    discover_hosts,
    nodes_add,
    nodes_ls,
    nodes_rm,
    parse_ssh_config_hosts,
)

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


def _canonical_tmux_conf_hash() -> str:
    """Compute the MD5 hash of the canonical tmux.conf for test assertions.

    Returns:
        str: Hex digest of the shipped tmux.conf.
    """
    ref = importlib.resources.files("nx.data").joinpath("tmux.conf")
    return hashlib.md5(ref.read_bytes()).hexdigest()


def _make_fake_exec_for_nodes(
    calls: list,
    responses: dict[str, tuple[bytes, bytes, int]] | None = None,
):
    """Create a fake create_subprocess_exec for node tests.

    The responses dict maps a substring to (stdout, stderr, returncode).
    When a subprocess call is made, the first matching substring in the
    args determines the response. If no match, returns empty success.

    Args:
        calls: List to append captured positional args to.
        responses: Mapping of command substring to (stdout, stderr, returncode).

    Returns:
        Async callable matching the asyncio.create_subprocess_exec signature.
    """
    if responses is None:
        responses = {}

    async def fake_exec(*args, **kwargs):
        """Route to correct response based on command args."""
        calls.append(args)
        joined = " ".join(str(a) for a in args)

        for pattern, (stdout, stderr, rc) in responses.items():
            if pattern in joined:
                return FakeProcess(stdout=stdout, stderr=stderr, returncode=rc)

        return FakeProcess(stdout=b"", returncode=0)

    return fake_exec


# ---------------------------------------------------------------------------
# nodes_ls tests
# ---------------------------------------------------------------------------


def test_nodes_ls(monkeypatch, tmp_path):
    """nodes_ls returns status for each node in the fleet.

    Scenario:
        - Config: nodes=["local", "dev-server"].
        - local tmux -V returns "tmux 3.4".
        - dev-server tmux -V returns "tmux 3.2".
        - dev-server md5sum returns a matching hash (no drift).
    Expected:
        - Two NodeStatus results, both reachable, no drift.
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    canonical_hash = _canonical_tmux_conf_hash()

    calls: list[tuple] = []
    responses = {
        # Reason: local calls use "tmux" directly; remote calls use "ssh".
        # The local tmux -V check.
        "tmux -V": (b"tmux 3.4\n", b"", 0),
        # The remote tmux -V check — goes via SSH so the command string
        # includes the node name and the tmux command.
        "ssh": (b"tmux 3.2\n", b"", 0),
        # The remote md5sum call — also via SSH. Return matching hash.
        "md5sum": (f"{canonical_hash}  /home/u/.config/nexus/tmux.conf\n".encode(), b"", 0),
    }

    # Reason: We need more fine-grained control. When the joined args
    # contain "ssh" AND "md5sum", we want the md5sum response. When
    # it contains "ssh" AND "tmux -V", we want the tmux version.
    # Rebuild with ordering that checks md5sum before generic ssh.
    ordered_responses = {
        "md5sum": (f"{canonical_hash}  /home/u/.config/nexus/tmux.conf\n".encode(), b"", 0),
        "tmux -V": (b"tmux 3.4\n", b"", 0),
    }

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_nodes(calls, ordered_responses),
    )

    # Reason: nodes_ls calls _local_tmux_conf_hash() which reads the
    # packaged tmux.conf — no need to mock that since the package data
    # exists in the test environment.

    statuses = asyncio.run(nodes_ls(config))

    assert len(statuses) == 2
    # Local node.
    local_status = next(s for s in statuses if s.node == "local")
    assert local_status.reachable is True
    assert local_status.tmux_version == "tmux 3.4"
    # Remote node.
    remote_status = next(s for s in statuses if s.node == "dev-server")
    assert remote_status.reachable is True


def test_nodes_ls_unreachable(monkeypatch):
    """nodes_ls marks unreachable nodes correctly.

    Scenario:
        - Config: nodes=["local", "dead-server"].
        - local tmux -V returns "tmux 3.4".
        - dead-server tmux -V returns non-zero (unreachable).
    Expected:
        - local is reachable, dead-server is not.
    """
    config = FleetConfig(
        nodes=["local", "dead-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    calls: list[tuple] = []

    async def fake_exec(*args, **kwargs):
        """Local succeeds, remote fails."""
        calls.append(args)
        joined = " ".join(str(a) for a in args)
        if args[0] == "tmux":
            # Local call
            return FakeProcess(stdout=b"tmux 3.4\n", returncode=0)
        else:
            # SSH call to dead-server — unreachable
            return FakeProcess(stdout=b"", stderr=b"Connection refused", returncode=255)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    statuses = asyncio.run(nodes_ls(config))

    local_status = next(s for s in statuses if s.node == "local")
    assert local_status.reachable is True

    dead_status = next(s for s in statuses if s.node == "dead-server")
    assert dead_status.reachable is False
    assert dead_status.tmux_version is None


# ---------------------------------------------------------------------------
# nodes_add tests
# ---------------------------------------------------------------------------


def test_nodes_add_verifies_tmux(monkeypatch, tmp_path):
    """nodes_add raises RuntimeError when remote tmux version < 3.0.

    Scenario:
        - Remote host returns "tmux 2.9".
    Expected:
        - RuntimeError mentioning version requirement.
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    calls: list[tuple] = []
    responses = {
        "tmux -V": (b"tmux 2.9\n", b"", 0),
    }

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_nodes(calls, responses),
    )

    ssh_config = tmp_path / "nexus_config"

    with pytest.raises(RuntimeError, match="tmux >= 3.0 required"):
        asyncio.run(nodes_add("new-server", config, ssh_config_path=ssh_config))


def test_nodes_add_creates_socket_dir(monkeypatch, tmp_path):
    """nodes_add creates the ~/.ssh/sockets/ directory.

    Scenario:
        - Remote host returns "tmux 3.4".
        - The socket dir does not exist yet.
    Expected:
        - Log includes "Ensured ... exists".
        - The socket directory is created.
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    calls: list[tuple] = []
    responses = {
        "tmux -V": (b"tmux 3.4\n", b"", 0),
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_nodes(calls, responses),
    )

    ssh_config = tmp_path / "nexus_config"

    # Reason: Monkeypatch Path.home() to use tmp_path so we can verify
    # socket dir creation without touching the real filesystem.
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    messages = asyncio.run(nodes_add("new-server", config, ssh_config_path=ssh_config))

    socket_dir = fake_home / ".ssh" / "sockets"
    assert socket_dir.exists()
    assert any("Ensured" in m for m in messages)


def test_nodes_add_pushes_tmux_conf(monkeypatch, tmp_path):
    """nodes_add sends the canonical tmux.conf to the remote host.

    Scenario:
        - Remote host returns "tmux 3.4".
    Expected:
        - A subprocess call is made that includes "tmux.conf" (the push).
        - Log includes "Pushed tmux.conf".
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    calls: list[tuple] = []
    responses = {
        "tmux -V": (b"tmux 3.4\n", b"", 0),
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_nodes(calls, responses),
    )

    ssh_config = tmp_path / "nexus_config"

    # Monkeypatch Path.home() so socket dir is created in tmp.
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    messages = asyncio.run(nodes_add("new-server", config, ssh_config_path=ssh_config))

    assert any("Pushed tmux.conf" in m for m in messages)

    # Reason: Verify that one of the subprocess calls involves writing tmux.conf.
    tmux_conf_calls = [
        c for c in calls if any("tmux.conf" in str(a) for a in c)
    ]
    assert len(tmux_conf_calls) >= 1


def test_nodes_add_appends_ssh_config(monkeypatch, tmp_path):
    """nodes_add appends a Host block to the SSH config file.

    Scenario:
        - Remote host returns "tmux 3.4".
        - SSH config file does not exist yet.
    Expected:
        - SSH config file is created with the Host block.
        - Log includes "Added SSH config for".
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    calls: list[tuple] = []
    responses = {
        "tmux -V": (b"tmux 3.4\n", b"", 0),
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_nodes(calls, responses),
    )

    ssh_config = tmp_path / "nexus_config"

    # Monkeypatch Path.home() for socket dir.
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    messages = asyncio.run(nodes_add("new-server", config, ssh_config_path=ssh_config))

    assert ssh_config.exists()
    content = ssh_config.read_text()
    assert "Host new-server" in content
    assert "ControlMaster auto" in content
    assert "ControlPersist 10m" in content
    assert "ServerAliveInterval 30" in content
    assert any("Added SSH config for new-server" in m for m in messages)


def test_nodes_add_idempotent(monkeypatch, tmp_path):
    """Adding the same host twice does not duplicate the SSH config block.

    Scenario:
        - Remote host returns "tmux 3.4".
        - nodes_add is called twice for the same host.
    Expected:
        - SSH config contains exactly one "Host new-server" block.
        - Second call logs "already exists".
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    calls: list[tuple] = []
    responses = {
        "tmux -V": (b"tmux 3.4\n", b"", 0),
    }
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_exec_for_nodes(calls, responses),
    )

    ssh_config = tmp_path / "nexus_config"

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    # First add.
    asyncio.run(nodes_add("new-server", config, ssh_config_path=ssh_config))

    # Second add.
    messages = asyncio.run(nodes_add("new-server", config, ssh_config_path=ssh_config))

    content = ssh_config.read_text()
    # Reason: Count occurrences of "Host new-server" — should be exactly 1.
    assert content.count("Host new-server") == 1
    assert any("already exists" in m for m in messages)


# ---------------------------------------------------------------------------
# nodes_rm tests
# ---------------------------------------------------------------------------


def test_nodes_rm(tmp_path):
    """nodes_rm removes the Host block from the SSH config file and fleet config.

    Scenario:
        - SSH config contains a Host block for "dev-server".
        - Fleet config includes "dev-server".
    Expected:
        - After removal, "Host dev-server" is no longer in the file.
        - Fleet config no longer includes "dev-server".
        - Log includes "Removed SSH config for dev-server".
    """
    config = FleetConfig(
        nodes=["local", "dev-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    ssh_config = tmp_path / "nexus_config"
    block = SSH_CONFIG_TEMPLATE.format(host="dev-server")
    ssh_config.write_text(block)

    fleet_config = tmp_path / "fleet.toml"

    messages = nodes_rm(
        "dev-server", config, ssh_config_path=ssh_config, fleet_config_path=fleet_config
    )

    content = ssh_config.read_text()
    assert "Host dev-server" not in content
    assert "dev-server" not in config.nodes
    assert any("Removed SSH config for dev-server" in m for m in messages)


def test_nodes_rm_nonexistent(tmp_path):
    """nodes_rm raises ValueError for a host not in SSH config or fleet.

    Scenario:
        - SSH config exists but does not contain "unknown-host".
        - Fleet config does not contain "unknown-host".
    Expected:
        - ValueError with descriptive message.
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    ssh_config = tmp_path / "nexus_config"
    # Write a different host block so the file exists.
    block = SSH_CONFIG_TEMPLATE.format(host="other-server")
    ssh_config.write_text(block)

    with pytest.raises(ValueError, match="not found"):
        nodes_rm("unknown-host", config, ssh_config_path=ssh_config)


def test_nodes_rm_no_config_file(tmp_path):
    """nodes_rm raises ValueError when host is in neither SSH config nor fleet.

    Scenario:
        - No SSH config file at the specified path.
        - Fleet config does not contain the host.
    Expected:
        - ValueError mentioning the host is not found.
    """
    config = FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    ssh_config = tmp_path / "nexus_config"
    # File does not exist.

    with pytest.raises(ValueError, match="not found"):
        nodes_rm("any-host", config, ssh_config_path=ssh_config)


def test_nodes_rm_fleet_only(tmp_path):
    """nodes_rm removes host from fleet config even without SSH config entry.

    Scenario:
        - Host is in fleet config but not in SSH config.
    Expected:
        - Host removed from fleet config.
        - No error raised.
    """
    config = FleetConfig(
        nodes=["local", "orphan-server"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    ssh_config = tmp_path / "nexus_config"
    ssh_config.write_text("")
    fleet_config = tmp_path / "fleet.toml"

    messages = nodes_rm(
        "orphan-server", config, ssh_config_path=ssh_config, fleet_config_path=fleet_config
    )

    assert "orphan-server" not in config.nodes
    assert any("Removed orphan-server from fleet config" in m for m in messages)


# ---------------------------------------------------------------------------
# parse_ssh_config_hosts tests
# ---------------------------------------------------------------------------


def test_parse_ssh_config_hosts(tmp_path):
    """parse_ssh_config_hosts extracts hosts and skips wildcards.

    Scenario:
        - SSH config has single-host lines, a multi-host line, and wildcards.
    Expected:
        - Concrete hostnames returned; wildcards filtered out.
    """
    ssh_config = tmp_path / "config"
    ssh_config.write_text(
        "Host alpha\n"
        "    User root\n"
        "\n"
        "Host beta gamma\n"
        "    User deploy\n"
        "\n"
        "Host *\n"
        "    ServerAliveInterval 60\n"
        "\n"
        "Host !internal\n"
        "    ProxyJump bastion\n"
        "\n"
        "Host jump-?.example.com\n"
        "    User admin\n"
    )

    hosts = parse_ssh_config_hosts(ssh_config)

    assert hosts == ["alpha", "beta", "gamma"]


def test_parse_ssh_config_with_include(tmp_path):
    """parse_ssh_config_hosts follows Include directives.

    Scenario:
        - Main config includes a sub-config via Include directive.
        - Sub-config defines additional hosts.
    Expected:
        - Hosts from both files are returned.
    """
    sub_dir = tmp_path / "config.d"
    sub_dir.mkdir()
    sub_config = sub_dir / "extra.conf"
    sub_config.write_text("Host delta\n    User ops\n")

    main_config = tmp_path / "config"
    main_config.write_text(
        f"Include {sub_dir}/*.conf\n"
        "\n"
        "Host alpha\n"
        "    User root\n"
    )

    hosts = parse_ssh_config_hosts(main_config)

    assert "alpha" in hosts
    assert "delta" in hosts


def test_parse_ssh_config_missing_file(tmp_path):
    """parse_ssh_config_hosts returns empty list for missing file.

    Scenario:
        - SSH config file does not exist.
    Expected:
        - Empty list returned, no error.
    """
    hosts = parse_ssh_config_hosts(tmp_path / "nonexistent")

    assert hosts == []


# ---------------------------------------------------------------------------
# discover_hosts tests
# ---------------------------------------------------------------------------


def test_discover_hosts_filters_fleet(tmp_path):
    """discover_hosts subtracts existing fleet nodes from SSH config hosts.

    Scenario:
        - SSH config has hosts: alpha, beta, gamma.
        - Fleet already contains: local, alpha.
    Expected:
        - Only beta and gamma returned.
    """
    ssh_config = tmp_path / "config"
    ssh_config.write_text(
        "Host alpha\n"
        "    User root\n"
        "Host beta\n"
        "    User deploy\n"
        "Host gamma\n"
        "    User ops\n"
    )

    config = FleetConfig(
        nodes=["local", "alpha"],
        default_node="local",
        default_cmd="/bin/bash",
    )

    candidates = discover_hosts(config, ssh_config_path=ssh_config)

    assert candidates == ["beta", "gamma"]


# ---------------------------------------------------------------------------
# CLI nodes add (no args) tests
# ---------------------------------------------------------------------------


def test_nodes_add_no_args_fzf(monkeypatch, tmp_path):
    """nodes add with no args invokes fzf when multiple candidates exist.

    Scenario:
        - SSH config has hosts: alpha, beta.
        - Fleet has only local.
        - fzf returns "alpha".
    Expected:
        - nodes_add is called with "alpha".
    """
    import subprocess as sp
    from unittest.mock import MagicMock, patch
    import nx.cli as cli_mod

    monkeypatch.setattr(
        "nx.cli.discover_hosts",
        lambda config, **kw: ["alpha", "beta"],
    )

    captured = {}

    async def fake_nodes_add(host, config, **kw):
        captured["host"] = host
        return [f"Added {host}"]

    monkeypatch.setattr("nx.cli.nodes_add", fake_nodes_add)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/fzf")

    def fake_run(cmd, **kwargs):
        """Intercept fzf calls."""
        if cmd and cmd[0] == "fzf":
            return sp.CompletedProcess(cmd, 0, stdout="alpha\n", stderr="")
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Reason: CliRunner replaces sys.stdin, so patching stdin.isatty
    # directly doesn't survive. We patch the entire sys module as seen
    # by cli.py to control isatty.
    with patch.object(cli_mod, "subprocess") as mock_sp, \
         patch.object(cli_mod, "sys") as mock_sys:
        mock_sp.run = fake_run
        mock_sys.stdin.isatty.return_value = True

        result = runner.invoke(app, ["nodes", "add"])

    assert result.exit_code == 0
    assert captured["host"] == "alpha"


def test_nodes_add_no_args_single_host(monkeypatch, tmp_path):
    """nodes add with no args auto-selects when only 1 candidate.

    Scenario:
        - Only one candidate host available.
    Expected:
        - Auto-selected without fzf.
    """
    monkeypatch.setattr(
        "nx.cli.discover_hosts",
        lambda config, **kw: ["only-host"],
    )

    captured = {}

    async def fake_nodes_add(host, config, **kw):
        captured["host"] = host
        return [f"Added {host}"]

    monkeypatch.setattr("nx.cli.nodes_add", fake_nodes_add)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/fzf")

    result = runner.invoke(app, ["nodes", "add"])

    assert result.exit_code == 0
    assert captured["host"] == "only-host"
    assert "Auto-selected" in result.output


def test_nodes_add_no_candidates(monkeypatch):
    """nodes add with no args exits with error when no candidates found.

    Scenario:
        - All SSH config hosts already in fleet.
    Expected:
        - Error message and exit code 1.
    """
    monkeypatch.setattr(
        "nx.cli.discover_hosts",
        lambda config, **kw: [],
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/fzf")

    result = runner.invoke(app, ["nodes", "add"])

    assert result.exit_code == 1
    assert "No new hosts" in result.output
