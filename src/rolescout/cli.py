"""The ``rolescout`` console command."""

import argparse
import sys

from rolescout import __version__
from rolescout.adapters import ADAPTERS, SourceError, get_adapter

DESCRIPTION_PREVIEW_CHARS = 400


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        roles = get_adapter(args.source).fetch(args.slug)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{args.source}/{args.slug}: {len(roles)} open roles")
    if not roles:
        return 0
    if not 1 <= args.sample <= len(roles):
        print(f"error: --sample must be between 1 and {len(roles)}", file=sys.stderr)
        return 1

    print(f"\nSample role ({args.sample} of {len(roles)}):")
    for field, value in roles[args.sample - 1].to_dict().items():
        if field == "description" and not args.full and len(value) > DESCRIPTION_PREVIEW_CHARS:
            value = f"{value[:DESCRIPTION_PREVIEW_CHARS]}… [{len(value)} chars; --full to show all]"
        print(f"  {field + ':':<19}{value}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rolescout", description=__doc__)
    parser.add_argument("--version", action="version", version=f"rolescout {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser(
        "fetch", help="fetch one board and print the role count and a sample role")
    fetch.add_argument("source", choices=sorted(ADAPTERS))
    fetch.add_argument("slug", help="board slug, e.g. the last part of jobs.lever.co/<slug>")
    fetch.add_argument("--sample", type=int, default=1, metavar="N",
                       help="which role to print, 1-based (default: 1)")
    fetch.add_argument("--full", action="store_true", help="print the whole description")
    fetch.set_defaults(func=_cmd_fetch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
