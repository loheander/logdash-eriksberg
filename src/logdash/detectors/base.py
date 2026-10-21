"""Detector contract, registry and score normalisation for the anomaly module.

A detector is one class in one module under ``logdash/detectors/``: it reads the cache of the
stages it depends on and returns the candidate anomalies as rows. The anomaly analysis
(``analyses/anomalies.py``) runs every registered detector, normalises the scores and writes the
combined table; the page reads that table only. Adding a detector therefore touches neither the
page nor the framework: create the module, decorate the class with ``@register``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import Any, NamedTuple, Protocol, runtime_checkable

import polars as pl

from logdash.analyses.explorer import LINK_KEYS
from logdash.app.filters import Filters
from logdash.cache.store import CacheStore
from logdash.config import Settings

# Bump when the score formula, the columns or the caps' meaning change; part of the stage version.
FRAMEWORK_VERSION = "3"

Schema = dict[str, pl.DataType | type[pl.DataType]]

# Columns a detector returns. ``raw`` is the detector's own score (larger is more anomalous),
# ``kind`` says what ``entity`` is (one of ``KINDS``) and ``template_id``
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
class EntityKind:
    """What a page needs to know about one kind of entity; declared once with ``declare_kind``.

    ``link_param`` is the explorer query key the entity goes into (None: the explorer cannot
    filter on it). ``filter_field`` is the ``Filters`` attribute whose values restrict rows of
    this kind (None: the global filters do not apply). ``link_window`` says whether the explorer
    link is limited to the anomaly's window; it is not for a kind whose lines span other times
    (a request).
    """

    name: str
    link_param: str | None = None
    filter_field: str | None = None
    link_window: bool = True


KINDS: dict[str, EntityKind] = {}


def declare_kind(
    name: str,
    link_param: str | None = None,
    filter_field: str | None = None,
    link_window: bool = True,
) -> EntityKind:
    """Declare an entity kind (the only place a new kind needs to be added); validated."""
    if name in KINDS:
        raise DetectorError(f"entity kind {name!r} already declared")
    if link_param is not None and link_param not in LINK_KEYS:
        raise DetectorError(f"kind {name!r}: the explorer has no {link_param!r} parameter")
    if filter_field is not None and filter_field not in {f.name for f in fields(Filters)}:
        raise DetectorError(f"kind {name!r}: Filters has no {filter_field!r} field")
    KINDS[name] = EntityKind(name, link_param, filter_field, link_window)
    return KINDS[name]


@dataclass(frozen=True)
class DetectContext:
    """What ``Detector.detect`` gets: the cache (read the upstream stages through it).

    ``meta`` is the detector's to fill (JSON values): the stage stores it as ``info`` in the
    detector's ``meta.json`` entry, and the page shows ``info["note"]`` (text) if there is one.
    Use it for what the rows alone do not tell, such as how many entities could not be scored.
    """

    store: CacheStore
    settings: Settings
    log: Callable[[str], None] = print
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Detector(Protocol):
    """One anomaly detector; ``detect`` returns rows with the columns of ``DETECTED_SCHEMA``.

    ``detect`` must return every row it considers anomalous: do not sort and cut before returning
    (the framework caps per entity and keeps the counts; a NaN sorts as the largest value in
    polars and would take a slot). Rows with a null or NaN ``raw``, or ``raw`` below the
    threshold, are dropped; ``+inf`` counts as far above the threshold.

    ``setting_names`` lists the settings that enter the cache fingerprint (``()`` if none). A
    detector whose threshold depends on settings may also define ``threshold_for(settings)``.
    """

    key: str  # value of the ``detector`` column
    type: str  # anomaly type (several detectors may share one)
    title: str
    version: str  # bump to invalidate the anomaly cache
    depends_on: tuple[str, ...]  # analyses whose cache ``detect`` reads
    threshold: float  # raw score at which a row counts as anomalous (> 0)
    setting_names: tuple[str, ...]

    def detect(self, ctx: DetectContext) -> pl.DataFrame: ...


class DetectorError(Exception):
    """A detector is registered twice, is malformed or returned a malformed frame."""


class DetectorRegistry:
    """Ordered collection of detectors keyed by ``Detector.key``."""

    def __init__(self) -> None:
        self._items: dict[str, Detector] = {}

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def add(self, detector: Detector) -> Detector:
        if not isinstance(detector, Detector):
            raise DetectorError(f"{detector!r} does not implement the Detector protocol")
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
    """Class decorator: instantiate the detector (no arguments) and add it to the registry.

    The registry checks the instance against the ``Detector`` protocol. Modules in ``detectors/``
    are imported by ``load_builtin`` when ``analyses.anomalies`` is imported.
    """
    DETECTORS.add(cls())  # type: ignore[arg-type]
    return cls


def threshold_of(detector: Detector, settings: Settings) -> float:
    """The detector's threshold for a run: ``threshold_for(settings)`` if it has one."""
    method = getattr(detector, "threshold_for", None)
    return float(method(settings)) if method is not None else detector.threshold


