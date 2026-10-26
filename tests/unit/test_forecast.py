"""Forecast stage: spike labels, split and targets, features, stored files and the page."""

import dataclasses
import json
from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest

from logdash.analyses import BUILTIN, load_builtin
from logdash.analyses import forecast as fc
from logdash.analyses.base import REGISTRY
from logdash.analyses.forecast import Forecast, Sources
from logdash.analyses.templates import MISSING
from logdash.app import ui
from logdash.app.main import create_app
from logdash.build import stage_statuses
from logdash.detectors import volume
from logdash.detectors.base import DetectContext
from tests.synthetic import utc

T0 = datetime(2023, 5, 21)


def make_sources(
    host_60: pl.DataFrame,
    peers: pl.DataFrame | None = None,
    host_mix: pl.DataFrame | None = None,
    template_counts: pl.DataFrame | None = None,
    template_texts: pl.DataFrame | None = None,
) -> Sources:
    """Hand-made upstream tables of the right schema (empty ones where not given)."""
    empty = pl.Datetime("us")
    return Sources(
        host_60=host_60,
        rate_bins=host_60["bin"],
        template_counts=template_counts
        if template_counts is not None
        else pl.DataFrame(
            schema={"template_id": pl.Int64, "bin": empty, "host": pl.String, "n": pl.Int64}
        ),
        template_texts=template_texts
        if template_texts is not None
        else pl.DataFrame(schema={"template_id": pl.Int64, "template": pl.String}),
        host_mix=host_mix
        if host_mix is not None
        else pl.DataFrame(
            schema={
                "bin": empty,
                "host": pl.String,
                "level": pl.String,
                "program": pl.String,
                "n": pl.Int64,
            }
        ),
        peers=peers
        if peers is not None
        else pl.DataFrame(schema={"host": pl.String, "peer": pl.String, "distance": pl.Float64}),
    )


def host_series(counts: dict[str, np.ndarray]) -> pl.DataFrame:
    rows = [
        (T0 + timedelta(minutes=m), host, int(n))
        for host, x in counts.items()
        for m, n in enumerate(x)
    ]
    return pl.DataFrame(
        rows, schema={"bin": pl.Datetime("us"), "host": pl.String, "n": pl.Int64}, orient="row"
    )


# ---------------------------------------------------------------------- pure functions


def test_split_bounds_and_periods():
    assert fc.split_bounds(1440) == (840, 960)
    assert fc.split_bounds(1439) == (839, 959)
    assert fc.split_bounds(12) == (7, 8)
    assert fc.period_index(12).tolist() == [0] * 7 + [1] + [2] * 4


def test_targets_look_only_ahead():
    spike = np.array([0, 0, 0, 1, 0, 0], dtype=bool)
    assert fc.targets(spike, 1).tolist() == [False, False, True, False, False, False]
    assert fc.targets(spike, 2).tolist() == [False, True, True, False, False, False]
    assert not fc.targets(np.array([0, 1, 0], dtype=bool), 1)[2]  # nothing after the spike
    assert not fc.targets(np.array([0, 1, 1], dtype=bool), 1)[2]  # window passes the end
    both = fc.targets(np.array([spike, ~spike]), 1)
    assert both[0].tolist() == fc.targets(spike, 1).tolist()
    assert both[1].tolist() == fc.targets(~spike, 1).tolist()
    assert not fc.targets(spike, 10).any()  # horizon longer than the span


def test_sample_mask_drops_windows_that_cross_a_period():
    assert np.flatnonzero(~fc.sample_mask(12, 1)).tolist() == [6, 7, 11]
    assert np.flatnonzero(~fc.sample_mask(12, 2)).tolist() == [5, 6, 7, 10, 11]
    assert not fc.sample_mask(5, 9).any()


def test_positive_rates_count_kept_samples():
    spike = np.zeros((2, 24), dtype=bool)
    assert fc.split_bounds(24) == (14, 16)
    spike[0, 3] = True  # target of minute 2 (train)
    spike[1, 15] = True  # target of minute 14 (validation)
    spike[0, 20:22] = True  # targets of minutes 19 and 20 (test)
    rates = fc.positive_rates(spike, (1, 2))
    train = rates["1"]["train"]
    assert train == {"samples": 2 * 13, "positives": 1, "rate": 1 / 26}
    # validation keeps minute 14 only (minute 15 would look into the test period)
    assert rates["1"]["validation"] == {"samples": 2, "positives": 1, "rate": 0.5}
    assert rates["1"]["test"]["samples"] == 2 * 7 and rates["1"]["test"]["positives"] == 2
    assert rates["2"]["validation"] == {"samples": 0, "positives": 0, "rate": None}
    json.dumps(rates, allow_nan=False)


