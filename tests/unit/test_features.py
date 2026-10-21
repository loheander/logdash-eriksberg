import json
import math
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


def matrix_windows(store):
    """Non-empty (window, entity) rows of the service grid: the bound for ``windows``."""
    return store.read_parquet("features", ft.grid_name("index", "service", 60)).height


def hand_matrix(counts, window_s=60):
    """Matrix of one entity per row group: ``counts[w][e]`` is a list over templates."""
    rows, cells, bins, ents = 0, [], [], []
    base = datetime(2024, 1, 1)
    for w, per_entity in enumerate(counts):
        for e, per_template in per_entity.items():
            if not any(per_template):
                continue
            bins.append(base + timedelta(seconds=w * window_s))
            ents.append(e)
            cells += [(rows, t, n) for t, n in enumerate(per_template) if n]
            rows += 1
    index = pl.DataFrame({"row": range(rows), "bin": bins, "entity": ents}).with_columns(
        pl.col("bin").cast(pl.Datetime("us"))
    )
    n_templates = len(next(iter(counts[0].values())))
    long = pl.DataFrame(cells, schema=["row", "template_id", "n"], orient="row")
    return ft.build_matrix(index, long, list(range(n_templates)), window_s), base


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
    hosts = feat.read_parquet("templates", "template_counts_host.parquet")["host"].n_unique()
    assert host.window_s == 300 and len(host.entities) == hosts
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
    source = feat.read_parquet("templates", "template_counts.parquet")
    start = lo + timedelta(hours=1)
    end = lo + timedelta(hours=2)
    inside = source.filter((pl.col("bin") >= start) & (pl.col("bin") < end))["n"].sum()
    assert 0 < inside < whole.counts.sum()
    assert ft.select_rows(matrix, start, end).counts.sum() == inside
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


def test_planted_correlation():
    # templates 0 and 1 always appear together, template 2 exactly when they do not
    windows = [{"a": [3, 3, 0]} if w % 2 == 0 else {"a": [0, 0, 2]} for w in range(20)]
    matrix, base = hand_matrix(windows)
    sel = ft.select_rows(matrix, base, base + timedelta(minutes=20))
    corr = ft.correlation(ft.scale_counts(sel.counts, "log1p", sel.n_rows), np.arange(3), 20)
    assert corr[0, 1] == pytest.approx(1.0) and corr[0, 2] == pytest.approx(-1.0)


def test_planted_association_and_mutual_information():
    # template 0 only from entity a, template 1 from both equally, template 2 only from b
    windows = [{"a": [1, 1, 0], "b": [0, 1, 1]} for _ in range(10)]
    matrix, base = hand_matrix(windows)
    sel = ft.select_rows(matrix, base, base + timedelta(minutes=10))
    stats = ft.feature_stats(sel, "log1p")
    assert stats.mutual_information[0] == pytest.approx(stats.mutual_information[2])
    assert stats.mutual_information[0] > 0.4 and stats.mutual_information[1] == pytest.approx(0)
    assert stats.by_entity.tolist() == [[10, 0], [10, 10], [0, 10]]
    assert ft.stats_table(stats, {}).sort("template_id")["top_entity"].to_list()[0] == "a"


def reference_mutual_information(present, n_windows):
    """Plain-loop I(occurs; entity) in bits with equally weighted entities."""
    n_entities = len(present)
    p_x = sum(c / n_windows for c in present) / n_entities
    total = 0.0
    for c in present:
        for p_given, p_marginal in ((c / n_windows, p_x), (1 - c / n_windows, 1 - p_x)):
            if p_given > 0:
                total += (1 / n_entities) * p_given * math.log2(p_given / p_marginal)
    return total


def test_mutual_information_matches_reference():
    rng = np.random.default_rng(3)
    present = rng.integers(0, 31, size=(12, 5)).astype(float)
    got = ft.presence_mutual_information(present, 30)
    expected = [reference_mutual_information(row, 30) for row in present]
    assert np.allclose(got, expected)


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
    assert (stats.windows <= matrix_windows(feat)).all()
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


