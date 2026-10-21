import json
from datetime import datetime, timedelta
from statistics import NormalDist

import numpy as np
import polars as pl
import pytest
from scipy import optimize, sparse
from sklearn.decomposition import PCA

from logdash.analyses import features as ft
from logdash.analyses import pca as pc
from logdash.analyses.features import Features
from logdash.analyses.pca import Pca
from logdash.analyses.templates import Templates
from logdash.app.filters import Filters
from logdash.cache.store import CacheStore
from tests.conftest import build_with
from tests.synthetic import utc


@pytest.fixture(scope="module")
def stage(built):
    build_with(built, Templates(), Features(), Pca())
    return built.store


def naive(value: datetime) -> datetime:
    return utc(value).replace(tzinfo=None)


def window_of(value: datetime, window_s: int = 60) -> datetime:
    return ft.floor_window(naive(value), window_s)


def test_files_per_grid(stage):
    entry = stage.manifest().get("pca")
    assert entry is not None and entry.rows["service_60_log1p_flagged"] > 0
    for group in ft.GROUPS:
        for window_s in ft.WINDOWS:
            index = stage.read_parquet("features", ft.grid_name("index", group, window_s))
            for scaling in ft.SCALINGS:
                scores = stage.read_parquet("pca", pc.fit_name("scores", group, window_s, scaling))
                assert scores["row"].to_list() == index["row"].to_list()
                assert scores["bin"].to_list() == index["bin"].to_list()
                assert (scores["spe"] >= 0).all() and scores["limit"].n_unique() == 1
                info = stage.read_json("pca", pc.fit_name("fit", group, window_s, scaling))
                assert info["n_windows"] == index.height and 1 <= info["k"] <= pc.MAX_K
                assert info["limit"] == scores["limit"][0] > 0
                assert sum(info["explained"]) <= 1 + 1e-9
                res = stage.read_parquet("pca", pc.fit_name("residuals", group, window_s, scaling))
                assert res["rank"].max() <= pc.TOP_RESIDUAL
                assert res.select("row", "rank").is_unique().all()
                assert res["share"].is_between(0, 1 + 1e-9).all()


def test_spe_matches_sklearn_reconstruction(stage):
    fit = pc.load_fit(stage, "service", 300, "log1p")
    scaled = ft.load_scaled(stage, "service", 300, "log1p")
    dense = scaled.toarray()
    model = PCA(n_components=fit.k, svd_solver="full").fit(dense)
    error = ((dense - model.inverse_transform(model.transform(dense))) ** 2).sum(axis=1)
    assert np.allclose(fit.spe, error)
    assert np.allclose(np.abs(fit.scores[:, 0]), np.abs(model.transform(dense)[:, 0]))
    assert np.allclose(fit.explained[: fit.k], model.explained_variance_ratio_)
    # the residual of a window is the sum of its squared per-template residuals
    top = fit.residuals.filter(pl.col("row") == int(np.argmax(fit.spe)))
    assert 0 < top["share"].sum() <= 1 + 1e-9


def test_synthetic_spike_and_novel_template_exceed_the_limit(stage, synthetic):
    truth = synthetic.truth
    spe = pc.load_spe(stage, "service", 60, "log1p")
    limit = spe["limit"][0]
    spike_bin = window_of(truth.spike_start)
    spike = spe.filter(pl.col("bin").is_between(spike_bin, naive(truth.spike_end)))
    assert spike["spe"].max() > limit
    assert spike.sort("spe").tail(1)["entity"][0] == "neutron-openvswitch-agent"
    novel = spe.filter(
        (pl.col("bin") >= window_of(truth.novel_first_seen)) & (pl.col("entity") == "haproxy")
    )
    assert (novel["spe"] > limit).any()
    # most windows stay below the limit
    assert (spe["spe"] > limit).mean() < 0.1


