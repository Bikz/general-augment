"""Rich output helpers for the standalone CLI."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def print_json(payload: Any) -> None:
    """Print JSON using Rich syntax highlighting."""
    console.print_json(json.dumps(payload, indent=2, sort_keys=True))


def print_success(message: str) -> None:
    """Print a success message."""
    console.print(f"[green]{message}[/green]")


def print_warning(message: str) -> None:
    """Print a warning message."""
    console.print(f"[yellow]{message}[/yellow]")


def print_error(message: str) -> None:
    """Print an error message."""
    console.print(f"[red]{message}[/red]")


def panel(title: str, body: str) -> None:
    """Print a titled panel."""
    console.print(Panel(body, title=title, border_style="cyan"))


def table(title: str, columns: list[str], rows: list[list[object]]) -> None:
    """Print a simple table."""
    rich_table = Table(title=title)
    for column in columns:
        rich_table.add_column(column)
    for row in rows:
        rich_table.add_row(*(str(cell) for cell in row))
    console.print(rich_table)