def test_association_rows_sum_to_one_with_many_entities():
    many = ft.MAX_ENTITIES + 10
    windows = [{f"e{i:02d}": [i + 1, 1] for i in range(many)} for _ in range(3)]
    matrix, base = hand_matrix(windows)
    sel = ft.select_rows(matrix, base, base + timedelta(minutes=3))
    stats = ft.feature_stats(sel, "log1p")
    fig = ft.association_figure(stats, np.array([0, 1]), {}, "host", None)
    z = np.asarray(fig.data[0].z)
    assert z.shape == (2, ft.MAX_ENTITIES + 1) and fig.data[0].x[-1] == "other (10)"
    assert np.allclose(z.sum(axis=1), 1.0)


def test_entities_without_rows_in_range_are_dropped():
    windows = [{"a": [1], "b": [1]}] * 5 + [{"a": [1]}] * 5
    matrix, base = hand_matrix(windows)
    late = ft.select_rows(matrix, base + timedelta(minutes=5), base + timedelta(minutes=10))
    assert late.entities == ["a"] and set(late.codes) == {0}
    assert ft.select_rows(matrix, base, base + timedelta(minutes=10)).entities == ["a", "b"]


def test_build_matrix_rejects_unknown_template_ids():
    index = pl.DataFrame({"row": [0], "bin": [datetime(2024, 1, 1)], "entity": ["a"]})
    long = pl.DataFrame({"row": [0], "template_id": [7], "n": [1]})
    with pytest.raises(ValueError, match="template ids"):
        ft.build_matrix(index, long, [1, 2, 3], 60)
    with pytest.raises(ValueError, match="template ids"):
        ft.build_matrix(index, long, [1, 2, 8], 60)


@pytest.mark.parametrize(
    ("value", "window_s", "floor", "ceil"),
    [
        (
            datetime(2024, 1, 1, 10, 7, 30),
            300,
            datetime(2024, 1, 1, 10, 5),
            datetime(2024, 1, 1, 10, 10),
        ),
        (
            datetime(2024, 1, 1, 10, 5),
            300,
            datetime(2024, 1, 1, 10, 5),
            datetime(2024, 1, 1, 10, 5),
        ),
        (
            datetime(2024, 1, 1, 10, 14, 59),
            900,
            datetime(2024, 1, 1, 10, 0),
            datetime(2024, 1, 1, 10, 15),
        ),
    ],
)
def test_floor_and_ceil_window(value, window_s, floor, ceil):
    assert ft.floor_window(value, window_s) == floor
    assert ft.ceil_window(value, window_s) == ceil


def test_visible_range_end_is_ceiled_and_clamped(feat, span):
    first, _ = span
    start = first.replace(second=0, microsecond=0)
    mid = start + timedelta(minutes=10, seconds=20)
    lo, hi = ft.visible_range(feat, Filters(start=start.isoformat(), end=mid.isoformat()), 60)
    assert hi - lo == timedelta(minutes=11)  # the window holding the end is included
    lo, hi = ft.visible_range(feat, Filters(start=mid.isoformat(), end=start.isoformat()), 60)
    assert hi - lo == timedelta(minutes=1)  # end before start: one window


def test_top_columns_ties_prefer_lower_id():
    counts = sparse.csr_matrix(np.array([[2.0, 5, 2, 0, 5]]))
    sel = ft.Selection(counts, np.array([0]), ["a"], 1, np.arange(5))
    stats = ft.feature_stats(sel, "log1p")
    assert ft.top_columns(stats, 4).tolist() == [1, 4, 0, 2]
    assert ft.top_columns(stats, 2).tolist() == [1, 4]


def test_ignored_filters_hint():
    assert ft.ignored_filters(Filters(), "service") == ""
    assert "host" in ft.ignored_filters(Filters(hosts=("h",)), "service")
    assert "service" in ft.ignored_filters(Filters(services=("s",)), "host")
    assert "level" in ft.ignored_filters(Filters(levels=("ERROR",)), "host")


def test_load_scaled_matches_page(feat):
    for scaling in ft.SCALINGS:
        scaled = ft.load_scaled(feat, "service", 300, scaling)
        matrix = ft.load_matrix(feat, "service", 300)
        assert scaled.shape == matrix.counts.shape
        assert scaled.nnz == matrix.counts.nnz
    tfidf = ft.load_scaled(feat, "host", 60, "tfidf")
    assert np.allclose(np.linalg.norm(tfidf.toarray(), axis=1), 1.0)


