"""Module 7: host profiles, host clusters, outlier hosts and the service x host matrix.

Built from ``templates/template_counts_host.parquet`` (template x minute x host) and, for the
service x host matrix, one aggregate over ``events``. The page only reads the files below.

A host *profile* is its template mix over the whole span: the share of each template among the
host's lines. Profiles are compared in the Hellinger space ``h = sqrt(share)`` (the square root
keeps a few dominant templates from deciding every distance; Euclidean distance there is the
Hellinger distance between mixes, up to a constant). Everything stored lives in that space:

- ``hosts.parquet``, one row per host in the row order of ``linkage.parquet``: ``row, host,
  events, cluster, hier_cluster, distance, z, outlier, leaf, x, y``. ``cluster`` is the k-means
  cluster (1 is the largest), ``hier_cluster`` the Ward tree cut into the same number of
  clusters, ``distance`` the Euclidean distance of the host's ``h`` to its cluster's centroid,
  ``z`` its robust z-score among the cluster's hosts, ``outlier`` is ``z > OUTLIER_Z``, ``leaf``
  the position in the dendrogram and ``x, y`` a PCA projection of ``h`` for plotting;
- ``clusters.parquet``: ``cluster, n_hosts, median_distance, scale, max_distance, outliers``.
  ``scale`` is ``max(1.4826 * MAD, 0.1 * median, 1e-9)`` of the member distances, so that
  ``(distance - median_distance) / scale`` is ``z``. Every cluster has at least ``MIN_PEERS``
  hosts, except when fewer than that many hosts exist (one cluster, ``z`` is 0: no peers);
- ``centroids.parquet``, long and sparse: ``cluster, template_id, share, root``. ``root`` is the
  centroid in the clustering space (mean of the members' ``h``, what distances are measured to),
  ``share`` the members' mean template share (what to show). Rows with both zero are left out;
  read it as a matrix with ``load_centroids``, whose columns follow any template ids you give;
- ``deviations.parquet``: ``row, host, rank, template_id, delta, share``, the ``TOP_DEVIATIONS``
  templates that contribute most to the squared distance of each host (``delta`` is
  ``h - centroid`` for that template, ``share`` its part of the squared distance);
- ``linkage.parquet``: the Ward linkage matrix ``left, right, distance, count`` (scipy format,
  indices refer to ``row``), ``service_host.parquet``: ``host, service, n`` and ``fit.json``
  (chosen ``k``, silhouette per ``k``, agreement between the two clusterings, template ids).

For the contextual detector (host windows against peers): a window's count vector ``c`` becomes
``h = sqrt(c / sum(c))``, and its deviation is the distance to the centroid of the host's
``cluster`` (``centroid_distance`` does this for sparse or dense rows). Windows are far noisier
than a whole-span profile, so judge them against their own distribution (for example per
cluster), not against ``clusters.parquet``, which describes whole-span host distances.
Read it with ``load_assignments``, ``load_centroids`` and ``load_clusters``.

k-means (scikit-learn, fixed seed, ``N_INIT`` restarts) is tried for ``k`` from 2 to ``MAX_K`` and
the ``k`` with the highest silhouette wins (the smaller one on a tie), among those where every
cluster has at least ``MIN_PEERS`` hosts, so that a lone odd host is scored against peers
instead of becoming a cluster of its own. With no such ``k`` all hosts form one cluster.
The dendrogram of the page is drawn from the stored linkage; with
162 hosts it is a tall scrolling plot with every host named, or a collapsed view with one leaf
per cluster of the cut.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dash_mantine_components as dmc
import numpy as np
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component
from scipy import sparse
from scipy.cluster.hierarchy import dendrogram, fcluster, leaves_list, linkage
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import adjusted_rand_score, silhouette_score

from logdash.analyses import features
from logdash.analyses.base import ComputeContext, register
from logdash.analyses.features import Lru
from logdash.analyses.templates import MISSING
from logdash.app import figures, ui
from logdash.app.filters import FILTER_STORE_ID, Filters, from_dict, page_url
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore

KEY = "hosts"
UPSTREAM = "templates"
MAX_K = 10  # largest k tried
MIN_PEERS = 3  # every cluster has at least this many hosts (and so a z-score)
SEED = 0
N_INIT = 10
OUTLIER_Z = 3.5  # robust z-score above which a host is an outlier of its cluster
MAD_SCALE = 1.4826
MAD_FLOOR = 0.1  # scale is at least this share of the median distance
TOP_DEVIATIONS = 3
VIEWS = ("full", "collapsed")
DEFAULT_VIEW = "full"
MODES = ("count", "share")
DEFAULT_MODE = "count"
ROW_PX = 15  # height of one host in the tall plots
OUTLIER_ROWS = 25
LABELED_OUTLIERS = 10
TEXT_CHARS = 60
MEMBERS_SHOWN = 8
EXAMPLES = 3

# --------------------------------------------------------------------------- compute


def profile_counts(counts: pl.DataFrame, template_ids: np.ndarray) -> tuple[list[str], np.ndarray]:
    """Hosts (sorted) and their ``hosts x templates`` count matrix over all minutes."""
    totals = counts.group_by("host", "template_id").agg(pl.col("n").sum())
    hosts = sorted(totals["host"].unique().to_list())
    matrix = np.zeros((len(hosts), len(template_ids)))
    if totals.height:
        rows = totals["host"].replace_strict({h: i for i, h in enumerate(hosts)}).to_numpy()
        cols = np.searchsorted(template_ids, totals["template_id"].to_numpy())
        matrix[rows, cols] = totals["n"].to_numpy()
    return hosts, matrix


def root_mix(counts: np.ndarray) -> np.ndarray:
    """Hellinger transform of count rows: ``sqrt(c / sum(c))``, zero for an empty row."""
    total = counts.sum(axis=1, keepdims=True)
    return np.sqrt(counts / np.where(total > 0, total, 1.0))


def centroid_distance(
    counts: np.ndarray | sparse.spmatrix, clusters: np.ndarray, centroids: np.ndarray
) -> np.ndarray:
    """Distance of each count row's Hellinger vector to ``centroids[clusters[row]]``.

    ``counts`` is ``rows x templates`` (dense or sparse), ``centroids`` is ``k x templates`` in
    the same column order (``load_centroids``), ``clusters`` indexes its rows (0-based). Uses
    ``|h - c|^2 = |h|^2 - 2 h.c + |c|^2`` with ``|h|^2 = 1`` for a non-empty row, so sparse
    windows are never densified.
    """
    clusters = np.asarray(clusters, dtype=np.int64)
    csr = sparse.csr_matrix(counts, dtype=np.float64)
    total = np.asarray(csr.sum(axis=1)).ravel()
    scaled = sparse.diags(1 / np.where(total > 0, total, 1.0)) @ csr
    scaled.data = np.sqrt(scaled.data)
    dots = np.asarray(scaled @ centroids.T)
    own = dots[np.arange(len(clusters)), clusters]
    norm_c = (centroids**2).sum(axis=1)[clusters]
    norm_h = (total > 0).astype(np.float64)
    return np.sqrt(np.maximum(norm_h - 2 * own + norm_c, 0.0))


def cluster_hosts(vectors: np.ndarray, k_max: int = MAX_K) -> tuple[np.ndarray, dict[int, float]]:
    """k-means labels (0-based, unordered) with the best silhouette and the score per ``k``.

    Only ``k`` for which every cluster keeps at least ``MIN_PEERS`` hosts is considered: a lone
    odd host would otherwise form a cluster of its own (silhouette rewards that) and have no
    peers to deviate from. Without such a ``k`` all hosts form one cluster.
    """
    n = len(vectors)
    scores: dict[int, float] = {}
    best: tuple[float, np.ndarray] | None = None
    for k in range(2, min(k_max, n // MIN_PEERS) + 1):
        with warnings.catch_warnings():  # duplicate profiles cannot fill k clusters
            warnings.simplefilter("ignore", ConvergenceWarning)
            labels = KMeans(k, n_init=N_INIT, random_state=SEED).fit_predict(vectors)
        sizes = np.unique(labels, return_counts=True)[1]
        if len(sizes) < k or sizes.min() < MIN_PEERS:
            continue
        scores[k] = float(silhouette_score(vectors, labels))
        if best is None or scores[k] > best[0]:
            best = (scores[k], labels)
    return (best[1] if best else np.zeros(n, dtype=np.int64)), scores


def by_size(labels: np.ndarray) -> np.ndarray:
    """Relabel to 1..k, the largest cluster first (ties by the first host)."""
    if len(labels) == 0:
        return labels.astype(np.int64)
    uniq, first, sizes = np.unique(labels, return_index=True, return_counts=True)
    order = np.lexsort((first, -sizes))
    mapping = {int(uniq[i]): rank + 1 for rank, i in enumerate(order)}
    return np.array([mapping[int(label)] for label in labels], dtype=np.int64)


def centroids_of(vectors: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Mean vector per cluster; row ``c - 1`` belongs to cluster ``c``."""
    k = int(labels.max()) if len(labels) else 0
    return np.array([vectors[labels == c].mean(axis=0) for c in range(1, k + 1)]).reshape(
        k, vectors.shape[1]
    )


