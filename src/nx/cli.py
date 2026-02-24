"""Nexus CLI entry point."""

import asyncio
import os
import shutil
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from nx import __version__
from nx.config import FleetConfig, load_config
from nx.ssh import fan_out, run_on_node
from nx.tmux import build_list_cmd, build_new_cmd, parse_list_output

app = typer.Typer(
    name="nx",
    help="nx — Distributed terminal orchestration.",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"nx {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """nx — Distributed terminal orchestration."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config()
    if not shutil.which("fzf"):
        typer.echo("Error: fzf is required but not found on $PATH.", err=True)
        raise typer.Exit(code=1)


@app.command("list")
def list_sessions(ctx: typer.Context) -> None:
    """List tmux sessions across all fleet nodes.

    Queries every node in the fleet for active nexus-managed tmux sessions
    and displays them in a table grouped by node. Unreachable nodes are
    flagged in the Status column.
    """
    config: FleetConfig = ctx.obj["config"]

    # Fan out the list command to all nodes concurrently.
    results = asyncio.run(
        fan_out(config.nodes, build_list_cmd(), max_concurrent=config.max_concurrent_ssh)
    )

    # Classify nodes into reachable (with parsed sessions) and unreachable.
    node_sessions: dict[str, list] = {}
    unreachable_nodes: list[str] = []

    for node in config.nodes:
        result = results[node]
        if result.returncode != 0:
            unreachable_nodes.append(node)
        else:
            sessions = parse_list_output(result.stdout)
            node_sessions[node] = sessions

    # Count total sessions across all reachable nodes.
    total_sessions = sum(len(s) for s in node_sessions.values())

    # If no sessions anywhere and no unreachable nodes, print a simple message.
    if total_sessions == 0 and not unreachable_nodes:
        console.print("No active sessions.")
        return

    # Build the Rich table.
    table = Table()
    table.add_column("Node")
    table.add_column("Session")
    table.add_column("Directory")
    table.add_column("Command")
    table.add_column("Status")

    # Render reachable nodes with their sessions.
    for node, sessions in node_sessions.items():
        for session in sessions:
            if session.is_dead:
                status = f"[EXITED {session.exit_status}]"
            else:
                status = "[RUNNING]"
            table.add_row(
                node,
                session.name,
                session.pane_path,
                session.pane_cmd,
                status,
            )

    # Render unreachable nodes.
    for node in unreachable_nodes:
        table.add_row(node, "", "", "", "[UNREACHABLE]")

    console.print(table)


@app.command("new")
def new_session(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Session name."),
    cmd: Optional[list[str]] = typer.Argument(None, help="Command to run in the session."),
    on: Optional[str] = typer.Option(None, "--on", help="Target node."),
    directory: Optional[str] = typer.Option(None, "--dir", "-d", help="Working directory."),
) -> None:
    """Create a new tmux session on a fleet node.

    Creates a nexus-managed tmux session with the given name. The session
    can target any node in the fleet and optionally run a specific command
    in a chosen working directory.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        name: Name for the new tmux session.
        cmd: Command to run inside the session. Defaults to the shell
            configured in the fleet config.
        on: Target node. Defaults to the fleet's default_node.
        directory: Working directory for the session. For local nodes
            defaults to the current directory; for remote nodes defaults
            to the remote user's $HOME.
    """
    config: FleetConfig = ctx.obj["config"]

    # Determine target node.
    node = on or config.default_node

    # Determine working directory.
    # Reason: Local sessions should inherit the caller's cwd for convenience,
    # while remote sessions default to $HOME (tmux's own default) since the
    # local cwd is unlikely to exist on the remote host.
    if directory is not None:
        session_dir = directory
    elif node == "local":
        session_dir = os.getcwd()
    else:
        session_dir = None

    # Determine command.
    session_cmd = " ".join(cmd) if cmd else config.default_cmd

    # Build and execute the tmux new-session command.
    tmux_cmd = build_new_cmd(name, cmd=session_cmd, directory=session_dir)
    result = asyncio.run(run_on_node(node, tmux_cmd))

    if result.returncode != 0:
        if "duplicate session" in (result.stderr or ""):
            console.print(f"Error: Session '{name}' already exists on {node}.")
        else:
            console.print(f"Error: {result.stderr}")
        raise typer.Exit(code=1)

    console.print(f"Created session {node}/{name}")


# Alias: nx l → nx list
app.command("l", hidden=True)(list_sessions)
