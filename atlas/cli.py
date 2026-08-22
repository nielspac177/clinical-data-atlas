"""Command-line entry point for the ``atlas`` console script.

Only the ``schema`` subcommand is wired up so far (Task 0.2). Later tasks
add ``harvest``, ``normalize``, ``enrich``, ``graph``, ``diff``,
``validate``, ``refresh``, ``check-urls``, and ``dod`` as subparsers of the
same parser built in :func:`build_parser`.
"""

from __future__ import annotations

import argparse
import sys

from atlas import schema


def _cmd_schema(args: argparse.Namespace) -> int:
    """Print, export, or check ``docs/schema.json`` + ``docs/schema.md``."""
    json_text = schema.export_json_schema_text()
    md_text = schema.render_markdown()

    if args.export:
        schema.SCHEMA_JSON_PATH.write_text(json_text)
        schema.SCHEMA_MD_PATH.write_text(md_text)
        print(f"wrote {schema.SCHEMA_JSON_PATH}")
        print(f"wrote {schema.SCHEMA_MD_PATH}")
        return 0

    if args.check:
        drifted = []
        if (
            not schema.SCHEMA_JSON_PATH.exists()
            or schema.SCHEMA_JSON_PATH.read_text() != json_text
        ):
            drifted.append(str(schema.SCHEMA_JSON_PATH))
        if (
            not schema.SCHEMA_MD_PATH.exists()
            or schema.SCHEMA_MD_PATH.read_text() != md_text
        ):
            drifted.append(str(schema.SCHEMA_MD_PATH))
        if drifted:
            for path in drifted:
                print(f"drift: {path} does not match atlas/schema.py", file=sys.stderr)
            return 1
        print("schema docs are up to date")
        return 0

    print(json_text, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``atlas`` argparse parser."""
    parser = argparse.ArgumentParser(
        prog="atlas", description="Clinical Data Atlas pipeline CLI"
    )
    subparsers = parser.add_subparsers(dest="command")

    schema_parser = subparsers.add_parser(
        "schema", help="Print, export, or check the canonical schema docs"
    )
    mode = schema_parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--export",
        action="store_true",
        help="Write docs/schema.json and docs/schema.md",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the checked-in docs have drifted from atlas/schema.py",
    )
    schema_parser.set_defaults(func=_cmd_schema)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` and dispatch to the matching subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