def robust_scores(distance: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, pl.DataFrame]:
    """Robust z-score per host within its cluster and the per-cluster distance statistics."""
    z = np.zeros(len(distance))
    rows = []
    for c in range(1, int(labels.max()) + 1 if len(labels) else 1):
        members = labels == c
        d = distance[members]
        median = float(np.median(d))
        mad = float(np.median(np.abs(d - median)))
        scale = max(MAD_SCALE * mad, MAD_FLOOR * median, 1e-9)
        if members.sum() >= MIN_PEERS:
            z[members] = (d - median) / scale
        rows.append(
            {
                "cluster": c,
                "n_hosts": int(members.sum()),
                "median_distance": median,
                "scale": scale,
                "max_distance": float(d.max()),
                "outliers": int((z[members] > OUTLIER_Z).sum()),
            }
        )
    schema = {
        "cluster": pl.Int64, "n_hosts": pl.Int64, "median_distance": pl.Float64,
        "scale": pl.Float64, "max_distance": pl.Float64, "outliers": pl.Int64,
    }  # fmt: skip
    return z, pl.DataFrame(rows, schema=schema)


def top_deviations(
    vectors: np.ndarray, labels: np.ndarray, centroids: np.ndarray, hosts: list[str],
    template_ids: np.ndarray,
) -> pl.DataFrame:  # fmt: skip
    """Per host the templates that contribute most to its squared distance to the centroid."""
    schema = {
        "row": pl.Int64, "host": pl.String, "rank": pl.Int64, "template_id": pl.Int64,
        "delta": pl.Float64, "share": pl.Float64,
    }  # fmt: skip
    if len(vectors) == 0 or vectors.shape[1] == 0:
        return pl.DataFrame(schema=schema)
    delta = vectors - centroids[labels - 1]
    square = delta**2
    n_top = min(TOP_DEVIATIONS, square.shape[1])
    best = np.argsort(-square, axis=1, kind="stable")[:, :n_top]
    values = np.take_along_axis(square, best, axis=1)
    total = square.sum(axis=1)
    frame = pl.DataFrame(
        {
            "row": np.repeat(np.arange(len(vectors)), n_top),
            "host": np.repeat(np.asarray(hosts, dtype=object), n_top),
            "rank": np.tile(np.arange(1, n_top + 1), len(vectors)),
            "template_id": template_ids[best].ravel(),
            "delta": np.take_along_axis(delta, best, axis=1).ravel(),
            "share": (values / np.where(total > 0, total, 1.0)[:, None]).ravel(),
        },
        schema=schema,
    )
    return frame.filter(pl.col("delta") != 0)


