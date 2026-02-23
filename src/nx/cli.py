"""Nexus CLI entry point."""

import typer

from nx import __version__
from nx.config import FleetConfig, load_config

app = typer.Typer(
    name="nx",
    help="nx — Distributed terminal orchestration.",
    no_args_is_help=True,
)


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