def test_residual_templates_name_the_novel_template(stage, synthetic):
    truth = synthetic.truth
    templates = stage.read_parquet("templates", "templates.parquet")
    novel_id = templates.filter(pl.col("template").str.contains("bgp session"))["template_id"][0]
    spe = pc.load_spe(stage, "service", 60, "log1p")
    flagged = spe.filter(
        (pl.col("bin") >= window_of(truth.novel_first_seen))
        & (pl.col("entity") == "haproxy")
        & (pl.col("spe") > pl.col("limit"))
    )
    residuals = pc.load_residuals(stage, "service", 60, "log1p")
    top = residuals.filter(pl.col("row").is_in(flagged["row"].to_list()) & (pl.col("rank") == 1))
    assert novel_id in top["template_id"].to_list()


def test_spe_limit_has_the_requested_false_alarm_rate():
    rng = np.random.default_rng(0)
    scale = np.array([4.0, 3.0] + [1.0] * 10 + [0.3] * 20)
    data = rng.normal(size=(200_000, len(scale))) * scale
    pca = PCA().fit(data[:50_000])
    k = 2
    limit = pc.spe_limit(pca.explained_variance_[k:])
    kept = pca.components_[:k]
    centered = data[50_000:] - pca.mean_
    spe = ((centered - centered @ kept.T @ kept) ** 2).sum(axis=1)
    assert 0.0005 < (spe > limit).mean() < 0.002
    loose = pc.spe_limit(pca.explained_variance_[k:], 0.05)
    assert loose < limit
    assert (spe > loose).mean() == pytest.approx(0.05, abs=0.005)
    assert pc.spe_limit(np.array([1.0]), 0.5) > 0


def test_spe_limit_degenerate_cases():
    assert pc.spe_limit(np.zeros(3)) == np.inf
    assert pc.spe_limit(np.zeros(0)) == np.inf
    assert np.isfinite(pc.spe_limit(np.array([2.0, 1.0, 0.5, 0.25])))


def test_spe_limit_normal_approximation_when_h0_vanishes():
    def h0(x):  # one large eigenvalue and many small ones
        t = [np.sum(np.array([1.0] + [x] * 50) ** i) for i in (1, 2, 3)]
        return 2 * t[0] * t[2] - 3 * t[1] ** 2

    x = optimize.brentq(h0, 0.01, 0.1, xtol=1e-14)
    values = np.array([1.0] + [x] * 50)
    theta1, theta2 = values.sum(), (values**2).sum()
    c = NormalDist().inv_cdf(1 - pc.ALPHA)
    assert pc.spe_limit(values) == pytest.approx(theta1 + c * np.sqrt(2 * theta2), rel=1e-3)


def test_components_kept():
    assert pc.components_kept(np.zeros(0)) == 0
    assert pc.components_kept(np.array([0.97, 0.03])) == 1
    assert pc.components_kept(np.array([0.5, 0.3, 0.16, 0.04])) == 3
    assert pc.components_kept(np.array([0.5, 0.5])) == 2
    assert pc.components_kept(np.full(100, 0.01)) == pc.MAX_K


def test_fit_model_small_and_degenerate():
    ids = np.array([5, 7, 9])
    empty = pc.fit_model(sparse.csr_matrix((0, 3)), ids)
    assert empty.k == 0 and empty.limit == np.inf and empty.residuals.height == 0
    assert pc.fit_model(sparse.csr_matrix((4, 0)), np.zeros(0, dtype=int)).spe.shape == (4,)
    constant = pc.fit_model(sparse.csr_matrix(np.ones((5, 3))), ids)
    assert constant.k == 1 and np.allclose(constant.spe, 0) and constant.residuals.height == 0
    rng = np.random.default_rng(2)
    data = sparse.csr_matrix(rng.poisson(0.5, size=(40, 2)).astype(float))
    two = pc.fit_model(data, ids[:2])  # fewer templates than stored residuals and scores
    assert (
        two.scores.shape == (40, pc.N_PROJ) and (two.residuals["template_id"].is_in([5, 7])).all()
    )
    assert two.residuals.group_by("row").len()["len"].max() <= 2