def test_stats_are_memoised_across_theme_and_top(feat, monkeypatch):
    ft.clear_caches()
    calls = []
    real = ft.feature_stats
    monkeypatch.setattr(ft, "feature_stats", lambda *a: calls.append(a) or real(*a))
    ft.draw_features(feat, Filters(), 60, "service", "log1p", 25, "light")
    ft.draw_features(feat, Filters(), 60, "service", "log1p", 15, "dark")
    ft.draw_features(feat, Filters(), 60, "service", "log1p", 25, "dark")
    assert len(calls) == 1
    ft.draw_features(feat, Filters(), 60, "service", "tfidf", 25, "dark")
    assert len(calls) == 2


def test_matrix_cache_follows_file_version(feat):
    ft.clear_caches()
    first = ft.load_matrix(feat)
    assert ft.load_matrix(feat) is first
    path = feat.path("features", ft.grid_name("matrix", "service", 60))
    stat = path.stat()
    import os

    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    second = ft.load_matrix(feat)
    assert second is not first
    assert len([k for k in ft._CACHE if k[1:] == ("service", 60)]) == 1  # stale one replaced


def test_degrades_without_stage(tmp_path):
    from logdash.cache.store import CacheStore

    store = CacheStore(tmp_path)
    out = ft.draw_features(store, Filters(), 60, "service", "log1p", 10, None)
    assert len(out) == 6 and out[0].layout.annotations and "not built" in str(out[4])


def test_degrades_on_empty_and_stale_stage(tmp_path, feat):
    from logdash.cache.store import CacheStore

    source, out = tmp_path / "templates", tmp_path / "features"
    source.mkdir()
    out.mkdir()
    schema = {
        "bin": pl.Datetime("us"),
        "program": pl.String,
        "template_id": pl.Int64,
        "n": pl.Int64,
    }
    pl.DataFrame(schema=schema).write_parquet(source / "template_counts.parquet")
    pl.DataFrame(schema={**schema, "host": pl.String}).drop("program").write_parquet(
        source / "template_counts_host.parquet"
    )
    pl.DataFrame(
        {"template_id": [], "template": []}, schema={"template_id": pl.Int64, "template": pl.String}
    ).write_parquet(source / "templates.parquet")
    (source / "meta.json").write_text(json.dumps({"ts_min": None, "ts_max": None}))
    rows = ft.compute_all(source, out, lambda _: None)
    assert rows["service_60_windows"] == 0 and rows["host_900_nonzero"] == 0
    store = CacheStore(tmp_path)
    assert ft.load_matrix(store).counts.shape == (0, 0)
    drawn = ft.draw_features(store, Filters(), 60, "service", "log1p", 10, None)
    assert "No events" in str(drawn[4])
    # templates rebuilt with more templates than the features stage knows about
    pl.DataFrame({"template_id": [1], "template": ["x"]}).write_parquet(
        source / "templates.parquet"
    )
    (out / "meta.json").write_text(
        json.dumps(
            {"template_ids": [], "ts_min": "2024-01-01T00:00:00", "ts_max": "2024-01-01T01:00:00"}
        )
    )
    assert "older than the templates" in str(
        ft.draw_features(store, Filters(), 60, "service", "log1p", 10, None)[4]
    )


@pytest.mark.parametrize(("raw", "expected"), [("300", 300), ("7", 60), (None, 60), ("x", 60)])
def test_window_value(raw, expected):
    assert ft.window_value(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [("15", 15), ("16", 25), (None, 25), ("x", 25)])
def test_top_value(raw, expected):
    assert ft.top_value(raw) == expected


def test_draw_features_filters(feat, span):
    lo, hi = span
    base = ft.draw_features(feat, Filters(), 60, "service", "log1p", 10, "light")
    assert len(base) == 6 and base[0].data and base[1].data and base[2].data and base[3].data
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
    assert (
        "host filter"
        in ft.draw_features(feat, Filters(hosts=("h",)), 60, "service", "log1p", 10, None)[5]
    )
    timed = Filters(
        start=(lo + timedelta(hours=1)).isoformat(), end=(lo + timedelta(hours=2)).isoformat()
    )
    timed_out = ft.draw_features(feat, timed, 60, "service", "log1p", 10, None)
    assert timed_out[0].data
    full_events = ft.compute_features(feat, Filters(), 60, "service", "log1p").stats.volume.sum()
    timed_events = ft.compute_features(feat, timed, 60, "service", "log1p").stats.volume.sum()
    assert 0 < timed_events < full_events
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
        "features-hint",
    ]
    props = ["figure"] * 4 + ["children"] * 2
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