def test_lag_z_ignores_the_future():
    rng = np.random.default_rng(3)
    hosts = ["a", "b", "c"]
    base = {h: rng.poisson(20, 300) for h in hosts}
    first = fc.build(make_sources(host_series(base)), 6.0)[0]
    for t in (0, 60, 150, 298):
        changed = {h: x.copy() for h, x in base.items()}
        for h in hosts:
            changed[h][t + 1 :] = rng.poisson(200, 299 - t)
        second = fc.build(make_sources(host_series(changed)), 6.0)[0]
        for col in ("lag_z", "lag_rate"):
            keep = (first["bin"] <= T0 + timedelta(minutes=t)).to_numpy()
            assert np.array_equal(first[col].to_numpy()[keep], second[col].to_numpy()[keep]), (
                col,
                t,
            )
        x = base["a"].astype(float)
        y = x.copy()
        y[t + 1 :] *= 10
        assert np.array_equal(fc.lag_features(x)[1][: t + 1], fc.lag_features(y)[1][: t + 1])
        assert np.array_equal(fc.lag_features(x)[0][: t + 1], fc.lag_features(y)[0][: t + 1])


def test_peer_rates_use_peers_or_the_fleet_median():
    hosts = ["a", "b", "c", "d"]
    rates = np.array([[1, 2, 3], [10, 20, 30], [100, 200, 300], [1000, 2000, 3000]], dtype=float)
    peers = pl.DataFrame(
        [("a", "b", 0.1), ("a", "c", 0.1), ("b", "e", 0.1), ("c", "a", 0.1)],
        schema=["host", "peer", "distance"],
        orient="row",
    )
    out, no_peer = fc.peer_rates(rates, hosts, peers)
    median = np.median(rates, axis=0)
    assert out[0].tolist() == [55, 110, 165]  # mean of b and c
    assert out[2].tolist() == [1, 2, 3]  # c's only peer is a
    assert out[1].tolist() == median.tolist() and out[3].tolist() == median.tolist()
    assert no_peer == ["b", "d"]


def test_levels_shares_and_top_lists():
    mix = pl.DataFrame(
        [
            (T0, "a", "INFO", "x", 3),
            (T0, "a", "ERROR", "x", 1),
            (T0, "a", "INFO", MISSING, 50),
            (T0, MISSING, "INFO", "z", 50),
            (T0, "a", "INFO", "y", 4),
            (T0, "a", "INFO", "w", 4),
            (T0 + timedelta(minutes=2), "a", "DEBUG", "w", 2),
        ],
        schema={
            "bin": pl.Datetime("us"),
            "host": pl.String,
            "level": pl.String,
            "program": pl.String,
            "n": pl.Int64,
        },
        orient="row",
    )
    services = fc.top_services(mix)
    # (missing) program and host are left out; y and w tie on 4+... w has 6, y 4, x 4: ties by name
    assert services["service"].to_list() == ["w", "x", "y"]
    assert services["lines"].to_list() == [6, 4, 4]
    series = host_series({"a": np.array([5, 0, 3])})
    built = fc.build(make_sources(series, host_mix=mix), 6.0)[0]
    levels = [c for c in built.columns if c.startswith("lvl_")]
    assert levels == [f"lvl_{level.lower()}" for level in fc.LEVELS]
    total = built.select(pl.sum_horizontal(levels)).to_series().to_list()
    assert (
        total[0] == pytest.approx(1.0, abs=1e-6)
        and total[1] == 0
        and total[2] == pytest.approx(1.0, abs=1e-6)
    )
    counts = pl.DataFrame(
        [(5, T0, "a", 4), (3, T0, "a", 4), (9, T0, MISSING, 99), (7, T0, "b", 1)],
        schema={
            "template_id": pl.Int64,
            "bin": pl.Datetime("us"),
            "host": pl.String,
            "n": pl.Int64,
        },
        orient="row",
    )
    texts = pl.DataFrame({"template_id": [3, 5], "template": ["three", "five"]})
    top = fc.top_templates(counts, texts, 2)
    assert top["template_id"].to_list() == [3, 5]  # tie on 4 lines: lower id first; 9 is (missing)
    assert top["text"].to_list() == ["three", "five"]
    assert fc.top_templates(counts, texts.clear())["text"].to_list() == ["", "", ""]