def test_empty_residual_space_raises_no_alarms():
    rng = np.random.default_rng(5)
    ids = np.arange(10)
    data = sparse.csr_matrix(rng.poisson(1.0, size=(500, 10)).astype(float))
    # components kept cover every direction (k >= rank): the SPE is rounding noise
    full = pc.fit_model(data, ids)
    assert full.k == 10 and full.limit == np.inf and full.spe.max() < 1e-20
    assert not (full.spe > full.limit).any()
    tiny = pc.fit_model(sparse.csr_matrix(np.outer(rng.poisson(3, 50), np.ones(3)) + 0.0), ids[:3])
    assert tiny.limit == np.inf and not (tiny.spe > tiny.limit).any()
    assert (pc.ratios(tiny.spe, tiny.limit) == 0).all()


def test_residual_blocks_cover_every_window(monkeypatch):
    monkeypatch.setattr(pc, "CHUNK", 7)
    rng = np.random.default_rng(4)
    data = sparse.csr_matrix(rng.poisson(0.3, size=(30, 6)).astype(float))
    ids = np.arange(100, 106)
    model = pc.fit_model(data, ids)
    monkeypatch.setattr(pc, "CHUNK", 10_000)
    whole = pc.fit_model(data, ids)
    assert np.allclose(model.spe, whole.spe)
    assert model.residuals.equals(whole.residuals)
    assert set(model.residuals["row"].to_list()) <= set(range(30))
    assert model.residuals["template_id"].is_in(ids.tolist()).all()


def test_top_residuals_order_and_sign():
    residual = np.array([[0.5, -2.0, 1.0, 0.0]])
    out = pc.top_residuals(residual, residual**2, np.array([5.25]), np.array([10, 11, 12, 13]), 3)
    assert out["row"].to_list() == [3, 3, 3]
    assert out["template_id"].to_list() == [11, 12, 10]
    assert out["residual"].to_list() == [-2.0, 1.0, 0.5]
    assert out["share"].to_list() == pytest.approx([4 / 5.25, 1 / 5.25, 0.25 / 5.25])


def test_load_fit_is_memoised_and_follows_files(stage):
    pc.clear_caches()
    first = pc.load_fit(stage)
    assert pc.load_fit(stage) is first
    path = stage.path("pca", pc.fit_name("scores", "service", 60, "log1p"))
    stat = path.stat()
    import os

    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert pc.load_fit(stage) is not first
    assert first.bins.shape == first.spe.shape and first.scores.shape[1] == pc.N_PROJ
    assert first.loadings.shape[1] == len(first.template_ids)
    spe = pc.load_spe(stage)
    assert spe.columns == ["row", "bin", "entity", "spe", "limit", "ratio"]
    assert pc.load_residuals(stage).columns == ["row", "rank", "template_id", "residual", "share"]


def test_select_windows_and_sample_rows(stage):
    fit = pc.load_fit(stage)
    lo, hi = ft.visible_range(stage, Filters(), 60)
    everything = pc.select_windows(fit, lo, hi)
    assert len(everything) == len(fit.spe)
    one = pc.select_windows(fit, lo, hi, ("haproxy",))
    assert 0 < len(one) < len(everything) and {fit.entities[c] for c in fit.codes[one]} == {
        "haproxy"
    }
    assert len(pc.select_windows(fit, hi, hi)) == 0
    half = pc.select_windows(fit, lo, lo + (hi - lo) / 2)
    assert 0 < len(half) < len(everything)
    spe = fit.spe
    shown = pc.sample_rows(everything, spe, 400)
    assert len(shown) == 400 and (np.diff(shown) > 0).all()
    assert set(np.argsort(-spe)[:100]) <= set(shown.tolist())  # the worst windows are kept
    assert pc.sample_rows(everything, spe, 10**6) is everything


