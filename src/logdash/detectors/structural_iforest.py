"""Structural anomalies, part 2: an Isolation Forest per entity on its 1-minute template counts.

The second half of the *structural* type of ``PLAN.md`` module 6 (the first is
``structural_pca.py``). A window is a row of ``features.load_scaled`` (log1p counts) of the
per-service and per-host grids. One forest is fitted per entity, on that entity's windows and
the templates it ever logs, because a pooled forest flags an entity with an unusual normal mix
on every window. Entities with fewer than ``MIN_WINDOWS`` windows, or whose windows are all
identical, are skipped (their number goes to the meta).

With ``s = -score_samples = 2 ** (-E[h] / c)``, a window is flagged when ``s > 0.5`` (it is
isolated in fewer splits than the average one) and ``s`` exceeds the entity's 99.5 %
quantile, so at most about 0.5 % of an entity's windows are flagged. Fleet-wide minutes are
dropped and neighbouring windows merged, as in the PCA detector (``_episodes``). The raw score
is ``1 / -log2(s) = c / E[h]``: 1 at ``s = 0.5``, so the threshold is 1.

The evidence names the three templates whose log1p count in the peak window exceeds the entity's
median by most.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl
from scipy import sparse
from sklearn.ensemble import IsolationForest

from logdash.analyses import features
from logdash.analyses.templates import short
from logdash.cache.store import CacheStore
from logdash.detectors import _episodes
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60
SCALING = "log1p"
THRESHOLD = 1.0  # raw = 1 / -log2(s); 1 is s = 0.5, the score of an average window
MIN_WINDOWS = 100  # windows an entity needs for its own forest
TREES = 100
SAMPLE = 256
SEED = 0
TOP = 3  # templates named in the evidence
TEXT_CHARS = 50
GRIDS = (("service", "service"), ("host", "host"))  # (features grouping, entity kind)


def isolation_scores(x: np.ndarray, seed: int = SEED) -> np.ndarray:
    """``s = 2 ** (-E[h] / c)`` of every row of the dense matrix ``x`` (larger: more isolated)."""
    forest = IsolationForest(
        n_estimators=TREES, max_samples=min(SAMPLE, len(x)), random_state=seed, n_jobs=1
    )
    return -forest.fit(x).score_samples(x)


def raw_score(s: np.ndarray) -> np.ndarray:
    """``1 / -log2(s)``: infinite at ``s = 1``, below 1 for ``s < 0.5``."""
    with np.errstate(divide="ignore"):
        return 1.0 / np.maximum(-np.log2(s), 0.0)


def entity_windows(
    scaled: sparse.csr_matrix, rows: np.ndarray
) -> tuple[np.ndarray, np.ndarray] | None:
    """Dense windows of one entity over the columns it ever logs, and those columns.

    None when the entity has too few windows or they are all the same.
    """
    if len(rows) < MIN_WINDOWS:
        return None
    part = scaled[rows]
    columns = np.unique(part.indices)
    x = part[:, columns].toarray()
    if len(columns) == 0 or np.all(x == x[0]):
        return None
    return x, columns


def score_grid(
    matrix: features.Matrix, scaled: sparse.csr_matrix
) -> tuple[pl.DataFrame, pl.DataFrame, int, int]:
    """``scores`` (``row, bin, entity, s``), ``windows`` (``bin, entity`` scored) and the
    number of entities and windows skipped."""
    order = np.argsort(matrix.codes, kind="stable")
    bounds = np.searchsorted(matrix.codes[order], np.arange(len(matrix.entities) + 1))
    rows_out, scores_out, skipped_entities, skipped_windows = [], [], 0, 0
    for code in range(len(matrix.entities)):
        rows = np.sort(order[bounds[code] : bounds[code + 1]])
        found = entity_windows(scaled, rows)
        if found is None:
            skipped_entities += 1
            skipped_windows += len(rows)
            continue
        rows_out.append(rows)
        scores_out.append(isolation_scores(found[0]))
    rows = np.concatenate(rows_out) if rows_out else np.empty(0, dtype=np.int64)
    s = np.concatenate(scores_out) if scores_out else np.empty(0)
    scores = pl.DataFrame(
        {
            "row": rows,
            "bin": matrix.bins[rows],
            "entity": np.array(matrix.entities, dtype=object)[matrix.codes[rows]],
            "s": s,
        },
        schema={
            "row": pl.Int64, "bin": pl.Datetime("us"), "entity": pl.String, "s": pl.Float64
        },
    )  # fmt: skip
    return scores, scores.select("bin", "entity"), skipped_entities, skipped_windows


def evidence(
    store: CacheStore, matrix: features.Matrix, scaled: sparse.csr_matrix, peaks: pl.DataFrame
) -> pl.DataFrame:
    """``row, evidence`` of the peak windows: the templates most above the entity's median."""
    texts = features.template_texts(store)
    codes = {name: i for i, name in enumerate(matrix.entities)}
    out = []
    for row, entity, s, minutes in peaks.select("row", "entity", "s", "minutes").iter_rows():
        members = np.flatnonzero(matrix.codes == codes[entity])
        median = np.median(scaled[members].toarray(), axis=0)
        excess = scaled[row].toarray()[0] - median
        top = [i for i in np.argsort(-excess)[:TOP] if excess[i] > 0]
        parts = [
            f"T{matrix.template_ids[i]} +{excess[i]:.1f} "
            f"{short(texts.get(int(matrix.template_ids[i]), ''), TEXT_CHARS)}"
            for i in top
        ]
        out.append(
            (
                row,
                f"isolation score {s:.2f}, {minutes} min; above the entity's median: "
                + ("; ".join(parts) if parts else "none"),
            )
        )
    return pl.DataFrame(out, schema={"row": pl.Int64, "evidence": pl.String}, orient="row")


