"""Contextual anomalies: a host's 1-minute template mix against the mix of its peers.

This is the *contextual* type of ``PLAN.md`` module 6: a window that is normal for the fleet but
not for the host's own kind of host (or the reverse). It builds on module 7 (``analyses/hosts``).
A window's counts become ``h = sqrt(c / sum(c))`` and its distance to the host's peer centroid is
``centroid_distance``. Three steps turn that into the raw score:

1. *Expected distance.* The distance of a window of ``n`` lines carries multinomial noise that
   falls with ``1 / sqrt(n)``, so each host gets its own fit ``expected(n) = a + b / sqrt(n)``
   (least squares, refitted three times without the windows more than 3 robust sigmas above the
   fit; ``b`` is not allowed to be negative). The scale is the scaled MAD of the residuals, at
   least ``MAD_FLOOR`` of the host's median distance, and widens by ``sqrt(median lines / n)``
   (limited to a factor ``SCALE_CLIP`` either way), so a sparse window needs a stronger
   deviation. ``u = (distance - expected) / scale`` is the host's standing against its own past.
2. *Peers in the same minute.* The centroid is a whole-day mean, so an event that hits many
   hosts at once makes all of them deviate from it. ``z = u - s`` where ``s`` is the median ``u`` of
   the host's peers (``hosts.load_peers``) that have a window in the same minute, or of the whole
   fleet when fewer than ``MIN_PEER_WINDOWS`` do. A host is only flagged when it deviates and its
   peers do not.
3. *Threshold and evidence.* ``z >= THRESHOLD`` is the candidate rule. 8 is about the 99th
   percentile of the pooled ``z`` on the real data (8.4, 99.9th: 17), i.e. about one window in a
   hundred before the evidence rule; the observed quantiles are in the detector's meta so a
   change of data shows. A candidate is kept only when the template that contributes most to its
   squared distance has at least ``MIN_EVIDENCE_LINES`` lines: one or two stray lines of a rare
   template are noise, not a contextual finding.

Peers are the host's nearest hosts within a fixed radius, not its k-means cluster. A host with no
valid peer group has no peer centroid and is **not scored**; their number and names go to the
detector's meta (``unscored_hosts``, ``unscored_names``) and the count to the page. Windows of
fewer than ``max(MIN_LINES, MIN_SHARE * median lines of the host)`` lines are not scored either,
and hosts with fewer than ``MIN_HOST_WINDOWS`` scored windows have no fit (``quiet_hosts``).

The evidence names the templates that contribute most to the squared distance of the window:
their share of the window and the peers' profile (the peer centroid squared; the centroid is a
mean of ``sqrt(share)``, so this is at most the peers' mean share and is shown as a profile, not
as a share). Ties are broken by line count, and the line counts are shown.
"""

from __future__ import annotations

import numpy as np
import polars as pl
from scipy import sparse

from logdash.analyses import features, hosts, templates
from logdash.analyses.templates import short
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60
THRESHOLD = 8.0  # z-score against the host's expected distance and its peers (see docstring)
MIN_LINES = 30  # windows with fewer lines are not scored
MIN_SHARE = 0.5  # ... nor those below this share of the host's median lines per window
MIN_HOST_WINDOWS = 10  # scored windows a host needs for its fit
MIN_PEER_WINDOWS = 3  # peers with a window in the minute, else the fleet is the comparison
MIN_EVIDENCE_LINES = 3  # lines of the template that contributes most to the distance
MAD_FLOOR = hosts.MAD_FLOOR  # the scale is at least this share of the median distance
SCALE_CLIP = (0.5, 2.0)  # limits of the sqrt(median lines / lines) factor on the scale
FIT_ROUNDS = 3
FIT_SIGMA = 3.0  # windows further above the fit than this are left out of the next fit
TOP = 3  # templates named in the evidence
TEXT_CHARS = 50
CHUNK = 2_000  # windows densified at once for the evidence