def test_figures(stage, synthetic):
    fit = pc.load_fit(stage)
    lo, hi = ft.visible_range(stage, Filters(), 60)
    rows = pc.select_windows(fit, lo, hi)
    texts = ft.template_texts(stage)
    scree = pc.scree_figure(fit, "dark")
    assert len(scree.data[0].y) == len(fit.explained) and scree.layout.shapes
    spe = pc.spe_figure(fit, rows, None)
    assert sum(len(t.x) for t in spe.data) == len(rows)
    assert spe.layout.yaxis.type == "log"
    assert len(pc.spe_figure(fit, rows[:0], None).layout.annotations) == 1
    for dims in (2, 3):
        for color in pc.COLORS:
            fig = pc.projection_figure(fit, rows, color, "service", dims, "light")
            assert fig.data and fig.layout.annotations
        entity = pc.projection_figure(fit, rows, "entity", "service", dims, None)
        assert sum(len(t.x) for t in entity.data) == len(rows)
    assert pc.projection_figure(fit, rows[:0], "time", "service", 2, None).layout.annotations
    time = pc.projection_figure(fit, rows, "time", "service", 2, None)
    assert time.data[0].marker.colorbar.title.text == "hours since start"
    load = pc.loadings_figure(fit, 2, texts, "light")
    assert len(load.data[0].x) == min(pc.LOADINGS_SHOWN, len(fit.template_ids))
    assert abs(load.data[0].x[0]) >= abs(load.data[0].x[-1])
    assert pc.loadings_figure(fit, 99, texts, None).layout.annotations
    assert pc.scree_figure(
        pc.Fit(
            *(fit.bins[:0], fit.codes[:0], [], fit.scores[:0], fit.spe[:0], 0.0, 0),
            np.zeros(0),
            np.zeros((0, 0)),
            fit.template_ids[:0],
            fit.residuals.clear(),
            60,
        ),
        None,
    ).layout.annotations


def test_many_entities_fold_into_other(stage):
    fit = pc.load_fit(stage, "host", 60, "log1p")
    rows = np.arange(len(fit.spe))
    groups = pc.entity_traces(fit, rows)
    assert len(groups) <= pc.MAX_CATEGORIES + 1
    assert sum(len(r) for _, r in groups) == len(rows)
    if len(fit.entities) > pc.MAX_CATEGORIES:
        assert groups[-1][0].startswith("other")


def test_flagged_table(stage, synthetic):
    fit = pc.load_fit(stage)
    texts = ft.template_texts(stage)
    rows = np.arange(len(fit.spe))
    table, hrefs = pc.flagged_table(fit, rows, texts, "service")
    assert 0 < table.height <= pc.TABLE_ROWS and len(hrefs) == table.height
    assert (table["SPE"] > 0).all() and (table["x limit"] > 1).all()
    assert table["SPE"].to_list() == sorted(table["SPE"].to_list(), reverse=True)
    assert all(h.startswith("/explorer?start=") and "service=" in h for h in hrefs)
    assert table["residual templates"][0].startswith("T") and table["top template"][0]
    by_host, host_links = pc.flagged_table(pc.load_fit(stage, "host"), rows[:0], texts, "host")
    assert by_host.is_empty() and host_links == []
    host_fit = pc.load_fit(stage, "host")
    _, links = pc.flagged_table(host_fit, np.arange(len(host_fit.spe)), texts, "host")
    assert links and all("host=" in h for h in links)


def test_host_fit_absorbs_the_spike(stage, synthetic):
    # recorded expectation (PLAN.md): a retained component captures the per-host spike
    truth = synthetic.truth
    spe = pc.load_spe(stage, "host", 60, "log1p")
    spike = spe.filter(
        pl.col("bin").is_between(window_of(truth.spike_start), naive(truth.spike_end))
    )
    assert spike["spe"].max() <= spike["limit"][0]


def test_draw_pca(stage, synthetic):
    out = pc.draw_pca(stage, Filters(), 60, "service", "log1p", "time", 1, "light")
    assert len(out) == 7 and all(f.data for f in out[:5]) and out[6] == ""
    for window_s in ft.WINDOWS:
        for group in ft.GROUPS:
            for scaling in ft.SCALINGS:
                assert pc.draw_pca(stage, Filters(), window_s, group, scaling, "score", 3, None)[
                    2
                ].data
    start = naive(synthetic.truth.spike_start)
    timed = Filters(start=start.isoformat(), end=(start + timedelta(minutes=5)).isoformat())
    narrowed = pc.draw_pca(stage, timed, 60, "service", "log1p", "entity", 1, None)
    assert sum(len(t.x) for t in narrowed[2].data) < sum(len(t.x) for t in out[2].data)
    table = pc.draw_pca(stage, timed, 60, "service", "log1p", "time", 1, None)[5]
    assert "neutron-openvswitch-agent" in str(table) and "/explorer?start=" in str(table)
    one = pc.draw_pca(
        stage, Filters(services=("haproxy",)), 60, "service", "log1p", "time", 1, None
    )
    assert len(one[2].data[0].x) < len(out[2].data[0].x)
    hint = pc.draw_pca(stage, Filters(hosts=("h",)), 60, "service", "log1p", "time", 1, None)[6]
    assert "host filter" in hint
    nothing = pc.draw_pca(stage, Filters(services=("x",)), 60, "service", "log1p", "time", 1, None)
    assert nothing[1].layout.annotations and "no windows above" in str(nothing[5])


