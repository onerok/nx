"""Nexus CLI entry point."""

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from nx import __version__
from nx.config import FleetConfig, load_config
from nx.ssh import fan_out
from nx.tmux import build_list_cmd, parse_list_output

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


# Alias: nx l → nx list
app.command("l", hidden=True)(list_sessions)
