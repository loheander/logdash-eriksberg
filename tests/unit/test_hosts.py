import json

import numpy as np
import polars as pl
import pytest
from scipy import sparse

from logdash.analyses import features as ft
from logdash.analyses import hosts as hs
from logdash.analyses.hosts import Hosts
from logdash.analyses.templates import Templates
from logdash.app.filters import Filters
from logdash.cache.store import CacheStore
from tests.conftest import build_with


@pytest.fixture(scope="module")
def stage(built):
    build_with(built, Templates(), Hosts())
    return built.store


def planted(seed: int = 0, per_group: int = 20):
    """Three groups of hosts with different template mixes and one host in group 0 that differs."""
    rng = np.random.default_rng(seed)
    base = np.array(
        [[50, 30, 10, 5, 3, 1, 1, 0], [5, 10, 50, 30, 3, 1, 1, 0], [1, 1, 2, 3, 40, 40, 10, 3]],
        dtype=float,
    )
    rows = [rng.poisson(base[g] * 40) for g in range(3) for _ in range(per_group)]
    odd = rng.poisson(np.array([5, 5, 5, 5, 5, 5, 50, 20]) * 40)
    counts = np.array([*rows, odd], dtype=float)
    hosts = [f"h{i:02d}" for i in range(len(counts))]
    return hosts, counts, np.arange(100, 108)


def test_planted_clusters_and_outlier():
    hosts, counts, ids = planted()
    result = hs.analyse(hosts, counts, ids)
    frame = result.hosts
    assert result.fit["k"] >= 3
    groups = [set(frame["cluster"][g * 20 : (g + 1) * 20].to_list()) for g in range(3)]
    assert all(len(g) == 1 for g in groups) and len(set().union(*groups)) == 3
    assert frame["cluster"].to_list().count(frame["cluster"][0]) == 20
    assert frame["hier_cluster"].n_unique() == result.fit["k"] and result.fit["ari"] > 0.9
    # silhouette picks k and the chosen one is the best of those tried
    scores = result.fit["silhouette"]
    assert scores[str(result.fit["k"])] == max(scores.values())
    # the odd host is far from every group and cannot form a cluster of its own
    odd = frame.filter(pl.col("host") == hosts[-1]).row(0, named=True)
    assert odd["outlier"] and result.clusters["n_hosts"].min() >= hs.MIN_PEERS
    # largest cluster first, ids 1..k
    assert result.clusters["cluster"].to_list() == list(range(1, result.fit["k"] + 1))
    assert result.clusters["n_hosts"].to_list() == sorted(result.clusters["n_hosts"], reverse=True)
    assert sorted(frame["leaf"].to_list()) == list(range(len(hosts)))
    assert result.linkage.shape == (len(hosts) - 1, 4)


def test_outlier_inside_a_cluster_is_flagged_with_evidence():
    rng = np.random.default_rng(1)
    base = np.array([60, 30, 5, 3, 2], dtype=float)
    counts = rng.poisson(base * 500, size=(30, 5)).astype(float)
    counts[7] = rng.poisson(np.array([20, 20, 5, 3, 60]) * 500)  # shifted towards template 4
    ids = np.array([10, 11, 12, 13, 14])
    result = hs.analyse([f"h{i}" for i in range(30)], counts, ids)
    row = result.hosts.row(7, named=True)
    assert row["outlier"] and row["z"] > hs.OUTLIER_Z
    assert result.hosts["outlier"].sum() == 1
    dev = result.deviations.filter(pl.col("row") == 7).sort("rank")
    assert dev["template_id"][0] in (14, 10, 11)
    assert dev["rank"].to_list() == [1, 2, 3] and dev["share"].sum() <= 1 + 1e-9
    assert dev["share"].to_list() == sorted(dev["share"], reverse=True)
    cluster = result.clusters.filter(pl.col("cluster") == row["cluster"]).row(0, named=True)
    assert (row["distance"] - cluster["median_distance"]) / cluster["scale"] == pytest.approx(
        row["z"]
    )
    assert cluster["outliers"] == 1


def test_small_clusters_have_no_scores():
    labels = np.array([1, 1, 2, 3, 3, 3])
    distance = np.array([0.1, 0.5, 0.0, 0.1, 0.1, 0.9])
    z, clusters = hs.robust_scores(distance, labels)
    assert (z[:3] == 0).all() and z[5] > hs.OUTLIER_Z  # a pair and a singleton get no z
    assert clusters["n_hosts"].to_list() == [2, 1, 3]
    assert clusters["scale"].min() > 0  # never divides by zero


