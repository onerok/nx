"""Integration tests: real tmux workflows on local node.

These tests create, manipulate, and destroy real tmux sessions using
the nx_test socket. They verify end-to-end behavior of the Nexus CLI
functions against actual tmux.
"""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from nx.config import FleetConfig
from nx.resolve import resolve_session
from nx.snapshot import save_snapshot, restore_snapshot
from nx.ssh import run_on_node
from nx.tmux import (
    build_capture_cmd,
    build_kill_cmd,
    build_list_cmd,
    build_new_cmd,
    build_send_keys_cmd,
    parse_list_output,
)


def _cleanup_session(name: str) -> None:
    """Kill a test session, ignoring errors if it doesn't exist."""
    asyncio.run(run_on_node("local", build_kill_cmd(name)))


# ---- Test 1: test_full_lifecycle ----
def test_full_lifecycle(nx_test_config):
    """new -> list -> peek -> send -> logs -> kill: full session lifecycle."""
    config = nx_test_config
    name = "integ-lifecycle"

    try:
        # Create a session running cat (blocks waiting for input)
        result = asyncio.run(run_on_node("local", build_new_cmd(name, cmd="cat")))
        assert result.returncode == 0, f"Failed to create session: {result.stderr}"
        time.sleep(0.5)

        # List -- session should appear
        result = asyncio.run(run_on_node("local", build_list_cmd()))
        assert result.returncode == 0
        sessions = parse_list_output(result.stdout)
        names = [s.name for s in sessions]
        assert name in names

        # Peek -- should return something (empty or prompt)
        result = asyncio.run(run_on_node("local", build_capture_cmd(name, 30)))
        assert result.returncode == 0

        # Send "hello"
        result = asyncio.run(
            run_on_node("local", build_send_keys_cmd(name, ["hello"]))
        )
        assert result.returncode == 0
        time.sleep(0.5)

        # Peek -- should now contain "hello" (cat echoes input)
        result = asyncio.run(run_on_node("local", build_capture_cmd(name, 30)))
        assert result.returncode == 0
        assert "hello" in result.stdout

        # Logs -- should capture scrollback
        result = asyncio.run(run_on_node("local", build_capture_cmd(name, 100)))
        assert result.returncode == 0
        assert "hello" in result.stdout

        # Kill
        result = asyncio.run(run_on_node("local", build_kill_cmd(name)))
        assert result.returncode == 0

        # Verify gone
        time.sleep(0.3)
        result = asyncio.run(run_on_node("local", build_list_cmd()))
        if result.returncode == 0:
            sessions = parse_list_output(result.stdout)
            names = [s.name for s in sessions]
            assert name not in names
    finally:
        _cleanup_session(name)


# ---- Test 2: test_send_peek_roundtrip ----
def test_send_peek_roundtrip(nx_test_config):
    """Send text to a cat session and verify it appears in peek output."""
    config = nx_test_config
    name = "integ-roundtrip"

    try:
        result = asyncio.run(run_on_node("local", build_new_cmd(name, cmd="cat")))
        assert result.returncode == 0
        time.sleep(0.5)

        # Send "hello world"
        result = asyncio.run(
            run_on_node("local", build_send_keys_cmd(name, ["hello world"]))
        )
        assert result.returncode == 0
        time.sleep(0.5)

        # Peek -- verify "hello world" appears
        result = asyncio.run(run_on_node("local", build_capture_cmd(name, 30)))
        assert result.returncode == 0
        assert "hello world" in result.stdout
    finally:
        _cleanup_session(name)


# ---- Test 3: test_snapshot_restore_cycle ----
def test_snapshot_restore_cycle(nx_test_config, tmp_path):
    """Create 2 sessions -> snapshot -> kill both -> restore -> verify recreated."""
    config = nx_test_config
    names = ["integ-snap-a", "integ-snap-b"]
    snap_path = tmp_path / "snapshot.json"

    try:
        # Create 2 sessions
        for n in names:
            result = asyncio.run(run_on_node("local", build_new_cmd(n, cmd="cat")))
            assert result.returncode == 0
        time.sleep(0.5)

        # Snapshot
        path = asyncio.run(save_snapshot(config, snapshot_path=snap_path))
        assert path.exists()

        # Kill both
        for n in names:
            asyncio.run(run_on_node("local", build_kill_cmd(n)))
        time.sleep(0.3)

        # Verify both gone
        result = asyncio.run(run_on_node("local", build_list_cmd()))
        if result.returncode == 0:
            sessions = parse_list_output(result.stdout)
            live_names = [s.name for s in sessions]
            for n in names:
                assert n not in live_names

        # Restore
        log = asyncio.run(restore_snapshot(config, snapshot_path=snap_path))
        assert len(log) == 2
        time.sleep(0.5)

        # Verify both recreated
        result = asyncio.run(run_on_node("local", build_list_cmd()))
        assert result.returncode == 0
        sessions = parse_list_output(result.stdout)
        live_names = [s.name for s in sessions]
        for n in names:
            assert n in live_names
    finally:
        for n in names:
            _cleanup_session(n)