def centroid_frame(
    centroids: np.ndarray, labels: np.ndarray, vectors: np.ndarray, counts: np.ndarray,
    template_ids: np.ndarray,
) -> pl.DataFrame:  # fmt: skip
    """Long centroids: ``cluster, template_id, share, root`` without all-zero entries."""
    shares = counts / np.where(
        counts.sum(axis=1, keepdims=True) > 0, counts.sum(1, keepdims=True), 1
    )
    parts = []
    for c in range(1, len(centroids) + 1):
        share = shares[labels == c].mean(axis=0)
        keep = np.flatnonzero((share > 0) | (centroids[c - 1] > 0))
        parts.append(
            pl.DataFrame(
                {
                    "cluster": np.full(len(keep), c, dtype=np.int64),
                    "template_id": template_ids[keep],
                    "share": share[keep],
                    "root": centroids[c - 1][keep],
                },
                schema={
                    "cluster": pl.Int64,
                    "template_id": pl.Int64,
                    "share": pl.Float64,
                    "root": pl.Float64,
                },
            )  # fmt: skip
        )
    return pl.concat(parts) if parts else pl.DataFrame(
        schema={"cluster": pl.Int64, "template_id": pl.Int64, "share": pl.Float64,
                "root": pl.Float64}
    )  # fmt: skip


@dataclass(frozen=True)
class Result:
    """Everything the stage writes, in memory."""

    hosts: pl.DataFrame
    clusters: pl.DataFrame
    centroids: pl.DataFrame
    deviations: pl.DataFrame
    linkage: np.ndarray
    fit: dict[str, Any]


def analyse(hosts: list[str], counts: np.ndarray, template_ids: np.ndarray) -> Result:
    """Cluster the host profiles and score every host against its cluster."""
    n = len(hosts)
    vectors = root_mix(counts)
    raw, silhouette = cluster_hosts(vectors)
    labels = by_size(raw)
    centroids = centroids_of(vectors, labels)
    distance = np.linalg.norm(vectors - centroids[labels - 1], axis=1) if n else np.zeros(0)
    z, clusters = robust_scores(distance, labels)
    k = int(labels.max()) if n else 0
    tree = linkage(vectors, "ward") if n >= 2 else np.zeros((0, 4))
    hier = by_size(fcluster(tree, k, "maxclust")) if n >= 2 and k >= 1 else np.ones(n, np.int64)
    leaf = np.empty(n, dtype=np.int64)
    leaf[leaves_list(tree) if n >= 2 else np.arange(n)] = np.arange(n)
    planar = PCA(n_components=min(2, n, vectors.shape[1])).fit(vectors) if n >= 2 else None
    xy = np.zeros((n, 2))
    if planar is not None and planar.n_components_ > 0:
        coords = planar.transform(vectors)
        xy[:, : coords.shape[1]] = coords
    frame = pl.DataFrame(
        {
            "row": np.arange(n, dtype=np.int64),
            "host": hosts,
            "events": counts.sum(axis=1).astype(np.int64),
            "cluster": labels,
            "hier_cluster": hier,
            "distance": distance,
            "z": z,
            "outlier": z > OUTLIER_Z,
            "leaf": leaf,
            "x": xy[:, 0],
            "y": xy[:, 1],
        },
        schema={
            "row": pl.Int64, "host": pl.String, "events": pl.Int64, "cluster": pl.Int64,
            "hier_cluster": pl.Int64, "distance": pl.Float64, "z": pl.Float64,
            "outlier": pl.Boolean, "leaf": pl.Int64, "x": pl.Float64, "y": pl.Float64,
        },
    )  # fmt: skip
    fit = {
        "n_hosts": n,
        "n_templates": len(template_ids),
        "k": k,
        "silhouette": {str(key): value for key, value in silhouette.items()},
        "ari": float(adjusted_rand_score(labels, hier)) if n >= 2 else None,
        "outlier_z": OUTLIER_Z,
        "min_peers": MIN_PEERS,
        "projection_explained": planar.explained_variance_ratio_.tolist() if planar else [],
        "template_ids": [int(i) for i in template_ids],
        "transform": "sqrt(template share)",
    }
    return Result(
        frame,
        clusters,
        centroid_frame(centroids, labels, vectors, counts, template_ids),
        top_deviations(vectors, labels, centroids, hosts, template_ids),
        tree,
        fit,
    )


