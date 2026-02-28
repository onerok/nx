"""Fleet configuration loading and validation."""

import os
from pathlib import Path

import tomllib
from pydantic import BaseModel, field_validator, model_validator


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "nexus" / "fleet.toml"


class FleetConfig(BaseModel):
    """Fleet configuration model.

    Attributes:
        nodes: List of nodes in the fleet. "local" is always included.
        default_node: Default target for 'nx new' if --on is omitted.
        default_cmd: Default command if none specified. Supports env var expansion.
        max_concurrent_ssh: Max concurrent SSH connections during fan-out.
        auto_reap_clean_exit: Auto-delete panes that exit with code 0.
    """

    nodes: list[str] = ["local"]
    default_node: str = "local"
    default_cmd: str = "$SHELL"
    max_concurrent_ssh: int = 16
    auto_reap_clean_exit: bool = True

    @field_validator("default_node", "default_cmd", mode="before")
    @classmethod
    def expand_env_vars(cls, v: str) -> str:
        """Expand environment variables in string fields.

        Args:
            v: Raw string value that may contain env var references.

        Returns:
            str: String with env vars expanded.
        """
        return os.path.expandvars(v)

    @model_validator(mode="after")
    def ensure_local_in_nodes(self) -> "FleetConfig":
        """Ensure 'local' is always present in the nodes list.

        Returns:
            FleetConfig: The validated config with 'local' guaranteed in nodes.
        """
        if "local" not in self.nodes:
            self.nodes.insert(0, "local")
        return self


def load_config(path: Path | None = None) -> FleetConfig:
    """Load fleet configuration from TOML file.

    Reads the fleet config from the given path (or the default
    ~/.config/nexus/fleet.toml). If the file doesn't exist,
    returns a FleetConfig with default values.

    Args:
        path: Path to the config file. Defaults to ~/.config/nexus/fleet.toml.

    Returns:
        FleetConfig: The loaded and validated configuration.

    Raises:
        pydantic.ValidationError: If the config file contains invalid values.
    """
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        return FleetConfig()

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    return FleetConfig(**data)


def save_config(config: FleetConfig, path: Path | None = None) -> None:
    """Save fleet configuration to TOML file.

    Writes the fleet config to the given path (or the default
    ~/.config/nexus/fleet.toml). Creates parent directories if needed.

    Args:
        config: Fleet configuration to save.
        path: Path to the config file. Defaults to ~/.config/nexus/fleet.toml.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Reason: We write TOML manually to avoid adding a tomli_w dependency.
    # The fleet config schema is flat and simple.
    nodes_str = ", ".join(f'"{n}"' for n in config.nodes)
    lines = [
        f"nodes = [{nodes_str}]",
        f'default_node = "{config.default_node}"',
        f'default_cmd = "{config.default_cmd}"',
        f"max_concurrent_ssh = {config.max_concurrent_ssh}",
        f"auto_reap_clean_exit = {'true' if config.auto_reap_clean_exit else 'false'}",
    ]
    config_path.write_text("\n".join(lines) + "\n")