def test_build_on_empty_sources():
    empty = make_sources(host_series({}))
    frame, meta, counts = fc.build(empty, 6.0)
    assert frame.columns == ["bin", "host", "spike"] and frame.is_empty()
    assert meta["host_minutes"] == 0
    json.dumps(meta, allow_nan=False)
    figure = fc.spike_figure(counts, meta, "dark")
    assert figure.layout.annotations[0].text == "No data in the cache" and not figure.data
    assert fc.period_text(meta) == ""
    assert fc.rate_table(meta)["train"].to_list() == ["-", "-", "-"]


def test_build_rejects_non_finite_features(monkeypatch):
    monkeypatch.setattr(fc, "lag_features", lambda x: (np.log1p(x), np.full(len(x), np.nan)))
    with pytest.raises(ValueError, match="lag_z"):
        fc.build(make_sources(host_series({"a": np.arange(10)})), 6.0)


def test_naive_bins_converts_time_zones():
    df = pl.DataFrame({"bin": [datetime(2023, 5, 21, 12)]}).with_columns(
        pl.col("bin").dt.replace_time_zone("Europe/Stockholm")
    )
    out = fc.naive_bins(df)
    assert out.schema["bin"] == pl.Datetime("us") and out["bin"][0] == datetime(2023, 5, 21, 10)


def test_dense_cube_leaves_out_unknown_keys_hosts_and_minutes():
    df = pl.DataFrame(
        [
            (T0, "a", 1, 2),
            (T0, "a", 1, 3),
            (T0 + timedelta(minutes=1), "b", 2, 4),
            (T0, "zz", 1, 100),  # unknown host
            (T0, "a", 9, 100),  # unknown key
            (T0 + timedelta(minutes=5), "a", 1, 100),  # outside the span
            (T0 - timedelta(minutes=1), "a", 1, 100),
        ],
        schema={"bin": pl.Datetime("us"), "host": pl.String, "k": pl.Int64, "n": pl.Int64},
        orient="row",
    )
    cube = fc.dense_cube(df, "k", [1, 2], ["a", "b"], T0, 3)
    assert cube.shape == (2, 2, 3)
    assert cube[0, 0].tolist() == [5, 0, 0] and cube[1, 1].tolist() == [0, 4, 0]
    assert cube.sum() == 9
    assert (
        fc.dense_cube(df, None, [None], ["a", "b"], T0, 3).sum() == 9 + 100
    )  # the unknown key counts


# ---------------------------------------------------------------------- synthetic cache


def naive(t: datetime) -> datetime:
    return utc(t).replace(tzinfo=None)


def test_injected_spikes_are_spike_minutes(forecast_cache):
    built, _, truth = forecast_cache
    frame = built.store.read_parquet(fc.KEY, "frame.parquet")
    meta = built.store.read_json(fc.KEY, "meta.json")
    spikes = set(frame.filter(pl.col("spike"))[["host", "bin"]].iter_rows())
    minute = timedelta(minutes=1)
    allowed: dict[str, list[tuple[datetime, datetime]]] = {}
    for onset, host in zip(truth.forecast_onsets, truth.forecast_hosts, strict=True):
        first = naive(onset)
        assert (host, first) in spikes and (host, first + minute) in spikes
        allowed.setdefault(host, []).append((first - minute, first + 2 * minute))
        for k in (1, 2, 3):  # the precursor minutes are not spikes
            assert (host, first - k * minute) not in spikes
    # the spike host's own spike: every minute bin it overlaps
    host = truth.spike_host
    start = naive(truth.spike_start).replace(second=0, microsecond=0)
    t = start
    while t < naive(truth.spike_end):
        assert (host, t) in spikes
        t += minute
    allowed.setdefault(host, []).append((start - minute, t + minute))
    for h, b in spikes:
        assert any(lo <= b <= hi for lo, hi in allowed.get(h, [])), (h, b)
    assert meta["episodes"] == 25