def test_cluster_hosts_degenerate_inputs():
    labels, scores = hs.cluster_hosts(np.zeros((2, 4)))
    assert labels.tolist() == [0, 0] and scores == {}
    labels, scores = hs.cluster_hosts(np.ones((8, 3)))  # identical profiles
    assert set(labels.tolist()) == {0} and scores == {}
    labels, scores = hs.cluster_hosts(np.eye(5))  # no k leaves every cluster MIN_PEERS hosts
    assert set(labels.tolist()) == {0} and scores == {}
    blobs = np.repeat(np.eye(3) * 5, 4, axis=0) + np.random.default_rng(0).normal(0, 0.1, (12, 3))
    labels, scores = hs.cluster_hosts(blobs)
    assert max(scores, key=lambda k: scores[k]) == 3 and np.bincount(labels).tolist() == [4, 4, 4]
    assert min(np.unique(labels, return_counts=True)[1]) >= hs.MIN_PEERS
    assert hs.by_size(np.array([5, 7, 7, 7, 5, 9])).tolist() == [2, 1, 1, 1, 2, 3]
    assert hs.by_size(np.zeros(0)).tolist() == []


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_analyse_few_hosts(n):
    rng = np.random.default_rng(n)
    counts = rng.poisson(5, size=(n, 4)).astype(float) + 1
    result = hs.analyse([f"h{i}" for i in range(n)], counts, np.arange(4))
    assert result.hosts.height == n and result.fit["n_hosts"] == n
    assert result.hosts["outlier"].sum() == 0
    if n < 2 * hs.MIN_PEERS:
        assert result.fit["k"] == (1 if n else 0) and result.fit["silhouette"] == {}
    assert result.deviations.height <= n * hs.TOP_DEVIATIONS
    assert (result.hosts["cluster"] >= 1).all() if n else True


def test_root_mix_and_centroid_distance_agree_with_the_definition():
    rng = np.random.default_rng(3)
    counts = rng.poisson(0.7, size=(40, 12)).astype(float)
    counts[5] = 0  # an empty window
    centroids = root_centroids = hs.root_mix(rng.poisson(3, size=(3, 12)).astype(float))
    clusters = rng.integers(0, 3, size=40)
    expected = np.linalg.norm(hs.root_mix(counts) - centroids[clusters], axis=1)
    assert hs.root_mix(counts)[5].sum() == 0
    for form in (counts, sparse.csr_matrix(counts), sparse.coo_matrix(counts)):
        got = hs.centroid_distance(form, clusters, root_centroids)
        assert np.allclose(got, expected, atol=1e-9)
    assert hs.centroid_distance(sparse.csr_matrix((0, 12)), clusters[:0], centroids).shape == (0,)


def test_profile_counts_aligns_templates():
    counts = pl.DataFrame(
        {"host": ["b", "a", "a", "b"], "template_id": [7, 3, 7, 7], "n": [2, 5, 1, 4], "bin": 0}
    )
    hosts, matrix = hs.profile_counts(counts, np.array([3, 5, 7]))
    assert hosts == ["a", "b"] and matrix.tolist() == [[5, 0, 1], [0, 0, 6]]
    none, empty = hs.profile_counts(counts.clear(), np.array([3, 5]))
    assert none == [] and empty.shape == (0, 2)


def test_stage_files_on_synthetic_data(stage, synthetic):
    entry = stage.manifest().get("hosts")
    assert entry is not None and entry.rows["hosts"] == 6
    profile = hs.load_profile(stage)
    assert profile.hosts.height == 6 and profile.linkage.shape == (5, 4)
    assert profile.fit["k"] >= 1 and profile.hosts["cluster"].min() == 1
    truth = synthetic.truth
    assert {truth.spike_host, truth.silent_host} <= set(profile.hosts["host"])
    # lines per host as in the events, and the service x host matrix adds up to all of them
    assert profile.service_host["n"].sum() == synthetic.truth.n_rows
    assert profile.hosts["events"].sum() <= profile.service_host["n"].sum()  # rows without template
    pair = profile.service_host.filter(
        (pl.col("host") == truth.spike_host) & (pl.col("service") == "neutron-openvswitch-agent")
    )
    assert pair["n"][0] > 2_000  # the injected spike
    templates = stage.read_parquet("templates", "templates.parquet")
    assert profile.fit["template_ids"] == sorted(templates["template_id"].to_list())
    assert set(profile.deviations["template_id"]) <= set(profile.fit["template_ids"])
    assert profile.hosts["leaf"].n_unique() == 6


def test_dendrogram_order_matches_the_leaf_column(stage):
    profile = hs.load_profile(stage)
    fig, height = hs.dendrogram_figure(profile, "full", "dark")
    names = list(fig.layout.yaxis.ticktext)
    ordered = profile.hosts.sort("leaf")["host"].to_list()
    assert names == [figures_short(h) for h in ordered]
    assert height >= 320