@register
class StructuralIforest:
    key = "structural_iforest"
    type = "structural"
    title = "Isolation Forest per entity"
    version = f"1:{WINDOW_S}:{SCALING}:{THRESHOLD}:{MIN_WINDOWS}:{TREES}:{SAMPLE}:{SEED}"
    setting_names: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ("features", "templates")
    threshold = THRESHOLD

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        parts = []
        info: dict[str, Any] = {}
        for group, kind in GRIDS:
            matrix = features.load_matrix(ctx.store, group, WINDOW_S)
            scaled = features.load_scaled(ctx.store, group, WINDOW_S, SCALING)
            if scaled.shape[0] != len(matrix.bins):
                raise ValueError(f"{group}: scaled matrix and index differ in length")
            scores, windows, skipped, skipped_windows = score_grid(matrix, scaled)
            scores = scores.with_columns(
                level=pl.col("s").quantile(_episodes.LEVEL_QUANTILE, "linear").over("entity")
            )
            flagged = scores.filter((pl.col("s") > 0.5) & (pl.col("s") > pl.col("level")))
            kept, fleet = _episodes.drop_fleet_wide(flagged, windows)
            episodes = _episodes.merge_episodes(
                kept.select("bin", "entity", raw=raw_score(kept["s"].to_numpy()), row="row"),
                WINDOW_S,
            )
            peaks = episodes.join(scores.select("row", "s"), on="row")
            ctx.log(
                f"structural_iforest: {group}: {flagged.height} of {scores.height} windows "
                f"flagged, {fleet.windows} fleet-wide in {fleet.minutes} minutes, "
                f"{episodes.height} episodes, {skipped} entities skipped"
            )
            info[group] = {
                "windows": scores.height,
                "flagged": flagged.height,
                "fleet_windows": fleet.windows,
                "fleet_minutes": fleet.minutes,
                "worst_fleet_minutes": fleet.worst,
                "episodes": episodes.height,
                "skipped_entities": skipped,
                "skipped_windows": skipped_windows,
            }
            parts.append(
                peaks.join(evidence(ctx.store, matrix, scaled, peaks), on="row")
                .with_columns(
                    (pl.col("minutes") * WINDOW_S).alias("window_s"), pl.lit(kind).alias("kind")
                )
                .select("bin", "window_s", "kind", "entity", "raw", "evidence")
            )
        ctx.meta.update(info)
        return pl.concat(parts)