def test_page_does_not_fit(stage, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("fitted at request time")

    monkeypatch.setattr(pc, "fit_model", refuse)
    monkeypatch.setattr(PCA, "fit", refuse)
    pc.clear_caches()
    assert pc.draw_pca(stage, Filters(), 300, "host", "tfidf", "time", 2, None)[0].data


def test_degrades_without_stage_or_windows(tmp_path):
    store = CacheStore(tmp_path)
    out = pc.draw_pca(store, Filters(), 60, "service", "log1p", "time", 1, None)
    assert len(out) == 7 and out[0].layout.annotations and "not built" in str(out[5])
    (tmp_path / "pca").mkdir()
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "meta.json").write_text("{}")
    empty = pc.Model(np.zeros(0), 0, 0.0, np.zeros((0, 0)), np.zeros((0, 3)), np.zeros(0),
                     pl.DataFrame(schema=pc.RESIDUAL_SCHEMA))  # fmt: skip
    matrix = ft.Matrix(
        sparse.csr_matrix((0, 0)), np.array([], dtype="datetime64[us]"), np.array([], dtype=int),
        [], np.array([], dtype=int), 60,
    )  # fmt: skip
    pc.write_fit(tmp_path / "pca", "service", 60, "log1p", matrix, empty)
    assert "Too few windows" in str(
        pc.draw_pca(store, Filters(), 60, "service", "log1p", "time", 1, None)[5]
    )


@pytest.mark.parametrize(("raw", "expected"), [("score", "score"), ("x", "time"), (None, "time")])
def test_color_value(raw, expected):
    assert pc.color_value(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [("3", 3), ("9", 1), (None, 1), ("x", 1)])
def test_component_value(raw, expected):
    assert pc.component_value(raw) == expected


def test_layout_and_callback_over_http(stage, built):
    from logdash.app.main import create_app

    assert "pca-scree" in str(Pca().layout(stage))
    app = create_app(stage, build_with(built, Templates(), Features(), Pca()))
    client = app.server.test_client()
    assert client.get("/pca").status_code == 200
    outputs = ["pca-scree", "pca-spe", "pca-2d", "pca-3d", "pca-loadings", "pca-table", "pca-hint"]
    props = ["figure"] * 5 + ["children"] * 2
    body = {
        "output": ".." + "...".join(f"{o}.{p}" for o, p in zip(outputs, props, strict=True)) + "..",
        "outputs": [{"id": o, "property": p} for o, p in zip(outputs, props, strict=True)],
        "inputs": [
            {"id": "pca-window", "property": "value", "value": "300"},
            {"id": "pca-group", "property": "value", "value": "host"},
            {"id": "pca-scale", "property": "value", "value": "tfidf"},
            {"id": "pca-color", "property": "value", "value": "entity"},
            {"id": "pca-component", "property": "value", "value": "2"},
            {"id": "filters", "property": "data", "value": {}},
            {"id": "theme", "property": "data", "value": "dark"},
        ],
        "changedPropIds": ["pca-window.value"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200, resp.get_data()
    assert resp.get_json()["response"]["pca-2d"]["figure"]["data"]
    body["inputs"][1]["value"] = "bogus"
    body["inputs"][2]["value"] = None
    body["inputs"][3]["value"] = "bogus"
    assert (
        client.post(
            "/_dash-update-component", data=json.dumps(body), content_type="application/json"
        ).status_code
        == 200
    )
