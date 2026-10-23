"""Command line interface: ``logdash build | status | serve | spot-check``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from logdash import __version__
from logdash.analyses import load_builtin
from logdash.analyses.base import REGISTRY, RegistryError
from logdash.build import run_build, stage_statuses
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.tagging import spotcheck


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
    serve.add_argument("--debug", action="store_true", help="Dash debug mode with reloader")
    serve.add_argument("--no-build", action="store_true", help="do not build stale stages first")

    spot = sub.add_parser("spot-check", help="sample tagged templates for review, score the marks")
    actions = spot.add_subparsers(dest="action", required=True)
    sample = actions.add_parser("sample", help="write a review sheet from the cached tags")
    _common(sample)
    sample.add_argument(
        "--out", type=Path, default=spotcheck.DEFAULT_SHEET, help="sheet (CSV) to write"
    )
    sample.add_argument(
        "--size", type=int, default=spotcheck.SAMPLE_SIZE, help="templates to sample"
    )
    sample.add_argument("--seed", type=int, default=spotcheck.SEED, help="random seed")
    sample.add_argument("--force", action="store_true", help="overwrite an existing sheet")
    score = actions.add_parser("score", help="agreement from a marked sheet")
    score.add_argument(
        "--sheet", type=Path, default=spotcheck.DEFAULT_SHEET, help="marked sheet (CSV)"
    )
    score.add_argument(
        "--out", type=Path, default=spotcheck.DEFAULT_REPORT, help="report (Markdown) to write"
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_env().with_overrides(
        data_dir=args.data_dir,
        cache_dir=args.cache_dir,
        memory_limit=args.memory_limit,
        threads=args.threads,
    )


def cmd_build(args: argparse.Namespace) -> int:
    load_builtin()
    try:
        run_build(_settings(args), only=args.only, force=args.force, strict=args.strict)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    load_builtin()
    print(f"{'stage':<16}{'state':<11}{'duration':>10}  created")
    for st in stage_statuses(_settings(args), REGISTRY, strict=args.strict):
        duration = f"{st.entry.duration_s:.1f}s" if st.entry else "-"
        created = st.entry.created_at if st.entry else "-"
        print(f"{st.key:<16}{st.state:<11}{duration:>10}  {created}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from logdash.app.main import create_app  # Dash is only needed to serve

    load_builtin()
    settings = _settings(args)
    if not args.no_build:
        try:
            run_build(settings, allow_model=False)  # serve never calls a language model (ADR001)
        except RegistryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    store = CacheStore(settings.cache_dir, settings)
    try:
        app = create_app(store)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def _spot_sample(args: argparse.Namespace) -> int:
    from logdash.analyses.tags import read_meta, read_tags
    from logdash.analyses.templates import KEY as TEMPLATES_KEY

    load_builtin()
    settings = _settings(args)
    store = CacheStore(settings.cache_dir, settings)
    if args.out.exists() and not args.force:
        print(
            f"error: {args.out} exists (it may hold marks); pass --force to overwrite",
            file=sys.stderr,
        )
        return 2
    tags = read_tags(store)
    if tags is None:
        print(
            "error: no semantic tags in the cache; "
            "run `logdash build --only tags` with OPENROUTER_API_KEY set",
            file=sys.stderr,
        )
        return 2
    state = next((s.state for s in stage_statuses(settings, REGISTRY) if s.key == "tags"), None)
    if state != "fresh":
        print(
            f"warning: the tags stage is {state}; the sheet may not match the current templates",
            file=sys.stderr,
        )
    try:
        templates = store.read_parquet(TEMPLATES_KEY, "templates.parquet")
        counts = store.read_parquet(TEMPLATES_KEY, "template_counts.parquet")
        pop = spotcheck.population(tags, templates, counts)
        picked = spotcheck.sample(pop, args.size, args.seed)
        meta = spotcheck.sheet_meta(
            tags, pop, picked, read_meta(store), seed=args.seed, n=args.size
        )
        sidecar = spotcheck.write_sheet(args.out, picked, meta)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if meta["untagged"]:
        print(
            f"warning: {meta['untagged']} untagged template(s) left out; "
            "`logdash build --only tags --force` retries them",
            file=sys.stderr,
        )
    print(
        f"wrote {args.out} and {sidecar}: {picked.height} templates ({spotcheck.strata_line(meta)})"
    )
    if picked.height < args.size:
        print(f"only {picked.height} tagged templates could be sampled (asked for {args.size})")
    return 0


def _spot_score(args: argparse.Namespace) -> int:
    try:
        rows = spotcheck.read_sheet(args.sheet)
        meta = spotcheck.read_sidecar(spotcheck.sidecar_path(args.sheet))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = spotcheck.score(rows)
    text = spotcheck.render_report(result, meta)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {args.out}")
    if result.overall.unmarked:
        print(
            f"warning: {result.overall.unmarked} of {result.rows * len(spotcheck.TAGS)} "
            "marks are empty and not counted",
            file=sys.stderr,
        )
    return 0


def cmd_spot_check(args: argparse.Namespace) -> int:
    return _spot_sample(args) if args.action == "sample" else _spot_score(args)


COMMANDS = {
    "build": cmd_build,
    "status": cmd_status,
    "serve": cmd_serve,
    "spot-check": cmd_spot_check,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return COMMANDS[args.command](args)