def write_result(out_dir: Path, result: Result) -> None:
    for name, frame in (
        ("hosts", result.hosts),
        ("clusters", result.clusters),
        ("centroids", result.centroids),
        ("deviations", result.deviations),
    ):
        frame.write_parquet(out_dir / f"{name}.parquet", compression="zstd")
    pl.DataFrame(
        result.linkage, schema=dict.fromkeys(("left", "right", "distance", "count"), pl.Float64)
    ).write_parquet(out_dir / "linkage.parquet", compression="zstd")
    (out_dir / "fit.json").write_text(json.dumps(result.fit))


def compute_all(
    ctx: ComputeContext, source_dir: Path, log: Callable[[str], None] = print
) -> dict[str, int]:
    """Profiles, clusters and the service x host matrix; returns row counts."""
    template_ids = np.sort(
        pl.read_parquet(source_dir / "templates.parquet")["template_id"].to_numpy()
    )
    counts = pl.read_parquet(source_dir / "template_counts_host.parquet")
    hosts, matrix = profile_counts(counts, template_ids)
    result = analyse(hosts, matrix, template_ids)
    write_result(ctx.out_dir, result)
    pairs = ctx.con.execute(
        f"""SELECT coalesce(host, '{MISSING}') AS host,
                   coalesce(program, '{MISSING}') AS service, count(*)::BIGINT AS n
            FROM events GROUP BY ALL ORDER BY host, service"""
    ).pl()
    pairs.write_parquet(ctx.out_dir / "service_host.parquet", compression="zstd")
    outliers = int(result.hosts["outlier"].sum())
    log(
        f"{len(hosts)} hosts, k={result.fit['k']} "
        f"(silhouette {max(result.fit['silhouette'].values(), default=0):.2f}), "
        f"{outliers} outlier hosts, {pairs.height} service x host pairs"
    )
    return {"hosts": len(hosts), "outliers": outliers, "pairs": pairs.height}


# ------------------------------------------------------------------------ loading


@dataclass(frozen=True)
class Profile:
    """The stored stage in memory."""

    hosts: pl.DataFrame
    clusters: pl.DataFrame
    centroids: pl.DataFrame
    deviations: pl.DataFrame
    linkage: np.ndarray
    service_host: pl.DataFrame
    fit: dict[str, Any]

    @property
    def k(self) -> int:
        return int(self.fit["k"])


FILES = (
    "hosts.parquet", "clusters.parquet", "centroids.parquet", "deviations.parquet",
    "linkage.parquet", "service_host.parquet", "fit.json",
)  # fmt: skip
_PROFILES: Lru[Profile] = Lru(2)


def clear_caches() -> None:
    _PROFILES.items.clear()


def load_profile(store: CacheStore) -> Profile:
    """The whole stage, memoised until a file changes."""
    stamp = tuple(store.path(KEY, name).stat().st_mtime_ns for name in FILES)

    def make() -> Profile:
        link = store.read_parquet(KEY, "linkage.parquet").to_numpy().reshape(-1, 4)
        return Profile(
            store.read_parquet(KEY, "hosts.parquet"),
            store.read_parquet(KEY, "clusters.parquet"),
            store.read_parquet(KEY, "centroids.parquet"),
            store.read_parquet(KEY, "deviations.parquet"),
            link,
            store.read_parquet(KEY, "service_host.parquet"),
            store.read_json(KEY, "fit.json"),
        )

    return _PROFILES.get((str(store.root), stamp), make)


def load_assignments(store: CacheStore) -> pl.DataFrame:
    """``host, cluster, distance, z, outlier, events``: the peer group of every host."""
    return store.read_parquet(KEY, "hosts.parquet").select(
        "host", "cluster", "distance", "z", "outlier", "events"
    )


def load_clusters(store: CacheStore) -> pl.DataFrame:
    """``cluster, n_hosts, median_distance, scale, max_distance, outliers`` per cluster."""
    return store.read_parquet(KEY, "clusters.parquet")


