"""Precompute pipeline: build stale stages and record them in the manifest."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from logdash.cache.manifest import Entry, data_fingerprint, now_iso, stage_fingerprint
from logdash.cache.store import EVENTS_KEY, CacheStore
from logdash.config import Settings
from logdash.ingest.reader import ingest

EVENTS_VERSION = "1"

Log = Callable[[str], None]


@dataclass(frozen=True)
class StageResult:
    key: str
    status: str  # "built" or "fresh"
    duration_s: float = 0.0


def events_fingerprint(settings: Settings, *, strict: bool = False) -> str:
    return stage_fingerprint(
        version=EVENTS_VERSION,
        data_fp=data_fingerprint(settings.data_files(), strict=strict),
        params=settings.fingerprint_params(),
        upstream={},
    )


def run_build(
    settings: Settings,
    *,
    force: bool = False,
    strict: bool = False,
    log: Log = print,
) -> list[StageResult]:
    """Build the ``events`` stage if it is stale."""
    store = CacheStore(settings.cache_dir, settings)
    manifest = store.manifest()
    fingerprint = events_fingerprint(settings, strict=strict)
    if not force and not manifest.is_stale(EVENTS_KEY, fingerprint):
        log(f"{EVENTS_KEY}: up to date")
        return [StageResult(EVENTS_KEY, "fresh")]
    log(f"{EVENTS_KEY}: ingesting {len(settings.data_files())} file(s)")
    started = time.perf_counter()
    result = ingest(settings, store.events_dir)
    duration = time.perf_counter() - started
    manifest.set(
        EVENTS_KEY,
        Entry(
            fingerprint=fingerprint,
            version=EVENTS_VERSION,
            created_at=now_iso(),
            duration_s=round(duration, 3),
            rows={"rows": result.rows, "malformed": result.malformed, "hours": result.hours},
        ),
    )
    manifest.save()
    log(f"{EVENTS_KEY}: {result.rows:,} rows in {duration:.1f}s")
    return [StageResult(EVENTS_KEY, "built", duration)]
