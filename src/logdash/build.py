"""Precompute pipeline: build stale stages in dependency order and record them."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from logdash.analyses.base import REGISTRY, Analysis, ComputeContext, Registry
from logdash.cache.manifest import Entry, Manifest, data_fingerprint, now_iso, stage_fingerprint
from logdash.cache.store import EVENTS_KEY, CacheStore
from logdash.config import Settings
from logdash.ingest.reader import ingest

EVENTS_VERSION = "2"

Log = Callable[[str], None]


@dataclass(frozen=True)
class StageResult:
    key: str
    status: str  # "built" or "fresh"
    duration_s: float = 0.0


@dataclass(frozen=True)
class StageStatus:
    key: str
    state: str  # "fresh", "stale", "missing" or "unverified" (no data files to compare)
    entry: Entry | None


def compute_fingerprints(
    settings: Settings, registry: Registry, *, strict: bool = False
) -> dict[str, str]:
    """Fingerprint of ``events`` and of every registered analysis."""
    data_fp = data_fingerprint(settings.data_files(), strict=strict)
    params = settings.fingerprint_params()
    prints = {
        EVENTS_KEY: stage_fingerprint(
            version=EVENTS_VERSION, data_fp=data_fp, params=params, upstream={}
        )
    }
    for analysis in registry.ordered():
        upstream = {EVENTS_KEY: prints[EVENTS_KEY]}
        upstream.update({dep: prints[dep] for dep in analysis.depends_on})
        prints[analysis.key] = stage_fingerprint(
            version=analysis.version, data_fp=data_fp, params=params, upstream=upstream
        )
    return prints


def stage_statuses(
    settings: Settings, registry: Registry, *, strict: bool = False
) -> list[StageStatus]:
    """State of each stage in build order."""
    manifest = CacheStore(settings.cache_dir, settings).manifest()
    prints = compute_fingerprints(settings, registry, strict=strict)
    verifiable = bool(settings.data_files())
    keys = [EVENTS_KEY, *(a.key for a in registry.ordered())]
    out = []
    for key in keys:
        entry = manifest.get(key)
        if entry is None:
            state = "missing"
        elif not verifiable:
            state = "unverified"
        else:
            state = "stale" if manifest.is_stale(key, prints[key]) else "fresh"
        out.append(StageStatus(key, state, entry))
    return out


def run_build(
    settings: Settings,
    *,
    only: str | None = None,
    force: bool = False,
    strict: bool = False,
    registry: Registry | None = None,
    log: Log = print,
) -> list[StageResult]:
    """Build stale stages (``events`` first, then analyses in topological order).

    ``only`` restricts the run to one stage plus its dependencies; ``force`` then
    applies to that stage alone. A stage is rebuilt if forced, missing, fingerprint
    changed, or any upstream stage was rebuilt in this run.
    """
    registry = REGISTRY if registry is None else registry
    if only is None or only == EVENTS_KEY:
        analyses = registry.ordered() if only is None else []
    else:
        analyses = registry.ordered([only])
    store = CacheStore(settings.cache_dir, settings)
    manifest = store.manifest()
    prints = compute_fingerprints(settings, registry, strict=strict)
    rebuilt: set[str] = set()
    results: list[StageResult] = []

    def forced(key: str) -> bool:
        return force and only in (None, key)

    def needs_build(key: str, upstream: set[str]) -> bool:
        return forced(key) or manifest.is_stale(key, prints[key]) or bool(upstream & rebuilt)

    def record(key: str, version: str, started: float, rows: dict[str, int]) -> StageResult:
        duration = time.perf_counter() - started
        entry = Entry(prints[key], version, now_iso(), round(duration, 3), rows)
        manifest.set(key, entry)
        manifest.save()
        rebuilt.add(key)
        log(f"{key}: built in {duration:.1f}s")
        return StageResult(key, "built", duration)

    if needs_build(EVENTS_KEY, set()):
        log(f"{EVENTS_KEY}: ingesting {len(settings.data_files())} file(s)")
        started = time.perf_counter()
        ingested = ingest(settings, store.events_dir)
        rows = {"rows": ingested.rows, "malformed": ingested.malformed, "hours": ingested.hours}
        results.append(record(EVENTS_KEY, EVENTS_VERSION, started, rows))
    else:
        log(f"{EVENTS_KEY}: up to date")
        results.append(StageResult(EVENTS_KEY, "fresh"))

    for analysis in analyses:
        upstream = {EVENTS_KEY, *analysis.depends_on}
        if needs_build(analysis.key, upstream):
            results.append(_build_analysis(analysis, store, manifest, settings, log, record))
        else:
            log(f"{analysis.key}: up to date")
            results.append(StageResult(analysis.key, "fresh"))
    return results


def _build_analysis(
    analysis: Analysis,
    store: CacheStore,
    manifest: Manifest,
    settings: Settings,
    log: Log,
    record: Callable[[str, str, float, dict[str, int]], StageResult],
) -> StageResult:
    log(f"{analysis.key}: computing")
    started = time.perf_counter()
    out_dir = store.reset_module(analysis.key)
    manifest.remove(analysis.key)
    con = store.connect()
    ctx = ComputeContext(analysis.key, con, settings, out_dir, log)
    try:
        analysis.compute(ctx)
    except BaseException:
        manifest.save()
        raise
    finally:
        con.close()
    rows = {**store.count_parquet_rows(analysis.key), **ctx.rows}
    return record(analysis.key, analysis.version, started, rows)
