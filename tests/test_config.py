"""Tests for FleetConfig loading, validation, and env-var expansion (Milestone 1)."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from nx.config import FleetConfig, load_config


def test_load_valid_config(tmp_path: Path) -> None:
    """Load a TOML file with all fields explicitly set to non-default values.

    Args:
        tmp_path: pytest built-in fixture for a temporary directory.

    Asserts:
        Every field on the returned FleetConfig matches what was written.
    """
    config_file = tmp_path / "fleet.toml"
    config_file.write_text(
        'nodes = ["local", "dev-server", "gpu-rig"]\n'
        'default_node = "dev-server"\n'
        'default_cmd = "/usr/bin/zsh"\n'
        "max_concurrent_ssh = 8\n"
        "auto_reap_clean_exit = false\n"
    )

    config = load_config(config_file)

    assert config.nodes == ["local", "dev-server", "gpu-rig"]
    assert config.default_node == "dev-server"
    assert config.default_cmd == "/usr/bin/zsh"
    assert config.max_concurrent_ssh == 8
    assert config.auto_reap_clean_exit is False


def test_expand_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that $SHELL and other env vars are expanded in string fields.

    Args:
        tmp_path: pytest built-in fixture for a temporary directory.
        monkeypatch: pytest fixture for patching environment variables.

    Asserts:
        default_cmd is expanded from '$SHELL' to the actual shell path.
        default_node is expanded when it contains an env var.
    """
    # Reason: Set a known env var so the test is deterministic regardless of host.
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    monkeypatch.setenv("NX_TEST_NODE", "staging-box")

    config_file = tmp_path / "fleet.toml"
    config_file.write_text(
        'default_cmd = "$SHELL"\n'
        'default_node = "$NX_TEST_NODE"\n'
    )

    config = load_config(config_file)

    assert config.default_cmd == "/usr/bin/fish"
    assert config.default_node == "staging-box"


def test_missing_config_uses_defaults() -> None:
    """Calling load_config with a nonexistent path returns all defaults.

    Asserts:
        nodes defaults to ["local"].
        default_node defaults to "local".
        default_cmd defaults to "$SHELL" (literal; pydantic v2 field_validator
        with mode='before' does not fire for field defaults -- expansion only
        happens when a value is explicitly supplied via TOML or constructor kwargs).
        max_concurrent_ssh defaults to 16.
        auto_reap_clean_exit defaults to True.
    """
    config = load_config(Path("/nonexistent/path/fleet.toml"))

    assert config.nodes == ["local"]
    assert config.default_node == "local"
    # Reason: pydantic v2 field_validator(mode="before") only triggers on
    # values explicitly passed to the model, not on field defaults. So when
    # no config file exists, FleetConfig() keeps the literal "$SHELL".
    assert config.default_cmd == "$SHELL"
    assert config.max_concurrent_ssh == 16
    assert config.auto_reap_clean_exit is True


def test_invalid_config_raises(tmp_path: Path) -> None:
    """A TOML file with wrong types raises pydantic ValidationError.

    Args:
        tmp_path: pytest built-in fixture for a temporary directory.

    Asserts:
        load_config raises ValidationError for a non-integer max_concurrent_ssh.
    """
    config_file = tmp_path / "fleet.toml"
    config_file.write_text('max_concurrent_ssh = "not_a_number"\n')

    with pytest.raises(ValidationError):
        load_config(config_file)


def test_local_always_in_nodes(tmp_path: Path) -> None:
    """Even when 'local' is omitted from the nodes list, it is inserted at index 0.

    Args:
        tmp_path: pytest built-in fixture for a temporary directory.

    Asserts:
        'local' is present in config.nodes.
        'local' is at index 0.
        The original nodes follow after 'local'.
    """
    config_file = tmp_path / "fleet.toml"
    config_file.write_text('nodes = ["dev-server", "gpu-rig"]\n')

    config = load_config(config_file)

    assert "local" in config.nodes
    assert config.nodes[0] == "local"
    # Reason: Ensure the user-specified nodes are preserved after "local".
    assert "dev-server" in config.nodes
    assert "gpu-rig" in config.nodes
