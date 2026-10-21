"""Command line interface: ``logdash build | status | serve``."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from logdash import __version__
from logdash.build import events_fingerprint, run_build
from logdash.cache.store import EVENTS_KEY, CacheStore
from logdash.config import Settings


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", dest="data_dir", help="directory with *.csv.zst files")
    parser.add_argument("--cache", dest="cache_dir", help="cache directory")
    parser.add_argument("--memory-limit", help="DuckDB memory limit, e.g. 8GB")
    parser.add_argument("--threads", type=int, help="DuckDB threads")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="logdash", description="OpenStack log dashboard")
    parser.add_argument("--version", action="version", version=f"logdash {__version__}")
    sub = parser.add_subparsers(dest="command")

    build = sub.add_parser("build", help="precompute stale cache stages")
    _common(build)
    build.add_argument("--only", metavar="KEY", help="build only this stage (and its stale deps)")
    build.add_argument("--force", action="store_true", help="rebuild even if up to date")
    build.add_argument("--strict", action="store_true", help="fingerprint file content, not mtime")

    status = sub.add_parser("status", help="show cache state per stage")
    _common(status)
    status.add_argument("--strict", action="store_true", help="fingerprint file content")

    serve = sub.add_parser("serve", help="start the dashboard")
    _common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8050)
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_env().with_overrides(
        data_dir=args.data_dir,
        cache_dir=args.cache_dir,
        memory_limit=args.memory_limit,
        threads=args.threads,
    )


def cmd_build(args: argparse.Namespace) -> int:
    run_build(_settings(args), force=args.force, strict=args.strict)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    manifest = CacheStore(settings.cache_dir, settings).manifest()
    entry = manifest.get(EVENTS_KEY)
    if entry is None:
        state = "missing"
    elif settings.data_files() and manifest.is_stale(
        EVENTS_KEY, events_fingerprint(settings, strict=args.strict)
    ):
        state = "stale"
    else:
        state = "fresh"
    print(f"{'stage':<16}{'state':<9}{'duration':>10}  created")
    duration = f"{entry.duration_s:.1f}s" if entry else "-"
    created = entry.created_at if entry else "-"
    print(f"{EVENTS_KEY:<16}{state:<9}{duration:>10}  {created}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    print("serve: not implemented yet")
    return 1


COMMANDS = {"build": cmd_build, "status": cmd_status, "serve": cmd_serve}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return COMMANDS[args.command](args)