# A row's score blends where it ranks among the detector's rows of its kind with how far above
# the threshold it is, relative to that kind's strong rows; see ``score_columns``.
LOG_CAP = 10.0  # octaves above the threshold (a factor of 1024) beyond which raw is clipped
SCALE_QUANTILE = 0.99  # the kind's rows at this quantile of the excess get full magnitude


def score_columns(threshold: float) -> list[pl.Expr]:
    """Expressions for ``score`` in ``[0, 1]``: ``0.5 * percentile + 0.5 * magnitude``.

    ``percentile`` is the row's rank among the rows of the same detector and kind (ties share
    the average rank), so a detector's units and tail behaviour do not matter and no kind
    crowds out another. ``magnitude`` is the log-scale excess ``log2(raw / threshold)``, with
    ``raw`` clipped to ``2 ** LOG_CAP`` times the threshold (so ``+inf`` is finite), divided by
    the excess at the ``SCALE_QUANTILE`` of the same kind (at least one octave) and clipped to
    ``[0, 1]``. Scaling per kind keeps a grouping with a heavier tail (host SPE ratios reach
    thousands, service ratios tens) from filling the top of the ranking. The score keeps its
    spread, orders rows within a detector and kind, and across them compares positions in each
    one's ranking and excess; it is not a probability.
    """
    percentile = pl.col("raw").rank("average").over("kind") / pl.len().over("kind")
    octaves = (pl.col("raw") / threshold).clip(1.0, 2.0**LOG_CAP).log(2)
    scale = octaves.quantile(SCALE_QUANTILE).over("kind").clip(lower_bound=1.0)
    magnitude = (octaves / scale).clip(0.0, 1.0)
    return [(0.5 * percentile + 0.5 * magnitude).alias("score")]


class Finished(NamedTuple):
    """Result of ``finish``: the kept rows and how many rows reached the threshold."""

    rows: pl.DataFrame
    above_threshold: int


def finish(
    detector: Detector,
    found: pl.DataFrame,
    max_rows: int,
    max_per_entity: int | None = None,
    threshold: float | None = None,
) -> Finished:
    """Validate a detector's rows, drop those below its threshold and add the common columns.

    The score is computed over every row at or above the threshold. Then at most
    ``max_per_entity`` highest-scoring rows are kept per (kind, entity), so that a sustained
    incident on a few entities does not hide an isolated anomaly on a quiet one, and finally the
    ``max_rows`` highest overall; rows are sorted by score (descending), then time.
    """
    missing = set(REQUIRED) - set(found.columns)
    if missing:
        raise DetectorError(f"detector {detector.key!r} did not return {sorted(missing)}")
    limit = detector.threshold if threshold is None else threshold
    if "template_id" not in found.columns:
        found = found.with_columns(pl.lit(None, dtype=pl.Int64).alias("template_id"))
    typed = found.select(*DETECTED_SCHEMA).cast(pl.Schema(DETECTED_SCHEMA))
    unknown = sorted(set(typed["kind"].drop_nulls()) - set(KINDS))
    if unknown or typed["kind"].null_count():
        raise DetectorError(
            f"detector {detector.key!r} returned entity kind {unknown or 'null'}; declared "
            f"kinds are {sorted(KINDS)} (add one with declare_kind in detectors/base.py)"
        )
    rows = (
        typed.filter(pl.col("raw").is_not_null() & ~pl.col("raw").is_nan())
        .filter(pl.col("raw") >= limit)
        .with_columns(
            pl.lit(detector.key).alias("detector"),
            pl.lit(detector.type).alias("type"),
            *score_columns(limit),
        )
        .sort(["score", "bin"], descending=[True, False])
    )
    above = rows.height
    if max_per_entity is not None:
        rows = rows.filter(pl.int_range(pl.len()).over("kind", "entity") < max_per_entity)
    return Finished(rows.head(max_rows).select(*ANOMALY_SCHEMA), above)


def empty() -> pl.DataFrame:
    """A frame with the columns of ``anomalies.parquet`` and no rows."""
    return pl.DataFrame(schema=ANOMALY_SCHEMA)


declare_kind("service", link_param="service", filter_field="services")
declare_kind("host", link_param="host", filter_field="hosts")
declare_kind("request", link_param="request_id", link_window=False)