def load_centroids(
    store: CacheStore, template_ids: np.ndarray | None = None, column: str = "root"
) -> tuple[np.ndarray, np.ndarray]:
    """Cluster ids (ascending) and their centroids as a ``k x templates`` matrix.

    ``column`` is ``root`` (clustering space, what ``centroid_distance`` expects) or ``share``.
    Columns follow ``template_ids`` (default: the stage's own, from ``fit.json``); templates the
    stage never saw are zero. Row ``i`` belongs to ``clusters[i]``; use ``clusters.searchsorted``
    to turn a host's ``cluster`` into a row.
    """
    long = store.read_parquet(KEY, "centroids.parquet")
    ids = (
        np.asarray(store.read_json(KEY, "fit.json")["template_ids"], dtype=np.int64)
        if template_ids is None
        else np.asarray(template_ids, dtype=np.int64)
    )
    clusters = np.sort(store.read_parquet(KEY, "clusters.parquet")["cluster"].to_numpy())
    matrix = np.zeros((len(clusters), len(ids)))
    lookup = {int(t): i for i, t in enumerate(ids)}
    cols = np.array([lookup.get(int(t), -1) for t in long["template_id"]], dtype=np.int64)
    known = cols >= 0
    rows = np.searchsorted(clusters, long["cluster"].to_numpy())
    matrix[rows[known], cols[known]] = long[column].to_numpy()[known]
    return clusters, matrix


# ----------------------------------------------------------------------- pure helpers


def cluster_color(cluster: int) -> str:
    return figures.PALETTE[(cluster - 1) % len(figures.PALETTE)]


def cluster_label(profile: Profile, cluster: int) -> str:
    size = profile.clusters.filter(pl.col("cluster") == cluster)["n_hosts"]
    return f"cluster {cluster} ({size[0] if len(size) else 0})"


def members(tree: np.ndarray, node: int, n: int) -> list[int]:
    """Host rows below a node of the linkage (a host is its own node)."""
    found, stack = [], [node]
    while stack:
        current = stack.pop()
        if current < n:
            found.append(current)
        else:
            stack.extend(int(v) for v in tree[current - n, :2])
    return sorted(found)


def select_hosts(profile: Profile, filters: Filters) -> pl.DataFrame:
    """Hosts of the sidebar host filter (all when empty)."""
    hosts = profile.hosts
    return hosts.filter(pl.col("host").is_in(filters.hosts)) if filters.hosts else hosts


def short_text(texts: dict[int, str], template_id: int) -> str:
    return features.short(texts.get(template_id, ""), TEXT_CHARS)


def deviation_text(profile: Profile, row: int) -> tuple[str, str]:
    """Deviating templates of a host (``T5 +0.12, ...``) and the text of the first one."""
    part = profile.deviations.filter(pl.col("row") == row).sort("rank")
    text = ", ".join(f"T{t} {d:+.2f}" for t, d in part.select("template_id", "delta").iter_rows())
    return text, (str(part["template_id"][0]) if part.height else "")


# ------------------------------------------------------------------------ figures


def silhouette_figure(profile: Profile, scheme: str | None) -> go.Figure:
    scores = {int(k): v for k, v in profile.fit["silhouette"].items()}
    if not scores:
        return figures.empty_figure("fewer than 3 hosts: one cluster", scheme)
    ks = sorted(scores)
    fig = go.Figure(
        go.Bar(
            x=ks, y=[scores[k] for k in ks],
            marker_color=[figures.PALETTE[1] if k == profile.k else figures.PALETTE[0] for k in ks],
            hovertemplate="k=%{x}: %{y:.3f}<extra></extra>",
        )
    )  # fmt: skip
    fig.update_xaxes(title="k (k-means clusters)", dtick=1)
    fig.update_yaxes(title="silhouette", range=[min(0.0, min(scores.values())), 1])
    figures.add_note(fig, f"k = {profile.k} has the highest silhouette ({scores[profile.k]:.2f})")
    return figures.style(fig, scheme, height=320)


def projection_figure(profile: Profile, filters: Filters, scheme: str | None) -> go.Figure:
    hosts = profile.hosts
    if hosts.is_empty():
        return figures.empty_figure("no hosts", scheme)
    chosen = set(select_hosts(profile, filters)["host"].to_list())
    fig = go.Figure()
    for cluster in sorted(hosts["cluster"].unique().to_list()):
        part = hosts.filter((pl.col("cluster") == cluster) & pl.col("host").is_in(chosen))
        fig.add_trace(
            go.Scatter(
                x=part["x"].to_list(), y=part["y"].to_list(), mode="markers",
                name=cluster_label(profile, cluster), marker={"color": cluster_color(cluster)},
                text=[
                    f"{h}<br>distance {d:.3f}, z {z:.1f}"
                    for h, d, z in part.select("host", "distance", "z").iter_rows()
                ],
                hovertemplate="%{text}<extra></extra>",
            )
        )  # fmt: skip
    top = hosts.filter(pl.col("outlier") & pl.col("host").is_in(chosen)).sort("z", descending=True)
    if top.height:
        top = top.head(LABELED_OUTLIERS)
        fig.add_trace(
            go.Scatter(
                x=top["x"].to_list(), y=top["y"].to_list(), mode="markers+text", name="outliers",
                text=[figures.shorten_middle(h, 20) for h in top["host"]],
                textposition="top center", hoverinfo="skip",
                marker={"symbol": "diamond-open", "size": 14, "color": figures.PALETTE[1]},
            )
        )  # fmt: skip
    share = profile.fit["projection_explained"]
    fig.update_xaxes(title="PC1 of the host profiles")
    fig.update_yaxes(title="PC2")
    figures.add_note(
        fig, f"{len(chosen)} hosts; PC1 and PC2 keep {sum(share):.0%} of the profile variance"
    )
    return figures.style(fig, scheme, height=420)


