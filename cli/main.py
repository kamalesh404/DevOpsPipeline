"""Command line entry point for DevOpsPipeline."""

from __future__ import annotations

import logging

import click

from cli.pipeline import pipeline_group
from cli.plugin import plugin_group


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="devopspipeline", prog_name="devops")
@click.option("-v", "--verbose", count=True, help="Increase verbosity (-v INFO, -vv DEBUG).")
def cli(verbose: int) -> None:
    """DevOpsPipeline — build, test and deploy pipelines from the terminal."""
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("devopspipeline").setLevel(level)


cli.add_command(pipeline_group)
cli.add_command(plugin_group)


def main() -> None:
    """Console-script entry point."""
    cli(prog_name="devops")


if __name__ == "__main__":  # pragma: no cover
    main()