def fit_host(lines: np.ndarray, distance: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``expected, scale`` of the windows of one host (see the module docstring)."""
    x = 1 / np.sqrt(lines)
    keep = np.ones(len(x), dtype=bool)
    a, b = float(np.median(distance)), 0.0
    for _ in range(FIT_ROUNDS):
        if np.ptp(x[keep]) > 1e-9:
            slope, intercept = np.polyfit(x[keep], distance[keep], 1)
            a, b = (
                (float(intercept), float(slope))
                if slope > 0
                else (float(np.median(distance[keep])), 0.0)
            )
        resid = distance - (a + b * x)
        centre = np.median(resid[keep])
        sigma = max(1.4826 * np.median(np.abs(resid[keep] - centre)), 1e-9)
        keep = resid < centre + FIT_SIGMA * sigma
        if keep.sum() < MIN_HOST_WINDOWS:
            break
    expected = a + b * x
    resid = distance - expected
    mad = 1.4826 * np.median(np.abs(resid - np.median(resid)))
    noise = np.clip(np.sqrt(np.median(lines) / lines), *SCALE_CLIP)
    scale = np.maximum(mad, MAD_FLOOR * np.median(distance)) * noise
    return expected, np.maximum(scale, 1e-9)


def standing(frame: pl.DataFrame) -> pl.DataFrame:
    """``frame`` (``host, bin, lines, distance``) plus ``expected`` and ``u``, fitted hosts only."""
    parts = []
    for part in frame.partition_by("host", maintain_order=True):
        if part.height < MIN_HOST_WINDOWS:
            continue
        expected, scale = fit_host(part["lines"].to_numpy(), part["distance"].to_numpy())
        parts.append(
            part.with_columns(
                expected=pl.Series(expected),
                u=pl.Series((part["distance"].to_numpy() - expected) / scale),
            )
        )
    return (
        pl.concat(parts)
        if parts
        else frame.with_columns(
            expected=pl.lit(None, dtype=pl.Float64), u=pl.lit(None, dtype=pl.Float64)
        )
    )


def relative_to_peers(frame: pl.DataFrame, peers: pl.DataFrame) -> pl.DataFrame:
    """``frame`` (with ``u``) plus ``peer_u`` and ``z = u - peer_u``.

    ``peer_u`` is the median ``u`` of the host's peers in the same minute.

    The fleet's median ``u`` of the minute replaces it when fewer than ``MIN_PEER_WINDOWS`` peers
    have a window, and zero when the fleet is that small too.
    """
    theirs = (
        peers.select("host", "peer")
        .join(frame.select(pl.col("host").alias("peer"), "bin", pl.col("u").alias("pu")), on="peer")
        .group_by("host", "bin")
        .agg(peer_u=pl.col("pu").median(), k=pl.len())
    )
    fleet = frame.group_by("bin").agg(fleet_u=pl.col("u").median(), n_fleet=pl.len())
    return (
        frame.join(theirs, on=["host", "bin"], how="left")
        .join(fleet, on="bin")
        .with_columns(
            peer_u=pl.when(pl.col("k") >= MIN_PEER_WINDOWS)
            .then(pl.col("peer_u"))
            .when(pl.col("n_fleet") > MIN_PEER_WINDOWS)
            .then(pl.col("fleet_u"))
            .otherwise(0.0)
        )
        .with_columns(z=pl.col("u") - pl.col("peer_u"))
        .drop("k", "fleet_u", "n_fleet")
    )


def scored_windows(
    windows: pl.DataFrame, matrix: sparse.csr_matrix, centroids: hosts.Centroids,
    peers: pl.DataFrame,
) -> pl.DataFrame:  # fmt: skip
    """``row, host, bin, lines, distance, expected, z`` of every window that is scored.

    ``row`` is the window's row in ``matrix``. Windows too small for their host, and hosts with
    too few windows for a fit, are left out (see the module docstring).
    """
    lines = np.asarray(matrix.sum(axis=1)).ravel()
    distance = hosts.centroid_distance(matrix, windows["host"].to_list(), centroids)
    frame = windows.with_columns(
        row=pl.int_range(pl.len(), dtype=pl.Int64),
        lines=pl.Series(lines),
        distance=pl.Series(distance),
    )
    median_lines = frame.group_by("host").agg(median_lines=pl.col("lines").median())
    frame = (
        frame.join(median_lines, on="host")
        .filter(pl.col("lines") >= pl.max_horizontal(MIN_LINES, MIN_SHARE * pl.col("median_lines")))
        .drop("median_lines")
    )
    scored = relative_to_peers(standing(frame), peers)
    return scored.select("row", "host", "bin", "lines", "distance", "expected", "z")


def evidence(
    flagged: pl.DataFrame, matrix: sparse.csr_matrix, centroids: hosts.Centroids,
    template_ids: np.ndarray, texts: dict[int, str],
) -> tuple[list[str], np.ndarray]:  # fmt: skip
    """One text per flagged window, and the line count of its leading template."""
    out: list[str] = []
    leading: list[int] = []
    for start in range(0, flagged.height, CHUNK):
        part = flagged.slice(start, CHUNK)
        counts = matrix[part["row"].to_numpy()].toarray()
        lines = counts.sum(axis=1, keepdims=True)
        h = np.sqrt(counts / np.where(lines > 0, lines, 1.0))
        peer = centroids.matrix[centroids.rows(part["host"].to_list())]
        sq = (h - peer) ** 2
        total = sq.sum(axis=1, keepdims=True)
        share = sq / np.where(total > 0, total, 1.0)
        # largest contribution first; equal contributions (one-line templates) by line count
        top = np.lexsort((-counts, -np.round(sq, 9)), axis=1)[:, :TOP]
        for i, row in enumerate(part.iter_rows(named=True)):
            names = []
            for j in top[i]:
                tid = int(template_ids[j])
                text = short(texts.get(tid, ""), TEXT_CHARS)
                names.append(
                    f"T{tid} {share[i, j] * 100:.0f}% ({counts[i, j]:.0f} lines, "
                    f"{h[i, j] ** 2 * 100:.0f}% of the window, peer profile "
                    f"{peer[i, j] ** 2 * 100:.1f}%) {text}"
                )
            leading.append(int(counts[i, top[i, 0]]))
            out.append(
                f"z {row['z']:.1f}: distance to peers {row['distance']:.2f} (expected "
                f"{row['expected']:.2f} for {row['lines']:.0f} lines); templates: "
                + "; ".join(names)
            )
    return out, np.array(leading, dtype=np.int64)


@register
class Contextual:
    key = "contextual"
    type = "contextual"
    title = "Host against its peers"
    version = (
        f"2:{WINDOW_S}:{THRESHOLD}:{MIN_LINES}:{MIN_SHARE}:{MIN_HOST_WINDOWS}:{MIN_PEER_WINDOWS}:"
        f"{MIN_EVIDENCE_LINES}:{MAD_FLOOR}:{SCALE_CLIP}:{FIT_ROUNDS}:{FIT_SIGMA}:{TOP}"
    )
    setting_names: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = (hosts.KEY, templates.KEY)
    threshold = THRESHOLD

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        store = ctx.store
        assigned = hosts.load_assignments(store)
        centroids = hosts.load_peer_centroids(store)
        peers = hosts.load_peers(store)
        template_ids = hosts.stage_template_ids(store, None)
        unscored = sorted(assigned.filter(~pl.col("peer_ok"))["host"].to_list())
        counts = pl.read_parquet(store.path(templates.KEY, "template_counts_host.parquet"))
        counts = counts.filter(pl.col("host").is_in(list(centroids.keys)))
        windows, matrix = hosts.window_matrix(counts, template_ids)
        scored = scored_windows(windows, matrix, centroids, peers)
        quiet = len(centroids.keys) - scored["host"].n_unique()
        candidates = scored.filter(pl.col("z") >= THRESHOLD)
        texts = features.template_texts(store)
        notes, leading = evidence(candidates, matrix, centroids, template_ids, texts)
        flagged = candidates.with_columns(evidence=pl.Series(notes, dtype=pl.String))
        flagged = flagged.filter(pl.Series(leading >= MIN_EVIDENCE_LINES, dtype=pl.Boolean))
        quantiles = scored["z"].quantile(0.99), scored["z"].quantile(0.999)
        ctx.meta.update(
            {
                "unscored_hosts": len(unscored),
                "unscored_names": unscored,
                "quiet_hosts": quiet,
                "min_lines": MIN_LINES,
                "windows": windows.height,
                "scored_windows": scored.height,
                "candidates": candidates.height,
                "z_q99": None if quantiles[0] is None else round(float(quantiles[0]), 2),
                "z_q999": None if quantiles[1] is None else round(float(quantiles[1]), 2),
                "note": (
                    f"{len(unscored)} of {assigned.height} hosts have no valid peer group and are "
                    f"not scored (named in the detector's meta.json entry); windows with fewer "
                    f"than {MIN_LINES} lines are not scored ({scored.height:,} of "
                    f"{windows.height:,} windows were, {quiet} hosts too quiet for a fit); a host "
                    "is compared with its peers in the same minute, so events that hit many hosts "
                    "at once are left to the other detectors"
                ),
            }
        )
        ctx.log(
            f"contextual: {flagged.height} of {scored.height} scored windows flagged "
            f"({candidates.height} above threshold); {len(unscored)} hosts without peers not "
            f"scored, {quiet} too quiet"
        )
        return flagged.select(
            "bin",
            pl.lit(WINDOW_S, dtype=pl.Int64).alias("window_s"),
            pl.lit("host").alias("kind"),
            pl.col("host").alias("entity"),
            pl.col("z").alias("raw"),
            "evidence",
        )