def figures_short(text):
    from logdash.app import figures

    return figures.shorten_middle(text, 30)


def test_stored_format_serves_the_contextual_detector(stage):
    """Assignments, centroids and distances can be joined to template-count windows."""
    assignments = hs.load_assignments(stage)
    assert assignments.columns == ["host", "cluster", "distance", "z", "outlier", "events"]
    clusters = hs.load_clusters(stage)
    assert clusters.columns == [
        "cluster", "n_hosts", "median_distance", "scale", "max_distance", "outliers"
    ]  # fmt: skip
    ids, matrix = hs.load_centroids(stage)
    assert ids.tolist() == clusters.sort("cluster")["cluster"].to_list()
    profile = hs.load_profile(stage)
    template_ids = np.array(profile.fit["template_ids"])
    assert matrix.shape == (len(ids), len(template_ids))
    share_ids, share = hs.load_centroids(stage, template_ids[::-1], "share")
    assert np.allclose(share.sum(axis=1), 1)
    assert np.allclose(share[:, ::-1], hs.load_centroids(stage, column="share")[1])
    # a host's profile is at its stored distance from its centroid
    names, counts = hs.profile_counts(
        stage.read_parquet("templates", "template_counts_host.parquet"), template_ids
    )
    frame = assignments.sort("host")
    assert names == frame["host"].to_list()
    rows = np.searchsorted(ids, frame["cluster"].to_numpy())
    got = hs.centroid_distance(sparse.csr_matrix(counts), rows, matrix)
    assert np.allclose(got, frame["distance"].to_numpy())
    # unknown templates are ignored, not an error
    _, partial = hs.load_centroids(stage, np.array([-5, int(template_ids[0])]))
    assert partial.shape == (len(ids), 2)


def test_figures_and_tables(stage):
    profile = hs.load_profile(stage)
    texts = ft.template_texts(stage)
    sil = hs.silhouette_figure(profile, "light")
    assert sil.data or sil.layout.annotations
    proj = hs.projection_figure(profile, Filters(), None)
    assert sum(len(t.x) for t in proj.data if t.mode == "markers") == 6
    only = hs.projection_figure(profile, Filters(hosts=(profile.hosts["host"][0],)), None)
    assert sum(len(t.x) for t in only.data if t.mode == "markers") == 1
    assert hs.projection_figure(
        hs.Profile(
            profile.hosts.clear(), profile.clusters, profile.centroids, profile.deviations,
            profile.linkage, profile.service_host, profile.fit,
        ),
        Filters(),
        None,
    ).layout.annotations  # fmt: skip
    hosts, services, counts = hs.matrix_data(profile, Filters())
    assert hosts == profile.hosts.sort("leaf")["host"].to_list() and counts.sum() > 0
    assert services[0] == max(services, key=lambda s: counts[:, services.index(s)].sum())
    for mode in hs.MODES:
        fig, height = hs.matrix_figure(profile, Filters(), mode, "dark")
        assert fig.data[0].z is not None and height > 320
    sub, _, sub_counts = hs.matrix_data(
        profile, Filters(hosts=(hosts[0],), services=(services[0],))
    )
    assert sub == [hosts[0]] and sub_counts.shape == (1, 1)
    fig, _ = hs.matrix_figure(profile, Filters(services=("nope",)), "count", None)
    assert fig.layout.annotations and not fig.data
    table, links = hs.outlier_table(profile, Filters(hosts=(hosts[0],)), texts)
    assert table.height == 1 and links == [f"/explorer?host={hosts[0]}"]
    assert table["deviating templates"][0].startswith("T")
    everything, _ = hs.outlier_table(profile, Filters(), texts)
    assert everything.height == int(profile.hosts["outlier"].sum())
    clusters = hs.cluster_table(profile, texts)
    assert clusters.height == profile.k and clusters["hosts"].sum() == 6
    assert "%" in clusters["main templates"][0]
    for view in hs.VIEWS:
        assert hs.dendrogram_figure(profile, view, None)[0].data


