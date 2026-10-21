import json
from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest
from scipy import sparse

from logdash.analyses import features as ft
from logdash.analyses.features import Features
from logdash.analyses.templates import Templates
from logdash.app.filters import Filters
from tests.conftest import build_with


@pytest.fixture(scope="module")
def feat(built):
    build_with(built, Templates(), Features())
    return built.store


@pytest.fixture(scope="module")
def span(feat):
    meta = feat.read_json("features", "meta.json")
    return datetime.fromisoformat(meta["ts_min"]), datetime.fromisoformat(meta["ts_max"])


def dense(counts):
    return counts.toarray()


def test_built_from_template_counts(feat):
    for group, name in (
        ("service", "template_counts.parquet"),
        ("host", "template_counts_host.parquet"),
    ):
        source = feat.read_parquet("templates", name)
        for window_s in ft.WINDOWS:
            matrix = feat.read_parquet("features", ft.grid_name("matrix", group, window_s))
            index = feat.read_parquet("features", ft.grid_name("index", group, window_s))
            assert matrix["n"].sum() == source["n"].sum()
            assert matrix["row"].max() == index.height - 1 and index["row"].is_unique().all()
            assert matrix.select("row", "template_id").is_unique().all()
    coarse = feat.read_parquet("features", ft.grid_name("index", "service", 900))
    assert (
        coarse.height < feat.read_parquet("features", ft.grid_name("index", "service", 60)).height
    )
    assert (coarse["bin"].dt.minute() % 15 == 0).all()
    assert feat.read_json("features", "meta.json")["template_ids"] == sorted(
        feat.read_parquet("templates", "templates.parquet")["template_id"].to_list()
    )
    entry = feat.manifest().get("features")
    assert entry is not None and entry.rows["service_60_windows"] > 0


def test_load_matrix_matches_long_format(feat):
    matrix = ft.load_matrix(feat)
    assert ft.load_matrix(feat) is matrix
    long = feat.read_parquet("features", ft.grid_name("matrix", "service", 60))
    assert matrix.counts.shape == (len(matrix.bins), len(matrix.template_ids))
    assert matrix.counts.sum() == long["n"].sum()
    host = ft.load_matrix(feat, "host", 300)
    assert host.window_s == 300 and len(host.entities) > len(matrix.entities) - 1
    row = int(long["row"][0])
    col = int(np.searchsorted(matrix.template_ids, long["template_id"][0]))
    assert matrix.counts[row, col] == long["n"][0]