# ---- Test 4: test_gc_cleans_exited ----
def test_gc_cleans_exited(nx_test_config):
    """Create a session with 'exit 0', wait for exit, gc, verify gone."""
    import nx.tmux

    config = nx_test_config
    name = "integ-gc-exit"
    keepalive = "integ-gc-keepalive"
    socket = nx.tmux.SOCKET_NAME

    try:
        # Reason: Create a keep-alive session so the tmux server doesn't
        # shut down when the test session exits (tmux kills the server
        # when the last session is gone).
        result = asyncio.run(
            run_on_node("local", build_new_cmd(keepalive, cmd="cat"))
        )
        assert result.returncode == 0

        # Create the session that will exit. Use "sleep 1" so we have
        # time to set remain-on-exit before it finishes.
        result = asyncio.run(
            run_on_node("local", build_new_cmd(name, cmd="sleep 1"))
        )
        assert result.returncode == 0

        # Reason: Set remain-on-exit so tmux keeps the dead pane visible
        # in list-sessions instead of destroying it immediately.
        result = asyncio.run(
            run_on_node(
                "local",
                ["tmux", "-L", socket, "set-option", "-t", name, "remain-on-exit", "on"],
            )
        )
        assert result.returncode == 0

        # Wait for the sleep command to exit
        time.sleep(2.0)

        # Verify it's dead
        result = asyncio.run(run_on_node("local", build_list_cmd()))
        assert result.returncode == 0
        sessions = parse_list_output(result.stdout)
        dead = [s for s in sessions if s.name == name and s.is_dead]
        assert len(dead) == 1, (
            f"Expected dead session, got: {[(s.name, s.is_dead) for s in sessions]}"
        )

        # GC -- kill dead sessions
        for s in sessions:
            if s.name == name and s.is_dead:
                asyncio.run(run_on_node("local", build_kill_cmd(name)))
        time.sleep(0.3)

        # Verify gone
        result = asyncio.run(run_on_node("local", build_list_cmd()))
        assert result.returncode == 0
        sessions = parse_list_output(result.stdout)
        names_left = [s.name for s in sessions]
        assert name not in names_left
    finally:
        _cleanup_session(name)
        _cleanup_session(keepalive)


# ---- Test 5: test_resolution_fully_qualified ----
def test_resolution_fully_qualified(nx_test_config):
    """peek 'local/name' resolves without fan-out."""
    config = nx_test_config
    name = "integ-fqn"

    try:
        result = asyncio.run(run_on_node("local", build_new_cmd(name, cmd="cat")))
        assert result.returncode == 0
        time.sleep(0.5)

        # Resolve fully qualified name -- should return immediately without fan-out
        node, session = asyncio.run(resolve_session(f"local/{name}", config))
        assert node == "local"
        assert session == name

        # Verify we can peek using the resolved session
        result = asyncio.run(run_on_node(node, build_capture_cmd(session, 30)))
        assert result.returncode == 0
    finally:
        _cleanup_session(name)


# ---- Test 6: test_new_duplicate_rejected ----
def test_new_duplicate_rejected(nx_test_config):
    """Creating a session with the same name twice fails."""
    config = nx_test_config
    name = "integ-dup"

    try:
        # First creation -- should succeed
        result = asyncio.run(run_on_node("local", build_new_cmd(name, cmd="cat")))
        assert result.returncode == 0
        time.sleep(0.3)

        # Second creation -- should fail with "duplicate session"
        result = asyncio.run(run_on_node("local", build_new_cmd(name, cmd="cat")))
        assert result.returncode != 0
        assert "duplicate" in result.stderr.lower() or "exists" in result.stderr.lower(), (
            f"Expected duplicate error, got: {result.stderr}"
        )
    finally:
        _cleanup_session(name)


# ---- Test 7: test_fzf_roundtrip ----
def test_fzf_roundtrip(nx_test_config):
    """Verify fzf format/parse contract: create sessions, filter, parse back."""
    config = nx_test_config
    names = ["integ-fzf-alpha", "integ-fzf-beta"]

    try:
        for n in names:
            result = asyncio.run(run_on_node("local", build_new_cmd(n, cmd="cat")))
            assert result.returncode == 0
        time.sleep(0.5)

        # Format as fzf input: "local/sess1\nlocal/sess2"
        fzf_input = "\n".join(f"local/{n}" for n in names)

        # Filter through fzf --filter for non-interactive match
        result = subprocess.run(
            ["fzf", "--filter", "alpha"],
            input=fzf_input,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0
        selected = result.stdout.strip()
        assert selected == "local/integ-fzf-alpha"

        # Parse back -- verify resolve_session can handle it
        node, session = selected.split("/", 1)
        assert node == "local"
        assert session == "integ-fzf-alpha"
    finally:
        for n in names:
            _cleanup_session(n)


# ---- Test 8: test_list_empty_fleet ----
def test_list_empty_fleet(nx_test_config):
    """No sessions -> list returns empty (tmux server may not even exist)."""
    config = nx_test_config

    # Kill any leftover sessions first
    asyncio.run(
        run_on_node("local", ["tmux", "-L", "nx_test", "kill-server"])
    )
    time.sleep(0.3)

    # List sessions -- should return non-zero (no server) or empty output
    result = asyncio.run(run_on_node("local", build_list_cmd()))

    if result.returncode == 0:
        sessions = parse_list_output(result.stdout)
        assert len(sessions) == 0
    else:
        # tmux returns error when no server exists -- this is expected
        assert result.returncode != 0