def test_collapsed_tree_has_one_leaf_per_cluster():
    hosts, counts, ids = planted(per_group=15)
    result = hs.analyse(hosts, counts, ids)
    profile = hs.Profile(
        result.hosts, result.clusters, result.centroids, result.deviations, result.linkage,
        pl.DataFrame({"host": hosts, "service": "s", "n": 1}), result.fit,
    )  # fmt: skip
    full, full_px = hs.dendrogram_figure(profile, "full", None)
    collapsed, collapsed_px = hs.dendrogram_figure(profile, "collapsed", None)
    assert len(full.layout.yaxis.ticktext) == len(hosts)
    cuts = int(result.hosts["hier_cluster"].max())
    assert len(collapsed.layout.yaxis.ticktext) == cuts and collapsed_px < full_px
    assert sum(len(t.x) for t in collapsed.data[1:]) == cuts
    assert hs.cut_height(result.linkage, cuts) is not None
    assert hs.cut_height(result.linkage, 1) is None
    assert hs.members(result.linkage, len(hosts) + len(result.linkage) - 1, len(hosts)) == list(
        range(len(hosts))
    )
    tiny = hs.analyse(["a"], np.ones((1, 3)), np.arange(3))
    single = hs.Profile(*(tiny.hosts, tiny.clusters, tiny.centroids, tiny.deviations,
                          tiny.linkage, profile.service_host, tiny.fit))  # fmt: skip
    assert hs.dendrogram_figure(single, "full", None)[0].layout.annotations


def test_draw_and_degrade(stage, tmp_path):
    out = hs.draw_hosts(stage, Filters(), "full", "count", "light")
    assert len(out) == 10 and out[3].data and out[5].data and out[4]["height"] >= 320
    assert "dendrogram order" in out[5].layout.annotations[0].text
    filtered = hs.draw_hosts(
        stage, Filters(hosts=("x",), services=("y",)), "collapsed", "share", None
    )
    assert "host filter" in filtered[9] and filtered[5].layout.annotations
    assert hs.draw_hosts(stage, Filters(), "full", "count", None)[9].startswith("Profiles")
    missing = hs.draw_hosts(CacheStore(tmp_path), Filters(), "full", "count", None)
    assert "not built" in str(missing[0]) and len(missing) == 10 and missing[9] == ""
    (tmp_path / "hosts").mkdir()
    empty = hs.analyse([], np.zeros((0, 3)), np.arange(3))
    hs.write_result(tmp_path / "hosts", empty)
    schema = {"host": pl.String, "service": pl.String, "n": pl.Int64}
    pl.DataFrame(schema=schema).write_parquet(tmp_path / "hosts" / "service_host.parquet")
    assert "No hosts" in str(
        hs.draw_hosts(CacheStore(tmp_path), Filters(), "full", "count", None)[0]
    )


def test_page_reads_only(stage, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("clustered at request time")

    monkeypatch.setattr(hs, "analyse", refuse)
    monkeypatch.setattr(hs, "cluster_hosts", refuse)
    hs.clear_caches()
    assert hs.draw_hosts(stage, Filters(), "full", "count", None)[3].data


def test_profile_is_memoised_and_follows_files(stage):
    hs.clear_caches()
    first = hs.load_profile(stage)
    assert hs.load_profile(stage) is first
    path = stage.path("hosts", "fit.json")
    stat = path.stat()
    import os

    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert hs.load_profile(stage) is not first


@pytest.mark.parametrize(
    ("raw", "expected"), [("collapsed", "collapsed"), ("x", "full"), (None, "full")]
)
def test_view_value(raw, expected):
    assert hs.view_value(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [("share", "share"), ("x", "count"), (None, "count")])
def test_mode_value(raw, expected):
    assert hs.mode_value(raw) == expected


def test_layout_and_callback_over_http(stage, built):
    from logdash.app.main import create_app

    assert "hosts-tree" in str(Hosts().layout(stage))
    app = create_app(stage, build_with(built, Templates(), Hosts()))
    client = app.server.test_client()
    assert client.get("/hosts").status_code == 200
    outputs = [
        ("hosts-stats", "children"), ("hosts-silhouette", "figure"),
        ("hosts-projection", "figure"), ("hosts-tree", "figure"), ("hosts-tree", "style"),
        ("hosts-matrix", "figure"), ("hosts-matrix", "style"), ("hosts-table", "children"),
        ("hosts-clusters", "children"), ("hosts-hint", "children"),
    ]  # fmt: skip
    body = {
        "output": ".." + "...".join(f"{i}.{p}" for i, p in outputs) + "..",
        "outputs": [{"id": i, "property": p} for i, p in outputs],
        "inputs": [
            {"id": "hosts-view", "property": "value", "value": "collapsed"},
            {"id": "hosts-mode", "property": "value", "value": "share"},
            {"id": "filters", "property": "data", "value": {}},
            {"id": "theme", "property": "data", "value": "dark"},
        ],
        "changedPropIds": ["hosts-view.value"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200, resp.get_data()
    assert resp.get_json()["response"]["hosts-tree"]["figure"]["data"]
    body["inputs"][0]["value"] = "bogus"
    body["inputs"][1]["value"] = None
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
