"""Nexus CLI entry point."""

import asyncio
import os
import shutil
import subprocess
import sys
from typing import Optional

import typer
from coolname import generate_slug
from rich.console import Console
from rich.table import Table

from nx import __version__
from nx.config import FleetConfig, load_config
from nx.ssh import fan_out, run_on_node
from nx.resolve import AmbiguousSession, SessionNotFound, resolve_session
from nx.nodes import nodes_ls, nodes_add, nodes_rm, discover_hosts
from nx.tmux import build_list_cmd, build_new_cmd, build_capture_cmd, build_send_keys_cmd, build_kill_cmd, parse_list_output
from nx.snapshot import save_snapshot, restore_snapshot
from nx.dashboard import build_dashboard


_PICK_NODE = "__pick__"


class _OptionalOnCommand(typer.core.TyperCommand):
    """Typer command that allows --on without a value.

    Reason: Typer/Click requires --on to have a value. This subclass
    intercepts arg parsing and injects a sentinel when --on appears
    without a following value, so the fzf picker is triggered.
    """

    def parse_args(self, ctx, args):
        """Inject sentinel when --on has no value."""
        args = list(args)
        for i, arg in enumerate(args):
            if arg == "--on":
                # Reason: --on at end of args or followed by another flag
                # means the user wants the interactive picker.
                if i + 1 >= len(args) or args[i + 1].startswith("-"):
                    args.insert(i + 1, _PICK_NODE)
                break
        return super().parse_args(ctx, args)


def _stdin_is_tty() -> bool:
    """Check if stdin is an interactive terminal.

    Returns:
        bool: True if stdin is a tty.
    """
    return sys.stdin.isatty()

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
            stderr = result.stderr or ""
            # Reason: tmux exits non-zero when the nexus socket doesn't exist
            # (no sessions created yet). This is "zero sessions", not unreachable.
            if "no server running" in stderr or "No such file" in stderr:
                node_sessions[node] = []
            else:
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