def cut_height(tree: np.ndarray, k: int) -> float | None:
    """Height between the last merges that a cut into ``k`` clusters keeps and undoes."""
    n = len(tree) + 1
    if k < 2 or k > n:
        return None
    return float((tree[n - k - 1, 2] + tree[n - k, 2]) / 2) if n - k - 1 >= 0 else None


def dendrogram_figure(profile: Profile, view: str, scheme: str | None) -> tuple[go.Figure, int]:
    """Horizontal dendrogram (hosts on the vertical axis) and its pixel height.

    ``full`` names every host; ``collapsed`` stops at the clusters of the Ward cut.
    Leaf markers use the k-means cluster colours (the majority of a collapsed leaf).
    """
    tree, hosts = profile.linkage, profile.hosts
    n = hosts.height
    if n < 2:
        return figures.empty_figure("fewer than 2 hosts: no tree", scheme), 320
    cuts = int(hosts["hier_cluster"].to_numpy().max())
    collapsed = view == "collapsed" and cuts < n
    kwargs: dict[str, Any] = {"truncate_mode": "lastp", "p": max(cuts, 2)} if collapsed else {}
    layout = dendrogram(tree, no_plot=True, **kwargs)
    ys = [[(v - 5) / 10 for v in row] for row in layout["icoord"]]
    xs = [list(row) for row in layout["dcoord"]]
    line_x: list[float | None] = []
    line_y: list[float | None] = []
    for x, y in zip(xs, ys, strict=True):
        line_x.extend([*x, None])
        line_y.extend([*y, None])
    nodes = [int(v) for v in layout["leaves"]]
    cluster_of = hosts["cluster"].to_numpy()
    names = hosts["host"].to_list()
    leaf_members = [members(tree, v, n) for v in nodes]
    majority = [int(np.bincount(cluster_of[m]).argmax()) for m in leaf_members]
    if collapsed:
        labels = [names[m[0]] if len(m) == 1 else f"{len(m)} hosts" for m in leaf_members]
        hover = [
            ", ".join(names[i] for i in m[:MEMBERS_SHOWN])
            + (" ..." if len(m) > MEMBERS_SHOWN else "")
            for m in leaf_members
        ]
    else:
        labels = [figures.shorten_middle(names[v], 30) for v in nodes]
        hover = [names[v] for v in nodes]
    fig = go.Figure(
        go.Scatter(
            x=line_x, y=line_y, mode="lines", hoverinfo="skip", showlegend=False,
            line={"color": "#868e96", "width": 1},
        )
    )  # fmt: skip
    ypos = [float(i) for i in range(len(nodes))]
    for cluster in sorted(set(majority)):
        pick = [i for i, c in enumerate(majority) if c == cluster]
        fig.add_trace(
            go.Scatter(
                x=[0.0] * len(pick), y=[ypos[i] for i in pick], mode="markers",
                name=cluster_label(profile, cluster), marker={"color": cluster_color(cluster)},
                text=[hover[i] for i in pick], hovertemplate="%{text}<extra></extra>",
            )
        )  # fmt: skip
    cut = cut_height(tree, cuts)
    if cut is not None:
        fig.add_vline(x=cut, line_dash="dash", line_color=figures.PALETTE[1])
    fig.update_yaxes(
        tickmode="array",
        tickvals=ypos,
        ticktext=labels,
        autorange="reversed",
        tickfont={"size": 10},
    )
    fig.update_xaxes(title="Ward distance")
    figures.add_note(
        fig,
        (f"{cuts} clusters at the dashed cut; " if cut is not None else "")
        + (f"{len(nodes)} leaves" if collapsed else f"{n} hosts"),
    )
    return figures.style(fig, scheme, height=pixel_height(len(nodes))), pixel_height(len(nodes))


def pixel_height(rows: int) -> int:
    """Height for a plot with one labelled row per item (at least 320 px)."""
    return max(320, ROW_PX * rows + 110)


def matrix_data(profile: Profile, filters: Filters) -> tuple[list[str], list[str], np.ndarray]:
    """Hosts in dendrogram order, services by volume and the count matrix of the selection."""
    hosts = select_hosts(profile, filters).sort("leaf")["host"].to_list()
    pairs = profile.service_host.filter(pl.col("host").is_in(hosts))
    if filters.services:
        pairs = pairs.filter(pl.col("service").is_in(filters.services))
    services = (
        pairs.group_by("service")
        .agg(pl.col("n").sum())
        .sort("n", "service", descending=[True, False])
    )["service"].to_list()
    matrix = np.zeros((len(hosts), len(services)))
    if pairs.height:
        rows = pairs["host"].replace_strict({h: i for i, h in enumerate(hosts)}).to_numpy()
        cols = pairs["service"].replace_strict({s: i for i, s in enumerate(services)}).to_numpy()
        matrix[rows, cols] = pairs["n"].to_numpy()
    return hosts, services, matrix


