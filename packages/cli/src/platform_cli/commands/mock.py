"""Local mock server command."""

from __future__ import annotations

from typing import Annotated

import typer

from platform_cli.local_mock import DEFAULT_HOST, DEFAULT_PORT, run_server


def mock(
    host: Annotated[
        str,
        typer.Option("--host", help="Interface to bind the local mock server to."),
    ] = DEFAULT_HOST,
    port: Annotated[
        int,
        typer.Option("--port", min=1, max=65535, help="TCP port for the local mock server."),
    ] = DEFAULT_PORT,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", help="Suppress per-request HTTP access logs."),
    ] = False,
) -> None:
    """Run the local General Augment HTTP mock for app contract tests."""
    try:
        run_server(host, port, quiet=quiet)
    except KeyboardInterrupt:
        typer.echo("\nStopped General Augment local mock.", err=True)
