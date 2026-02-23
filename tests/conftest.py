"""Shared test fixtures for Nexus test suite."""

import pytest


@pytest.fixture
def mock_ssh():
    """Mock SSH command execution.

    Will be replaced with a configurable fake that returns
    predetermined stdout/stderr/returncode based on command patterns.
    """

    class MockSSH:
        def __init__(self):
            self.calls = []
            self._responses = {}

        def register(self, pattern: str, stdout: str = "", stderr: str = "", returncode: int = 0):
            self._responses[pattern] = (stdout, stderr, returncode)

    yield MockSSH()


@pytest.fixture
def tmp_config(tmp_path):
    """Create a temporary fleet.toml config.

    Args:
        tmp_path: pytest built-in fixture for temp directory.

    Returns:
        Path to temporary config directory.
    """
    config_dir = tmp_path / "nexus"
    config_dir.mkdir()
    config_file = config_dir / "fleet.toml"
    config_file.write_text(
        '[fleet]\nnodes = ["local"]\ndefault_node = "local"\ndefault_cmd = "/bin/bash"\n'
    )
    yield config_dir


@pytest.fixture
def mock_fzf():
    """Mock fzf interactive selection.

    Will be replaced with a fake that intercepts subprocess.run
    for fzf calls and returns a configured selection.
    """

    class MockFzf:
        def __init__(self):
            self.selection = None

        def set_selection(self, value: str):
            self.selection = value

    yield MockFzf()


@pytest.fixture
def mock_execvp():
    """Mock os.execvp to capture process replacement calls.

    Records (executable, args) instead of actually replacing the process.
    """

    class MockExecvp:
        def __init__(self):
            self.calls = []

        def __call__(self, executable: str, args: list[str]):
            self.calls.append((executable, args))

    yield MockExecvp()