@app.command("new", cls=_OptionalOnCommand)
def new_session(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Session name (auto-generated if omitted)."),
    cmd: Optional[list[str]] = typer.Argument(None, help="Command to run in the session."),
    on: Optional[str] = typer.Option(None, "--on", help="Target node. Use --on without a value to pick interactively."),
    directory: Optional[str] = typer.Option(None, "--dir", "-d", help="Working directory."),
    detach: bool = typer.Option(False, "--detach", "-D", help="Create session without attaching."),
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

    # Reason: Generate a random slug when the user omits the session name.
    if name is None:
        name = generate_slug(2)

    # Determine target node.
    # Reason: _PICK_NODE sentinel means --on was used without a value,
    # so we always show the fzf picker regardless of tty state.
    pick_node = on == _PICK_NODE

    if on and not pick_node:
        node = on
    elif len(config.nodes) == 1:
        node = config.nodes[0]
    elif pick_node or _stdin_is_tty():
        # Reason: Put default_node first so it's pre-highlighted in fzf.
        sorted_nodes = sorted(
            config.nodes, key=lambda n: (n != config.default_node, n)
        )
        result = subprocess.run(
            ["fzf", "--prompt", "Select node: "],
            input="\n".join(sorted_nodes),
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            console.print("Selection cancelled.")
            raise typer.Exit(code=1)
        node = result.stdout.strip()
    else:
        node = config.default_node

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

    if not detach:
        _attach_to_session(node, name)


def _attach_to_session(node: str, session: str) -> None:
    """Attach to a tmux session on the given node.

    Selects the attach strategy based on whether the caller is inside
    a tmux session:
        A — bare terminal: replaces the process via execvp.
        B — inside nexus tmux: switch-client or new-window with SSH.
        C — inside user's personal tmux: new-window nesting into nexus.

    Args:
        node: Fleet node hosting the session.
        session: tmux session name.
    """
    tmux_env = os.environ.get("TMUX", "")

    if not tmux_env:
        # Scenario A: Bare terminal — execvp replaces the current process.
        if node == "local":
            os.execvp("tmux", ["tmux", "-L", "nexus", "attach", "-t", session])
        else:
            os.execvp(
                "ssh", ["ssh", "-t", node, "tmux", "-L", "nexus", "attach", "-t", session]
            )
    elif "nexus" in tmux_env:
        # Scenario B: Inside nexus tmux — stay in the same tmux server.
        if node == "local":
            subprocess.run(
                ["tmux", "-L", "nexus", "switch-client", "-t", session]
            )
        else:
            subprocess.run(
                [
                    "tmux", "-L", "nexus", "new-window", "-n", session,
                    "ssh", "-t", node, "tmux", "-L", "nexus", "attach", "-t", session,
                ]
            )
        raise typer.Exit()
    else:
        # Scenario C: Inside user's personal tmux — open a new window
        # that nests into the nexus session.
        if node == "local":
            subprocess.run(
                [
                    "tmux", "new-window", "-n", session,
                    "tmux", "-L", "nexus", "attach", "-t", session,
                ]
            )
        else:
            subprocess.run(
                [
                    "tmux", "new-window", "-n", session,
                    "ssh", "-t", node, "tmux", "-L", "nexus", "attach", "-t", session,
                ]
            )
        raise typer.Exit()


@app.command("attach")
def attach_session(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Session name (bare or node/session)."),
) -> None:
    """Attach to an existing tmux session on a fleet node.

    Resolves the target session (by bare name or fully qualified node/session)
    and attaches using the appropriate strategy based on whether the caller is
    already inside a tmux session.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        name: Session name, either bare ("api") or fully qualified ("dev/api").
    """
    config: FleetConfig = ctx.obj["config"]

    # Resolve the session name to a (node, session) tuple.
    try:
        node, session = asyncio.run(resolve_session(name, config))
    except SessionNotFound as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)
    except AmbiguousSession as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    _attach_to_session(node, session)


@app.command("peek")
def peek_session(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Session name (bare or node/session)."),
) -> None:
    """Show the last 30 lines of a tmux session's pane output.

    Resolves the target session and captures the most recent 30 lines from its
    active pane, printing them to stdout. Useful for a quick glance at what a
    session is doing without attaching.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        name: Session name, either bare ("api") or fully qualified ("dev/api").
    """
    config: FleetConfig = ctx.obj["config"]

    try:
        node, session = asyncio.run(resolve_session(name, config))
    except SessionNotFound as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)
    except AmbiguousSession as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    cmd = build_capture_cmd(session, 30)
    result = asyncio.run(run_on_node(node, cmd))
    typer.echo(result.stdout, nl=False)


@app.command("logs")
def logs_session(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Session name (bare or node/session)."),
    lines: Optional[int] = typer.Option(
        None, "--lines", "-n", help="Number of lines (default: 100 interactive, full scrollback when piped)."
    ),
) -> None:
    """Capture pane output from a tmux session.

    Resolves the target session and captures scrollback from its active pane.
    When invoked interactively the default is 100 lines; when piped the full
    scrollback history is returned. Use --lines/-n to override explicitly.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        name: Session name, either bare ("api") or fully qualified ("dev/api").
        lines: Number of scrollback lines to capture. Defaults to 100 in a
            terminal, or the full scrollback buffer when output is piped.
    """
    config: FleetConfig = ctx.obj["config"]

    try:
        node, session = asyncio.run(resolve_session(name, config))
    except SessionNotFound as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)
    except AmbiguousSession as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    # Reason: When the user explicitly passes --lines we honour it. Otherwise
    # we pick a sensible default: 100 lines for interactive terminals, or the
    # entire scrollback buffer ("-") when output is piped so that downstream
    # tools (grep, less, etc.) receive everything.
    if lines is not None:
        lines_value: int | str = lines
    elif sys.stdout.isatty():
        lines_value = 100
    else:
        lines_value = "-"

    cmd = build_capture_cmd(session, lines_value)
    result = asyncio.run(run_on_node(node, cmd))
    typer.echo(result.stdout, nl=False)