def matrix_figure(
    profile: Profile, filters: Filters, mode: str, scheme: str | None
) -> tuple[go.Figure, int]:
    hosts, services, counts = matrix_data(profile, filters)
    if counts.size == 0 or counts.sum() == 0:
        return figures.empty_figure("no events for this selection", scheme), 320
    totals = counts.sum(axis=1, keepdims=True)
    bar: dict[str, Any]
    if mode == "share":
        values = np.where(counts > 0, counts / np.where(totals > 0, totals, 1), np.nan)
        bar = {"title": "share of the host's lines", "tickformat": ".0%"}
    else:
        values = np.where(counts > 0, np.log10(np.maximum(counts, 1)), np.nan)
        top = int(np.ceil(np.nanmax(values)))
        bar = {
            "title": "events", "tickvals": list(range(top + 1)),
            "ticktext": [f"{10**i:,}" for i in range(top + 1)],
        }  # fmt: skip
    fig = go.Figure(
        go.Heatmap(
            z=values, x=services, y=[figures.shorten_middle(h, 30) for h in hosts],
            colorscale="Viridis", colorbar=bar, xgap=1, ygap=1,
            customdata=np.stack([counts, np.repeat(totals, counts.shape[1], axis=1)], axis=-1),
            text=[[h] * len(services) for h in hosts],
            hovertemplate=(
                "%{text}<br>%{x}<br>%{customdata[0]:,.0f} of %{customdata[1]:,.0f} lines"
                "<extra></extra>"
            ),
        )
    )  # fmt: skip
    fig.update_yaxes(autorange="reversed", type="category", tickfont={"size": 10})
    fig.update_xaxes(type="category", tickangle=-45, tickfont={"size": 10}, side="top")
    figures.add_note(fig, f"{len(hosts)} hosts in dendrogram order x {len(services)} services")
    return figures.style(fig, scheme, height=pixel_height(len(hosts)) + 80), pixel_height(
        len(hosts)
    ) + 80


# ------------------------------------------------------------------------- tables


def outlier_table(
    profile: Profile, filters: Filters, texts: dict[int, str]
) -> tuple[pl.DataFrame, list[str]]:
    """Outlier hosts (or the filtered hosts) by robust z, with the deviating templates."""
    chosen = select_hosts(profile, filters)
    rows = chosen if filters.hosts else chosen.filter(pl.col("outlier"))
    rows = rows.sort("z", "host", descending=[True, False]).head(OUTLIER_ROWS)
    devs = [deviation_text(profile, int(r)) for r in rows["row"]]
    table = pl.DataFrame(
        {
            "host": rows["host"],
            "cluster": rows["cluster"],
            "events": rows["events"],
            "distance": rows["distance"].round(3),
            "z": rows["z"].round(1),
            "deviating templates": [d[0] for d in devs],
            "largest": [short_text(texts, int(d[1])) if d[1] else "" for d in devs],
        }
    )
    return table, [page_url("/explorer", host=h) for h in rows["host"]]


def cluster_table(profile: Profile, texts: dict[int, str]) -> pl.DataFrame:
    """One row per cluster: size, spread, example hosts and the templates that define it."""
    out = []
    for row in profile.clusters.sort("cluster").iter_rows(named=True):
        c = row["cluster"]
        members_ = profile.hosts.filter(pl.col("cluster") == c).sort("distance")["host"].to_list()
        top = (
            profile.centroids.filter(pl.col("cluster") == c).sort("share", descending=True).head(3)
        )
        out.append(
            {
                "cluster": c,
                "hosts": row["n_hosts"],
                "outliers": row["outliers"],
                "median distance": round(row["median_distance"], 3),
                "closest hosts": ", ".join(members_[:EXAMPLES]),
                "main templates": "; ".join(
                    f"T{t} {s:.0%} {short_text(texts, t)}"
                    for t, s in top.select("template_id", "share").iter_rows()
                ),
            }
        )
    return pl.DataFrame(
        out,
        schema={
            "cluster": pl.Int64, "hosts": pl.Int64, "outliers": pl.Int64,
            "median distance": pl.Float64, "closest hosts": pl.String, "main templates": pl.String,
        },
    )  # fmt: skip


def stat_cards(profile: Profile) -> Component:
    ari = profile.fit["ari"]
    scores = profile.fit["silhouette"]
    return ui.grid(
        [
            ui.stat_card("hosts", ui.fmt_int(profile.hosts.height)),
            ui.stat_card("services", ui.fmt_int(profile.service_host["service"].n_unique())),
            ui.stat_card("k-means clusters", str(profile.k)),
            ui.stat_card(
                "silhouette", f"{scores[str(profile.k)]:.2f}" if str(profile.k) in scores else "-"
            ),
            ui.stat_card("outlier hosts", ui.fmt_int(profile.hosts["outlier"].sum())),
            ui.stat_card("k-means vs tree (ARI)", "-" if ari is None else f"{ari:.2f}"),
        ],
        cols={"base": 2, "md": 3, "lg": 6},
    )


# ------------------------------------------------------------------------- callbacks


def unavailable(store: CacheStore) -> str | None:
    if not all(store.exists(KEY, name) for name in FILES):
        return "The hosts stage is not built; run `logdash build`."
    return None


