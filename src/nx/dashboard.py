"""Dashboard: CCTV-style read-only view of all fleet sessions."""

import shutil
import sys
from typing import TYPE_CHECKING

from nx.ssh import fan_out, run_on_node
from nx.tmux import build_list_cmd, parse_list_output

if TYPE_CHECKING:
    from nx.config import FleetConfig
    from nx.tmux import SessionInfo


DASH_SOCKET = "nx_dash"
DASH_SESSION = "dashboard"


async def build_dashboard(config: "FleetConfig") -> list[str]:
    """Build the dashboard tmux session with read-only panes.

    Steps:
    1. Fan-out list all sessions across the fleet.
    2. Create a temporary tmux session on the nx_dash socket.
    3. For each session, split a new pane with a read-only attach command.
    4. Tag each pane with @nx_target metadata.
    5. Store NX_BIN path via set-environment.
    6. Bind Enter to the teardown-and-attach shim.
    7. Select even-vertical layout and select the first pane.

    Args:
        config: Fleet configuration.

    Returns:
        list[str]: The execvp arguments to attach to the dashboard.
            Returns empty list if no active sessions found.
    """
    # Step 1: Fan out list command to all nodes.
    results = await fan_out(
        config.nodes, build_list_cmd(), max_concurrent=config.max_concurrent_ssh
    )

    # Collect all active (non-dead) sessions as (node, SessionInfo) pairs.
    all_sessions: list[tuple[str, "SessionInfo"]] = []
    for node in config.nodes:
        result = results[node]
        if result.returncode != 0:
            continue
        for info in parse_list_output(result.stdout):
            if not info.is_dead:
                all_sessions.append((node, info))

    if not all_sessions:
        return []

    # Step 2: Create the dashboard tmux session.
    # Reason: The first session's read-only attach becomes the initial window
    # command so the dashboard session starts with at least one pane.
    first_node, first_info = all_sessions[0]
    first_attach = _build_attach_cmd(first_node, first_info.name)

    await run_on_node("local", [
        "tmux", "-L", DASH_SOCKET, "new-session", "-d", "-s", DASH_SESSION,
        *first_attach,
    ])

    # Tag the first pane with @nx_target metadata.
    first_target = f"{first_node}/{first_info.name}"
    await run_on_node("local", [
        "tmux", "-L", DASH_SOCKET, "set-option", "-p", "-t", f"{DASH_SESSION}:0.0",
        "@nx_target", first_target,
    ])

    # Step 3-4: Split window for remaining sessions and tag each pane.
    for node, info in all_sessions[1:]:
        attach_cmd = _build_attach_cmd(node, info.name)
        await run_on_node("local", [
            "tmux", "-L", DASH_SOCKET, "split-window", "-t", DASH_SESSION,
            *attach_cmd,
        ])

        target = f"{node}/{info.name}"
        await run_on_node("local", [
            "tmux", "-L", DASH_SOCKET, "set-option", "-p",
            "@nx_target", target,
        ])

    # Step 5: Store NX_BIN path via set-environment.
    # Reason: The Enter-key shim needs to locate the nx binary at runtime.
    # shutil.which("nx") finds the installed entry point; sys.argv[0] is the
    # fallback for development invocations (e.g. `uv run nx`).
    nx_bin = shutil.which("nx") or sys.argv[0]
    await run_on_node("local", [
        "tmux", "-L", DASH_SOCKET, "set-environment", "NX_BIN", nx_bin,
    ])

    # Step 6: Bind Enter to the teardown-and-attach shim.
    # Reason: The shim captures the target from pane metadata, tears down the
    # dashboard, and execs `nx attach` in the user's original terminal context.
    shim = (
        "NX_BIN=$(tmux -L nx_dash show-environment -h NX_BIN | cut -d= -f2); "
        "TARGET=$(tmux -L nx_dash display-message -p '#{@nx_target}'); "
        "tmux -L nx_dash detach-client && tmux -L nx_dash kill-session; "
        'exec "$NX_BIN" attach "$TARGET"'
    )
    await run_on_node("local", [
        "tmux", "-L", DASH_SOCKET, "bind-key", "-n", "Enter",
        "run-shell", shim,
    ])

    # Step 7: Apply even-vertical layout so all panes are visible.
    await run_on_node("local", [
        "tmux", "-L", DASH_SOCKET, "select-layout", "-t", DASH_SESSION,
        "even-vertical",
    ])

    # Select first pane so the user starts at the top.
    await run_on_node("local", [
        "tmux", "-L", DASH_SOCKET, "select-pane", "-t", f"{DASH_SESSION}:0.0",
    ])

    # Return the execvp args for attaching to the dashboard.
    return ["tmux", "-L", DASH_SOCKET, "attach", "-t", DASH_SESSION]


def _build_attach_cmd(node: str, session: str) -> list[str]:
    """Build a read-only attach command for a session.

    For local sessions: tmux -L nexus attach -t <session> -r
    For remote sessions: ssh -t <node> tmux -L nexus attach -t <session> -r

    Args:
        node: Node hostname.
        session: Session name.

    Returns:
        list[str]: Command arguments for read-only attach.
    """
    if node == "local":
        return ["tmux", "-L", "nexus", "attach", "-t", session, "-r"]
    else:
        return ["ssh", "-t", node, "tmux", "-L", "nexus", "attach", "-t", session, "-r"]