@app.command("send")
def send_keys(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Session name (bare or node/session)."),
    keys: list[str] = typer.Argument(..., help="Keys to send to the session."),
    raw: bool = typer.Option(False, "--raw", help="Send keys verbatim without appending Enter."),
) -> None:
    """Send keystrokes to a tmux session.

    By default, appends Enter after the last key. Use --raw to pass keys
    verbatim to tmux send-keys (useful for control sequences like C-c).

    Args:
        ctx: Typer context carrying the loaded fleet config.
        name: Session name, either bare ("api") or fully qualified ("dev/api").
        keys: Keys to send to the session.
        raw: If True, send keys verbatim without appending Enter.
    """
    config: FleetConfig = ctx.obj["config"]

    try:
        node, session = asyncio.run(resolve_session(name, config))
    except SessionNotFound as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)
    except AmbiguousSession as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    cmd = build_send_keys_cmd(session, keys, raw=raw)
    result = asyncio.run(run_on_node(node, cmd))

    if result.returncode != 0:
        console.print(f"Error: {result.stderr}")
        raise typer.Exit(code=1)

    console.print(f"Sent to {node}/{session}")


@app.command("kill")
def kill_session(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Session name (bare or node/session)."),
) -> None:
    """Kill a tmux session.

    Resolves the target session and sends a kill-session command to the
    appropriate node.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        name: Session name, either bare ("api") or fully qualified ("dev/api").
    """
    config: FleetConfig = ctx.obj["config"]

    try:
        node, session = asyncio.run(resolve_session(name, config))
    except SessionNotFound as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)
    except AmbiguousSession as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    cmd = build_kill_cmd(session)
    result = asyncio.run(run_on_node(node, cmd))

    if result.returncode != 0:
        console.print(f"Error: {result.stderr}")
        raise typer.Exit(code=1)

    console.print(f"Killed session {node}/{session}")


@app.command("gc")
def gc_sessions(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, help="Session name to reap (default: all exited sessions)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List exited sessions without killing them."),
) -> None:
    """Reap exited tmux sessions fleet-wide.

    Discovers all sessions across the fleet and kills those that have exited.
    Optionally filters by session name. Prompts for confirmation in interactive
    terminals; auto-proceeds when piped.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        name: Optional session name to filter. If None, reaps all exited sessions.
        dry_run: If True, list exited sessions without killing them.
    """
    config: FleetConfig = ctx.obj["config"]

    # Fan out list command to all nodes.
    results = asyncio.run(
        fan_out(config.nodes, build_list_cmd(), max_concurrent=config.max_concurrent_ssh)
    )

    # Collect exited sessions as (node, session_name, exit_status) tuples.
    exited: list[tuple[str, str, int | None]] = []
    for node in config.nodes:
        result = results[node]
        if result.returncode != 0:
            continue
        sessions = parse_list_output(result.stdout)
        for session in sessions:
            if session.is_dead:
                if name is None or session.name == name:
                    exited.append((node, session.name, session.exit_status))

    if not exited:
        console.print("No exited sessions.")
        return

    # Dry-run: list what would be reaped and exit.
    if dry_run:
        parts = [f"{node}/{sname} [EXITED {status}]" for node, sname, status in exited]
        console.print(f"Would reap: {', '.join(parts)}")
        return

    # Interactive confirmation.
    if sys.stdin.isatty():
        typer.confirm(f"Reap {len(exited)} exited session(s)?", abort=True)

    # Kill each exited session.
    for node, sname, _ in exited:
        cmd = build_kill_cmd(sname)
        asyncio.run(run_on_node(node, cmd))
        console.print(f"Reaped {node}/{sname}")


@app.command("snapshot")
def snapshot_cmd(ctx: typer.Context) -> None:
    """Save fleet state to a JSON snapshot file.

    Captures all active sessions across the fleet and writes them to
    ~/.config/nexus/snapshot.json.
    """
    config: FleetConfig = ctx.obj["config"]
    path = asyncio.run(save_snapshot(config))
    # Reason: Count sessions from the snapshot file we just wrote to report accurately.
    import json
    data = json.loads(path.read_text())
    count = len(data.get("sessions", []))
    console.print(f"Saved {count} sessions to {path}")


