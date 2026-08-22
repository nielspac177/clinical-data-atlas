"""Command-line entry point for the ``atlas`` console script.

Subcommands (schema, harvest, normalize, enrich, graph, diff, validate,
refresh, check-urls, dod) land in later tasks. For now this only prints
usage so the packaged entry point (and ``uv run atlas``) resolve.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Print usage and return 0. Real subcommand dispatch lands in later tasks."""
    print(
        "atlas: Clinical Data Atlas pipeline CLI\n"
        "Subcommands (coming soon): schema, harvest, normalize, enrich, "
        "graph, diff, validate, refresh, check-urls, dod"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
