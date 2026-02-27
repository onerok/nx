"""Integration test fixtures for real tmux testing."""

import asyncio

import pytest

from nx.config import FleetConfig
from nx.ssh import run_on_node

# The integration test socket name. All integration tests use this
# dedicated tmux socket to avoid interfering with the user's real sessions.
TEST_SOCKET = "nx_test"


@pytest.fixture(autouse=True, scope="session")
def clean_tmux():
    """Kill all sessions on the nx_test tmux socket before and after tests.

    Guarantees no leaked state between test runs.
    """
    asyncio.run(run_on_node("local", ["tmux", "-L", TEST_SOCKET, "kill-server"]))
    yield
    asyncio.run(run_on_node("local", ["tmux", "-L", TEST_SOCKET, "kill-server"]))


@pytest.fixture
def nx_test_config(monkeypatch):
    """Return a FleetConfig using the nx_test socket.

    Patches nx.tmux.SOCKET_NAME so that all tmux command builders
    (build_list_cmd, build_new_cmd, etc.) use the test socket.
    """
    import nx.tmux

    monkeypatch.setattr(nx.tmux, "SOCKET_NAME", TEST_SOCKET)

    return FleetConfig(
        nodes=["local"],
        default_node="local",
        default_cmd="/bin/bash",
    )