@app.command("restore")
def restore_cmd(
    ctx: typer.Context,
    node: Optional[str] = typer.Option(None, "--node", help="Only restore sessions on this node."),
) -> None:
    """Restore fleet state from a JSON snapshot file.

    Reads ~/.config/nexus/snapshot.json and creates sessions for each
    entry. Use --node to filter to a specific node.
    """
    config: FleetConfig = ctx.obj["config"]
    messages = asyncio.run(restore_snapshot(config, node_filter=node))

    if not messages:
        console.print("No sessions to restore.")
        return

    for msg in messages:
        console.print(msg)

    console.print(f"Restored {len(messages)} sessions")


@app.command("dash")
def dashboard_cmd(ctx: typer.Context) -> None:
    """Open a CCTV-style read-only dashboard of all fleet sessions.

    Creates a temporary tmux session with split panes showing each active
    session in read-only mode. Press Enter to tear down the dashboard and
    attach to the selected session.
    """
    config: FleetConfig = ctx.obj["config"]
    exec_args = asyncio.run(build_dashboard(config))

    if not exec_args:
        console.print("No active sessions to display.")
        return

    os.execvp(exec_args[0], exec_args)


# ---------------------------------------------------------------------------
# nodes subcommand group
# ---------------------------------------------------------------------------

nodes_app = typer.Typer(name="nodes", help="Manage fleet nodes.", no_args_is_help=True)
app.add_typer(nodes_app)


@nodes_app.command("ls")
def nodes_list(ctx: typer.Context) -> None:
    """List fleet nodes with reachability and config status.

    Shows each node's connectivity status, tmux version, and whether
    the remote tmux.conf matches the canonical version.
    """
    config: FleetConfig = ctx.obj["config"]
    statuses = asyncio.run(nodes_ls(config))

    table = Table()
    table.add_column("Node")
    table.add_column("Status")
    table.add_column("tmux.conf")

    for status in statuses:
        if not status.reachable:
            table.add_row(status.node, "[UNREACHABLE]", "-")
        else:
            conn = "[OK]"
            drift = "[DRIFT]" if status.config_drift else "[OK]"
            table.add_row(status.node, conn, drift)

    console.print(table)


@nodes_app.command("add")
def nodes_add_cmd(
    ctx: typer.Context,
    host: Optional[str] = typer.Argument(None, help="Hostname to add to the fleet."),
) -> None:
    """Add a new node to the fleet.

    When called without arguments, parses ~/.ssh/config for Host entries,
    subtracts hosts already in the fleet, and presents an fzf picker for
    selection. When called with a hostname, adds it directly.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        host: Hostname to add. If None, discovers hosts from SSH config.
    """
    config: FleetConfig = ctx.obj["config"]

    if host is None:
        candidates = discover_hosts(config)

        if not candidates:
            console.print("No new hosts found in ~/.ssh/config")
            raise typer.Exit(code=1)

        if len(candidates) == 1:
            host = candidates[0]
            console.print(f"Auto-selected: {host}")
        elif sys.stdin.isatty():
            result = subprocess.run(
                ["fzf", "--prompt", "Select host to add: "],
                input="\n".join(candidates),
                text=True,
                capture_output=True,
            )
            if result.returncode != 0:
                console.print("Selection cancelled.")
                raise typer.Exit(code=1)
            host = result.stdout.strip()
        else:
            console.print("Multiple candidates found:")
            for c in candidates:
                console.print(f"  {c}")
            console.print("Use: nx nodes add <host>")
            raise typer.Exit(code=1)

    try:
        messages = asyncio.run(nodes_add(host, config))
    except RuntimeError as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    for msg in messages:
        console.print(msg)


@nodes_app.command("rm")
def nodes_rm_cmd(
    ctx: typer.Context,
    host: str = typer.Argument(..., help="Hostname to remove from the fleet."),
) -> None:
    """Remove a node from the fleet.

    Removes the node's SSH config block from the nexus SSH config file.

    Args:
        ctx: Typer context carrying the loaded fleet config.
        host: Hostname to remove.
    """
    config: FleetConfig = ctx.obj["config"]

    try:
        messages = nodes_rm(host, config)
    except ValueError as exc:
        console.print(f"Error: {exc}")
        raise typer.Exit(code=1)

    for msg in messages:
        console.print(msg)


# Alias: nx a → nx attach
app.command("a", hidden=True)(attach_session)

# Alias: nx l → nx list
app.command("l", hidden=True)(list_sessions)
