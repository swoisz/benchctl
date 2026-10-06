"""Command-line entry point.

Scaffold stub that only reports the version; U5 replaces this file wholesale.
"""

from __future__ import annotations

import click

from benchctl import __version__


@click.command()
@click.version_option(__version__, prog_name="benchctl")
def main() -> None:
    """Bench harness for ESP-IDF units."""
    click.echo(f"benchctl {__version__}")
