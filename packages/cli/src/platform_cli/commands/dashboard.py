"""Dashboard helpers."""

from __future__ import annotations

import webbrowser
from typing import Annotated

import typer

from platform_cli.output import print_json, print_success
from platform_cli.self_serve import dashboard_project_url

app = typer.Typer(help="Open General Augment dashboard views.")


@app.command("open")
def open_dashboard(
    project: Annotated[str | None, typer.Option(help="Project slug or id.")] = None,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print the URL without opening a browser."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Open the project dashboard for review."""
    url = dashboard_project_url(project)
    payload = {"url": url, "opened": not no_browser}
    if not no_browser:
        webbrowser.open(url)
    if json_output:
        print_json(payload)
        return
    print_success(f"Dashboard: {url}")
