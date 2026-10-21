"""Detector contract, registry and score normalisation for the anomaly module.

A detector is one class in one module under ``logdash/detectors/``: it reads the cache of the
stages it depends on and returns the candidate anomalies as rows. The anomaly analysis
(``analyses/anomalies.py``) runs every registered detector, normalises the scores and writes the
combined table; the page reads that table only. Adding a detector therefore touches neither the
page nor the framework: create the module, decorate the class with ``@register``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import polars as pl

from logdash.cache.store import CacheStore
from logdash.config import Settings

Schema = dict[str, pl.DataType | type[pl.DataType]]

# Columns a detector returns. ``raw`` is the detector's own score (larger is more anomalous),
# ``kind`` says what ``entity`` is (``service``, ``host`` or ``request``) and ``template_id``
# is optional (null when the row is not about one template). The framework adds ``detector``,
# ``type`` and the normalised ``score``.
REQUIRED: Schema = {
    "bin": pl.Datetime("us"),  # window start, UTC, naive
    "window_s": pl.Int64,
    "kind": pl.String,
    "entity": pl.String,
    "raw": pl.Float64,
    "evidence": pl.String,
}
OPTIONAL: Schema = {"template_id": pl.Int64}
DETECTED_SCHEMA: Schema = {**REQUIRED, **OPTIONAL}

# Columns of ``anomalies.parquet``.
ANOMALY_SCHEMA: Schema = {
    "detector": pl.String,
    "type": pl.String,
    "bin": pl.Datetime("us"),
    "window_s": pl.Int64,
    "kind": pl.String,
    "entity": pl.String,
    "template_id": pl.Int64,
    "raw": pl.Float64,
    "score": pl.Float64,
    "evidence": pl.String,
}


@dataclass(frozen=True)
class DetectContext:
    """What ``Detector.detect`` gets: the cache (read the upstream stages through it)."""

    store: CacheStore
    settings: Settings
    log: Callable[[str], None] = print


@runtime_checkable
class Detector(Protocol):
    """One anomaly detector; ``detect`` returns rows with the columns of ``DETECTED_SCHEMA``.

    A detector may also have ``setting_names``, the settings that enter the cache fingerprint.
    """

    key: str  # value of the ``detector`` column
    type: str  # anomaly type (several detectors may share one)
    title: str
    version: str  # bump to invalidate the anomaly cache
    depends_on: tuple[str, ...]  # analyses whose cache ``detect`` reads
    threshold: float  # raw score at which a row counts as anomalous (> 0)

    def detect(self, ctx: DetectContext) -> pl.DataFrame: ...


class DetectorError(Exception):
    """A detector is registered twice or returned a malformed frame."""


class DetectorRegistry:
    """Ordered collection of detectors keyed by ``Detector.key``."""

    def __init__(self) -> None:
        self._items: dict[str, Detector] = {}

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def add(self, detector: Detector) -> Detector:
        if detector.key in self._items:
            raise DetectorError(f"detector {detector.key!r} already registered")
        if not detector.threshold > 0:
            raise DetectorError(f"detector {detector.key!r} needs a positive threshold")
        self._items[detector.key] = detector
        return detector

    def get(self, key: str) -> Detector:
        try:
            return self._items[key]
        except KeyError:
            raise DetectorError(f"unknown detector {key!r}") from None

    def all(self) -> list[Detector]:
        return list(self._items.values())

    def types(self) -> list[str]:
        """Anomaly types of the registered detectors, in registration order, without repeats."""
        return list(dict.fromkeys(d.type for d in self._items.values()))

    def clear(self) -> None:
        self._items.clear()


DETECTORS = DetectorRegistry()


def register[T](cls: type[T]) -> type[T]:
    """Class decorator: instantiate the detector and add it to the global registry."""
    DETECTORS.add(cls())  # type: ignore[arg-type]
    return cls


def normalise(raw: pl.Expr | pl.Series, threshold: float) -> pl.Expr | pl.Series:
    """Map raw scores to ``[0.5, 1]``: ``1 - threshold / (2 raw)``.

    A row exactly at its detector's threshold scores 0.5; twice the threshold 0.75; the score
    approaches 1 as the raw score grows, so the scale reads as "multiples of the detector's own
    threshold" and stays comparable across detectors whose raw units differ. Callers pass raw
    scores at or above the threshold.
    """
    return 1 - threshold / (2 * raw)


def finish(detector: Detector, found: pl.DataFrame, max_rows: int) -> pl.DataFrame:
    """Validate a detector's rows, drop those below its threshold and add the common columns.

    Keeps the ``max_rows`` highest-scoring rows, sorted by score (descending), then time.
    """
    missing = set(REQUIRED) - set(found.columns)
    if missing:
        raise DetectorError(f"detector {detector.key!r} did not return {sorted(missing)}")
    if "template_id" not in found.columns:
        found = found.with_columns(pl.lit(None, dtype=pl.Int64).alias("template_id"))
    rows = (
        found.select(*DETECTED_SCHEMA)
        .cast(pl.Schema(DETECTED_SCHEMA))
        .filter(pl.col("raw").is_not_null() & ~pl.col("raw").is_nan())
        .filter(pl.col("raw") >= detector.threshold)
        .with_columns(
            pl.lit(detector.key).alias("detector"),
            pl.lit(detector.type).alias("type"),
            normalise(pl.col("raw"), detector.threshold).alias("score"),
        )
        .sort(["score", "bin"], descending=[True, False])
        .head(max_rows)
    )
    return rows.select(*ANOMALY_SCHEMA)


def empty() -> pl.DataFrame:
    """A frame with the columns of ``anomalies.parquet`` and no rows."""
    return pl.DataFrame(schema=ANOMALY_SCHEMA)