def test_select_rows_range_and_entities(feat, span):
    matrix = ft.load_matrix(feat)
    lo, hi = ft.visible_range(feat, Filters(), 60)
    assert lo <= span[0] and hi > span[1] and lo.second == 0
    whole = ft.select_rows(matrix, lo, hi)
    assert whole.counts.sum() == matrix.counts.sum()
    assert whole.n_windows == int((hi - lo).total_seconds() // 60)
    half = ft.select_rows(matrix, lo, lo + (hi - lo) / 2)
    assert 0 < half.counts.sum() < whole.counts.sum()
    one = ft.select_rows(matrix, lo, hi, ("haproxy",))
    assert one.entities == ["haproxy"] and set(one.codes) == {0}
    assert 0 < one.counts.sum() < whole.counts.sum()
    none = ft.select_rows(matrix, lo, hi, ("nope",))
    assert none.entities == [] and none.counts.shape[0] == 0 and none.n_rows == 1


def test_scaling():
    counts = sparse.csr_matrix(np.array([[3.0, 0, 1], [0, 0, 1], [0, 0, 0]]))
    log = ft.scale_counts(counts, "log1p", 4)
    assert np.allclose(dense(log), np.log1p(dense(counts)))
    tfidf = ft.scale_counts(counts, "tfidf", 4)
    norms = np.linalg.norm(dense(tfidf), axis=1)
    assert np.allclose(norms, [1, 1, 0])
    assert dense(tfidf)[1, 2] == pytest.approx(1.0)
    # the rarer column outweighs the common one in the first row
    assert dense(tfidf)[0, 0] > dense(tfidf)[0, 2]
    assert counts[0, 0] == 3  # input untouched


def test_variance_and_correlation_match_numpy():
    rng = np.random.default_rng(1)
    full = rng.poisson(0.4, size=(50, 4)).astype(float)
    full[:, 3] = 0
    full[:, 1] = full[:, 0] * 2  # perfectly correlated
    pad = 20  # all-zero rows that are not stored
    csr = sparse.csr_matrix(full)
    everything = np.vstack([full, np.zeros((pad, 4))])
    assert np.allclose(ft.column_variance(csr, 70), everything.var(axis=0))
    corr = ft.correlation(csr, np.array([0, 1, 2, 3]), 70)
    with np.errstate(invalid="ignore"):
        expected = np.nan_to_num(np.corrcoef(everything.T))
    expected[3, 3] = 0.0
    assert np.allclose(corr, expected)
    assert corr[0, 1] == pytest.approx(1.0) and corr[3, 3] == 0.0


def test_mutual_information():
    even = np.full((1, 4), 5.0)
    assert ft.presence_mutual_information(even, 10)[0] == pytest.approx(0.0)
    exclusive = np.array([[10.0, 0.0]])
    assert ft.presence_mutual_information(exclusive, 10)[0] == pytest.approx(1.0)
    everywhere = np.full((1, 3), 10.0)
    assert ft.presence_mutual_information(everywhere, 10)[0] == pytest.approx(0.0)
    never = np.zeros((1, 3))
    assert ft.presence_mutual_information(never, 10)[0] == 0.0
    mixed = np.array([[10.0, 5.0]])
    assert 0 < ft.presence_mutual_information(mixed, 10)[0] < 1


def test_feature_stats_on_synthetic(feat, span):
    lo, hi = ft.visible_range(feat, Filters(), 60)
    sel = ft.select_rows(ft.load_matrix(feat), lo, hi)
    stats = ft.feature_stats(sel, "log1p")
    templates = feat.read_parquet("templates", "templates.parquet")
    assert stats.volume.sum() == templates["count"].sum()
    assert (stats.windows <= stats.by_entity.shape[1] * sel.n_windows).all()
    assert stats.by_entity.sum() == stats.volume.sum()
    assert ((stats.mutual_information >= 0) & (stats.mutual_information <= 1.0 + 1e-9)).all()
    top = ft.top_columns(stats, 5)
    assert len(top) == 5 and (np.diff(stats.volume[top]) <= 0).all()
    assert len(ft.top_columns(stats, 10_000)) == int((stats.volume > 0).sum())
    texts = ft.template_texts(feat)
    table = ft.stats_table(stats, texts)
    assert table.height == int((stats.volume > 0).sum())
    assert table["events"].sum() == templates["count"].sum()
    assert table["top_share"].is_between(0, 1).all()
    # the iptables warning only comes from the l3 agent
    row = table.filter(pl.col("template").str.contains("iptables")).row(0, named=True)
    assert row["top_entity"] == "neutron-l3-agent" and row["top_share"] == pytest.approx(1.0)
    assert ft.stats_table(
        ft.feature_stats(ft.select_rows(ft.load_matrix(feat), hi, hi, ("x",)), "tfidf"), texts
    ).is_empty()


def test_figures(feat, span):
    lo, hi = ft.visible_range(feat, Filters(), 60)
    texts = ft.template_texts(feat)
    for scaling in ft.SCALINGS:
        sel = ft.select_rows(ft.load_matrix(feat), lo, hi)
        stats = ft.feature_stats(sel, scaling)
        cols = ft.top_columns(stats, 8)
        corr = ft.correlation(stats.scaled, cols, sel.n_rows)
        fig = ft.correlation_figure(corr, sel.template_ids[cols], texts, scaling, "dark")
        assert np.asarray(fig.data[0].z).shape == (8, 8)
        assoc = ft.association_figure(stats, cols, texts, "service", None)
        assert np.allclose(np.asarray(assoc.data[0].z).sum(axis=1), 1.0)
        table = ft.stats_table(stats, texts)
        bars = ft.ranking_figure(table, "variance", "v", "light")
        assert list(bars.data[0].x) == sorted(bars.data[0].x, reverse=True)
    assert ft.correlation_figure(
        np.zeros((1, 1)), np.array([1]), texts, "log1p", None
    ).layout.annotations
    empty = ft.feature_stats(ft.select_rows(ft.load_matrix(feat), hi, hi, ("x",)), "log1p")
    assert ft.association_figure(
        empty, np.array([], dtype=int), texts, "host", None
    ).layout.annotations
    assert ft.ranking_figure(ft.stats_table(empty, texts), "variance", "v", None).layout.annotations
    assert "none" in str(ft.stats_view(ft.stats_table(empty, texts)))
    assert "/explorer?template=" in str(ft.stats_view(table))


def test_draw_features_filters(feat, span):
    lo, hi = span
    base = ft.draw_features(feat, Filters(), 60, "service", "log1p", 10, "light")
    assert len(base) == 5 and base[0].data and base[1].data and base[2].data and base[3].data
    for window_s in ft.WINDOWS:
        for group in ft.GROUPS:
            out = ft.draw_features(feat, Filters(), window_s, group, "tfidf", 15, None)
            assert out[0].data
    narrowed = ft.draw_features(
        feat, Filters(services=("haproxy",)), 60, "service", "log1p", 10, None
    )
    assert np.asarray(narrowed[0].data[0].z).shape[0] < np.asarray(base[0].data[0].z).shape[0]
    by_host = ft.draw_features(
        feat, Filters(hosts=("eselda14u05s02",)), 60, "host", "log1p", 10, None
    )
    assert by_host[1].data
    # a service filter does not apply to the per-host grid
    unaffected = ft.draw_features(
        feat, Filters(services=("haproxy",)), 60, "host", "log1p", 10, None
    )
    assert len(unaffected[1].data[0].x) == len(
        ft.draw_features(feat, Filters(), 60, "host", "log1p", 10, None)[1].data[0].x
    )
    timed = Filters(
        start=(lo + timedelta(hours=1)).isoformat(), end=(lo + timedelta(hours=2)).isoformat()
    )
    assert ft.draw_features(feat, timed, 60, "service", "log1p", 10, None)[0].data
    outside = Filters(start="2000-01-01 00:00:00", end="2000-01-01 01:00:00")
    assert ft.draw_features(feat, outside, 60, "service", "log1p", 10, None)[0].layout.annotations
    assert ft.visible_range(feat, outside, 60)[0] < ft.visible_range(feat, outside, 60)[1]


def test_layout_and_callback_over_http(feat, built):
    from logdash.app.main import create_app

    assert "features-corr" in str(Features().layout(feat))
    app = create_app(feat, build_with(built, Templates(), Features()))
    client = app.server.test_client()
    assert client.get("/features").status_code == 200
    outputs = [
        "features-corr",
        "features-assoc",
        "features-variance",
        "features-mi",
        "features-table",
    ]
    props = ["figure"] * 4 + ["children"]
    body = {
        "output": ".." + "...".join(f"{o}.{p}" for o, p in zip(outputs, props, strict=True)) + "..",
        "outputs": [{"id": o, "property": p} for o, p in zip(outputs, props, strict=True)],
        "inputs": [
            {"id": "features-window", "property": "value", "value": "300"},
            {"id": "features-group", "property": "value", "value": "host"},
            {"id": "features-scale", "property": "value", "value": "tfidf"},
            {"id": "features-top", "property": "value", "value": "15"},
            {"id": "filters", "property": "data", "value": {}},
            {"id": "theme", "property": "data", "value": "dark"},
        ],
        "changedPropIds": ["features-window.value"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200, resp.get_data()
    assert resp.get_json()["response"]["features-corr"]["figure"]["data"]
    body["inputs"][1]["value"] = "bogus"
    body["inputs"][2]["value"] = None
    assert (
        client.post(
            "/_dash-update-component", data=json.dumps(body), content_type="application/json"
        ).status_code
        == 200
    )
