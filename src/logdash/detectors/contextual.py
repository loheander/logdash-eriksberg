"""Contextual anomalies: a host's 1-minute template mix against the mix of its peers.

This is the *contextual* type of ``PLAN.md`` module 6: a window that is normal for the fleet but
not for the host's own kind of host (or the reverse). It builds on module 7 (``analyses/hosts``).
A window's counts become ``h = sqrt(c / sum(c))`` and its distance to the host's peer centroid is
``centroid_distance``; the host's own baseline (``load_baseline``: median and scaled MAD of that
distance over its windows) turns the distance into a robust z-score, which is the raw score::

    z = (distance - median) / max(mad * noise, MAD_FLOOR * median)

Peers are the host's nearest hosts within a fixed radius, not its k-means cluster, so a lopsided
cluster does not make its members look alike. A host with no valid peer group has no peer centroid
and is **not scored**; their number goes to the detector's meta (``unscored_hosts``) and the page.

The distance of a window of ``n`` lines is noisy: it shrinks and grows roughly with ``1 / sqrt(n)``,
and a window of a few lines is far from any centroid whatever it says. Two rules keep that out:
windows of fewer than ``max(MIN_LINES, MIN_SHARE * median lines of the host)`` lines are not
scored (hosts that never reach that are counted as ``quiet_hosts``), and the scale of a window
below the host's median line count grows by ``sqrt(median / n)``.

The evidence names the templates that contribute most to the squared distance of the window:
their share of the window against the peers' share (the peer centroid squared, an approximation
of their mean share).
"""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy import sparse

from logdash.analyses import features, hosts, templates
from logdash.analyses.templates import short
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60
THRESHOLD = 8.0  # robust z-score of the window distance against the host's own baseline
MIN_LINES = 20  # windows with fewer lines are not scored
MIN_SHARE = 0.5  # ... nor those below this share of the host's median lines per window
MAD_FLOOR = hosts.MAD_FLOOR  # the scale is at least this share of the median distance
TOP = 3  # templates named in the evidence
TEXT_CHARS = 50
CHUNK = 2_000  # windows densified at once for the evidence


def scored_windows(
    windows: pl.DataFrame, matrix: sparse.csr_matrix, centroids: hosts.Centroids,
    baseline: pl.DataFrame,
) -> pl.DataFrame:  # fmt: skip
    """``row, host, bin, lines, distance, median, z`` of every window that is scored.

    ``row`` is the window's row in ``matrix``. Windows too small for their host (see the module
    docstring) are left out.
    """
    lines = np.asarray(matrix.sum(axis=1)).ravel()
    distance = hosts.centroid_distance(matrix, windows["host"].to_list(), centroids)
    frame = (
        windows.with_columns(
            row=pl.int_range(pl.len(), dtype=pl.Int64),
            lines=pl.Series(lines),
            distance=pl.Series(distance),
        )
        .join(baseline.select("host", "median_lines", "median", "mad"), on="host")
        .filter(pl.col("lines") >= pl.max_horizontal(MIN_LINES, MIN_SHARE * pl.col("median_lines")))
    )
    noise = (pl.col("median_lines") / pl.col("lines")).clip(lower_bound=1.0).sqrt()
    scale = pl.max_horizontal(pl.col("mad") * noise, MAD_FLOOR * pl.col("median"), 1e-9)
    return frame.with_columns(z=(pl.col("distance") - pl.col("median")) / scale).select(
        "row", "host", "bin", "lines", "distance", "median", "z"
    )


def evidence(
    flagged: pl.DataFrame, matrix: sparse.csr_matrix, centroids: hosts.Centroids,
    template_ids: np.ndarray, texts: dict[int, str],
) -> list[str]:  # fmt: skip
    """One text per flagged window: the numbers and the templates that deviate most."""
    out: list[str] = []
    for start in range(0, flagged.height, CHUNK):
        part = flagged.slice(start, CHUNK)
        counts = matrix[part["row"].to_numpy()].toarray()
        lines = counts.sum(axis=1, keepdims=True)
        h = np.sqrt(counts / np.where(lines > 0, lines, 1.0))
        peer = centroids.matrix[centroids.rows(part["host"].to_list())]
        delta = h - peer
        sq = delta**2
        share = sq / np.where(sq.sum(axis=1, keepdims=True) > 0, sq.sum(axis=1, keepdims=True), 1.0)
        top = np.argsort(-sq, axis=1)[:, :TOP]
        for i, row in enumerate(part.iter_rows(named=True)):
            names = []
            for j in top[i]:
                tid = int(template_ids[j])
                text = short(texts.get(tid, ""), TEXT_CHARS)
                names.append(
                    f"T{tid} {share[i, j] * 100:.0f}% ({h[i, j] ** 2 * 100:.0f}% of lines vs "
                    f"{peer[i, j] ** 2 * 100:.0f}% at peers) {text}"
                )
            out.append(
                f"z {row['z']:.1f}: distance to peers {row['distance']:.2f} (host usually "
                f"{row['median']:.2f}), {row['lines']:.0f} lines; templates: " + "; ".join(names)
            )
    return out


@register
class Contextual:
    key = "contextual"
    type = "contextual"
    title = "Host against its peers"
    version = f"1:{WINDOW_S}:{THRESHOLD}:{MIN_LINES}:{MIN_SHARE}:{MAD_FLOOR}:{TOP}"
    setting_names: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = (hosts.KEY, templates.KEY)
    threshold = THRESHOLD

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        store = ctx.store
        assigned = hosts.load_assignments(store)
        centroids = hosts.load_peer_centroids(store)
        baseline = hosts.load_baseline(store)
        template_ids = hosts.stage_template_ids(store, None)
        unscored = int((~assigned["peer_ok"]).sum())
        counts = pl.read_parquet(store.path(templates.KEY, "template_counts_host.parquet"))
        counts = counts.filter(pl.col("host").is_in(list(centroids.keys)))
        windows, matrix = hosts.window_matrix(counts, template_ids)
        scored = scored_windows(windows, matrix, centroids, baseline)
        quiet = baseline.height - scored["host"].n_unique()
        flagged = scored.filter(pl.col("z") >= THRESHOLD)
        ctx.meta.update(
            {
                "unscored_hosts": unscored,
                "quiet_hosts": quiet,
                "min_lines": MIN_LINES,
                "windows": windows.height,
                "scored_windows": scored.height,
                "note": (
                    f"{unscored} of {assigned.height} hosts have no valid peer group and are not "
                    f"scored; windows with fewer than {MIN_LINES} lines are not scored "
                    f"({scored.height:,} of {windows.height:,} windows were, {quiet} hosts never "
                    "reach that)"
                ),
            }
        )
        ctx.log(
            f"contextual: {flagged.height} of {scored.height} scored windows above threshold; "
            f"{unscored} hosts without peers not scored, {quiet} too quiet"
        )
        texts = features.template_texts(store)
        notes = evidence(flagged, matrix, centroids, template_ids, texts)
        return flagged.select(
            "bin",
            pl.lit(WINDOW_S, dtype=pl.Int64).alias("window_s"),
            pl.lit("host").alias("kind"),
            pl.col("host").alias("entity"),
            pl.col("z").alias("raw"),
            pl.Series("evidence", notes, dtype=pl.String),
        )