def test_spike_minutes_equal_the_volume_episodes(forecast_cache, monkeypatch):
    built, _, _ = forecast_cache
    monkeypatch.setattr(volume, "merge_periodic", lambda found: found)
    ctx = DetectContext(built.store, built.settings, lambda _: None)
    found = volume.Volume().detect(ctx)
    rows = found.filter((pl.col("kind") == "host") & pl.col("evidence").str.starts_with("spike:"))
    expected = {
        (r["entity"], r["bin"] + timedelta(minutes=k))
        for r in rows.iter_rows(named=True)
        for k in range(r["window_s"] // 60)
    }
    frame = built.store.read_parquet(fc.KEY, "frame.parquet")
    meta = built.store.read_json(fc.KEY, "meta.json")
    assert set(frame.filter(pl.col("spike"))[["host", "bin"]].iter_rows()) == expected
    assert rows.height == meta["episodes"]


def test_frame_shape_and_columns(forecast_cache):
    built, _, truth = forecast_cache
    frame = built.store.read_parquet(fc.KEY, "frame.parquet")
    meta = built.store.read_json(fc.KEY, "meta.json")
    assert frame.height == meta["hosts"] * meta["minutes"] == meta["host_minutes"]
    assert frame.select(["host", "bin"]).is_duplicated().sum() == 0
    assert MISSING not in set(frame["host"])
    assert sum(frame.null_count().row(0)) == 0
    names = {c for cols in meta["groups"].values() for c in cols}
    assert set(frame.columns) - {"bin", "host", "spike"} == names
    assert list(meta["groups"]) == list(fc.GROUPS)
    for col in names:
        assert frame.schema[col] == pl.Float32
        assert np.isfinite(frame[col].to_numpy()).all()
    for _, block in frame.group_by("host"):
        assert (block["bin"].diff().drop_nulls() == timedelta(minutes=1)).all()
    assert frame["host"].to_list() == sorted(frame["host"].to_list())  # host-major
    precursor = next(t for t in meta["templates"] if t["text"].startswith("Memory pressure"))
    onset, host = naive(truth.forecast_onsets[0]), truth.forecast_hosts[0]
    for k in (1, 2, 3):
        row = frame.filter(
            (pl.col("host") == host) & (pl.col("bin") == onset - timedelta(minutes=k))
        )
        assert row[precursor["column"]][0] > 0
    assert 0 < len(meta["templates"]) <= fc.TOP_TEMPLATES
    json.dumps(meta, allow_nan=False)


def test_stage_order_and_staleness(forecast_cache):
    built, registry, _ = forecast_cache
    assert BUILTIN.index("forecast") == BUILTIN.index("hosts") + 1
    load_builtin()
    keys = [a.key for a in REGISTRY.ordered()]
    assert keys.index("forecast") > keys.index("hosts")
    status = {s.key: s.state for s in stage_statuses(built.settings, registry)}
    assert status["forecast"] == "fresh"
    other = dataclasses.replace(built.settings, burst_k=built.settings.burst_k + 1)
    status = {s.key: s.state for s in stage_statuses(other, registry)}
    assert status["forecast"] == "stale" and status["templates"] == "fresh"


def test_page_layout_and_themes(forecast_cache):
    built, _, _ = forecast_cache
    meta = built.store.read_json(fc.KEY, "meta.json")
    text = str(Forecast().layout(built.store))
    for part in (
        "forecast-spikes",
        ui.fmt_int(meta["host_minutes"]),
        "spike episodes",
        "the global filters do not apply",
        "Positive rate per prediction horizon",
        "15 min",
    ):
        assert part in text
    table = fc.rate_table(meta)
    assert table.height == 3 and table.columns == ["horizon", "train", "validation", "test"]
    assert all("%" in c for c in table["train"].to_list())
    counts = built.store.read_parquet(fc.KEY, "spike_counts.parquet")
    light, dark = (fc.spike_figure(counts, meta, t) for t in ("light", "dark"))
    assert len(light.layout.shapes) == len(dark.layout.shapes) == 3
    assert light.layout.template.layout.font.color != dark.layout.template.layout.font.color
    shown = fc.period_text(meta)
    assert all(p in shown for p in fc.PERIODS) and shown.endswith("(UTC).")


def test_callback_over_http(forecast_cache):
    built, registry, _ = forecast_cache
    client = create_app(built.store, registry).server.test_client()
    assert client.get("/forecast").status_code == 200
    body = {
        "output": "forecast-spikes.figure",
        "outputs": {"id": "forecast-spikes", "property": "figure"},
        "inputs": [{"id": "theme", "property": "data", "value": "dark"}],
        "changedPropIds": ["theme.data"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    assert resp.get_json()["response"]["forecast-spikes"]["figure"]["data"]