def hint(filters: Filters) -> str:
    text = (
        "Profiles, clusters and the tree cover the whole span and every host, so the time range "
        "and level filters do not apply. "
    )
    if filters.hosts or filters.services:
        text += "The host filter selects rows and the service filter columns of the matrix."
    return text


def view_value(value: str | None) -> str:
    return value if value in VIEWS else DEFAULT_VIEW


def mode_value(value: str | None) -> str:
    return value if value in MODES else DEFAULT_MODE


def style_for(height: int) -> dict[str, Any]:
    return {"height": height}


def draw_hosts(
    store: CacheStore, filters: Filters, view: str, mode: str, scheme: str | None
) -> tuple[Any, ...]:
    """Cards, silhouette, projection, dendrogram, matrix, outlier and cluster tables, hint."""
    problem = unavailable(store)
    if problem is not None:
        empty = figures.empty_figure(problem, scheme)
        text = dmc.Text(problem)
        return text, empty, empty, empty, style_for(320), empty, style_for(320), text, text, ""
    profile = load_profile(store)
    if profile.hosts.is_empty():
        empty = figures.empty_figure("No hosts in the cache.", scheme)
        text = dmc.Text("No hosts in the cache.")
        return text, empty, empty, empty, style_for(320), empty, style_for(320), text, text, ""
    texts = features.template_texts(store)
    tree_fig, tree_px = dendrogram_figure(profile, view, scheme)
    matrix_fig, matrix_px = matrix_figure(profile, filters, mode, scheme)
    outliers, hrefs = outlier_table(profile, filters, texts)
    return (
        stat_cards(profile),
        silhouette_figure(profile, scheme),
        projection_figure(profile, filters, scheme),
        tree_fig,
        style_for(tree_px),
        matrix_fig,
        style_for(matrix_px),
        ui.link_table(outliers, hrefs) if outliers.height else dmc.Text("no outlier hosts"),
        ui.table(cluster_table(profile, texts)),
        hint(filters),
    )


def tall(graph_id: str) -> Component:
    """A tall plot inside a scrolling area (its height is set by the callback)."""
    return dmc.ScrollArea(ui.graph(graph_id, 320), h=720, type="auto")


@register
class Hosts:
    key = KEY
    title = "Hosts"
    version = "1"
    depends_on: tuple[str, ...] = (UPSTREAM,)

    def compute(self, ctx: ComputeContext) -> None:
        for name, count in compute_all(ctx, ctx.out_dir.parent / UPSTREAM, ctx.progress).items():
            ctx.note_rows(name, count)

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        segmented = features.segmented
        controls = dmc.Group(
            [
                segmented(
                    "hosts-view",
                    [("full", "tree: every host"), ("collapsed", "tree: clusters only")],
                    DEFAULT_VIEW,
                ),
                segmented(
                    "hosts-mode",
                    [("count", "matrix: events (log)"), ("share", "matrix: share of host")],
                    DEFAULT_MODE,
                ),
            ],
            gap="md",
        )
        return ui.page(
            "Hosts and services",
            controls,
            dmc.Box(id="hosts-stats"),
            ui.grid(
                [
                    ui.card("Choice of k (silhouette)", ui.graph("hosts-silhouette", 320)),
                    ui.card("Host profiles on two components", ui.graph("hosts-projection", 420)),
                ],
                cols={"base": 1, "lg": 2},
            ),
            ui.card("Clusters", dmc.Box(id="hosts-clusters")),
            ui.card("Outlier hosts (distance to the cluster centroid)", dmc.Box(id="hosts-table")),
            ui.card("Dendrogram (Ward linkage on the template mix)", tall("hosts-tree")),
            ui.card("Service x host matrix (hosts in dendrogram order)", tall("hosts-matrix")),
            dmc.Text(id="hosts-hint", size="sm", c="dimmed"),
            description=(
                "A host's profile is its template mix over the whole span, compared as the "
                "square root of the shares (Hellinger distance). k-means picks k by silhouette, "
                "the tree is Ward linkage on the same profiles, and a host is an outlier when its "
                "distance to its cluster's centroid is more than 3.5 robust standard deviations "
                "above the cluster's median. The clusters feed the contextual detector."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(
            Output("hosts-stats", "children"),
            Output("hosts-silhouette", "figure"),
            Output("hosts-projection", "figure"),
            Output("hosts-tree", "figure"),
            Output("hosts-tree", "style"),
            Output("hosts-matrix", "figure"),
            Output("hosts-matrix", "style"),
            Output("hosts-table", "children"),
            Output("hosts-clusters", "children"),
            Output("hosts-hint", "children"),
            Input("hosts-view", "value"),
            Input("hosts-mode", "value"),
            Input(FILTER_STORE_ID, "data"),
            Input(THEME_STORE_ID, "data"),
        )
        def draw(
            view: str | None,
            mode: str | None,
            filters: dict[str, Any] | None,
            theme: str | None,
        ) -> tuple[Any, ...]:
            return draw_hosts(store, from_dict(filters), view_value(view), mode_value(mode), theme)

        try:  # read the files now so that the first request does not
            if unavailable(store) is None:
                load_profile(store)
        except (OSError, ValueError):
            pass
