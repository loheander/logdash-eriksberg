import json
import shutil
from datetime import datetime, timedelta

import dash_mantine_components as dmc
import numpy as np
import polars as pl
import pytest

from logdash.analyses import temporal as t
from logdash.analyses.temporal import Temporal
from logdash.app import ui
from logdash.app.filters import Filters
from logdash.cache.store import CacheStore
from tests.conftest import build_with
from tests.synthetic import utc


@pytest.fixture(scope="module")
def tmp(built):
    build_with(built, Temporal())
    return built.store


def naive(dt):
    return utc(dt).replace(tzinfo=None)


def test_tables_conserve_counts_and_stay_small(tmp, synthetic):
    n = synthetic.truth.n_rows
    for size in t.RATE_BINS:
        df = tmp.read_parquet("temporal", f"rate_{size}.parquet")
        assert df["n"].sum() == n and df.height < 5_000_000
    for size in t.HOST_BINS:
        assert tmp.read_parquet("temporal", f"host_{size}.parquet")["n"].sum() == n
    meta = tmp.read_json("temporal", "meta.json")
    assert "eselda14u05s02" in meta["hosts"] and "haproxy" in meta["services"]


def test_finds_spike_and_silent_host(tmp, synthetic):
    truth = synthetic.truth
    an = tmp.read_parquet("temporal", "anomalies.parquet")
    spike = an.filter(
        (pl.col("kind") == "burst") & (pl.col("entity_type") == "host")
        & (pl.col("entity") == truth.spike_host)
    )  # fmt: skip
    assert spike.height >= 1
    row = spike.row(0, named=True)
    assert row["start"] <= naive(truth.spike_end) and row["end"] >= naive(truth.spike_start)
    assert row["peak"] > 1000 and row["score"] > 6
    svc = an.filter((pl.col("entity_type") == "service") & (pl.col("kind") == "burst"))
    assert "neutron-openvswitch-agent" in svc["entity"].to_list()
    gap = an.filter(
        (pl.col("kind") == "gap") & (pl.col("entity") == truth.silent_host)
    ).row(0, named=True)  # fmt: skip
    minutes = (naive(truth.gap_end) - naive(truth.gap_start)).total_seconds() / 60
    assert gap["score"] >= minutes - 3 and abs(gap["start"] - naive(truth.gap_start)) <= timedelta(
        minutes=2
    )
    assert an.filter((pl.col("kind") == "gap") & (pl.col("entity_type") == "all")).is_empty()


def test_detectors_on_arrays():
    x = np.full(200, 20.0)
    x[100] = 400
    found = t.find_bursts(x, 6)
    assert [(f["first"], f["last"]) for f in found] == [(100, 100)]
    assert t.find_bursts(np.full(100, 5.0), 6) == []
    y = np.full(100, 7.0)
    y[10:40] = 0
    assert [(g["first"], g["last"]) for g in t.find_gaps(y, 10)] == [(10, 39)]
    assert t.find_gaps(np.zeros(5), 10) == [] and t.find_gaps(np.zeros(12), 10) == []
    late = np.concatenate(
        [np.zeros(100), np.full(50, 7.0), np.zeros(30), np.full(20, 7.0), np.zeros(40)]
    )
    assert [(g["first"], g["last"]) for g in t.find_gaps(late, 10)] == [(150, 179)]
    assert t.find_drops(np.zeros(50), 6) == []
    assert t.runs(np.array([0, 1, 1, 0, 1])) == [(1, 2), (4, 4)]
    assert t.detect_anomalies(pl.DataFrame(), pl.DataFrame(), 6, 10).is_empty()


def test_detect_anomalies_empty_result():
    bins = [datetime(2023, 1, 1) + timedelta(minutes=i) for i in range(70)]
    rate = pl.DataFrame({"bin": bins, "level": "INFO", "program": "a", "n": 5})
    host = pl.DataFrame({"bin": bins, "host": "h", "n": 5})
    assert t.detect_anomalies(rate, host, 6, 10).is_empty()


def test_spectra_find_period():
    n = 4000
    bins = [datetime(2023, 1, 1) + timedelta(seconds=i) for i in range(n) if i % 50 == 0]
    rate = pl.DataFrame({"bin": bins, "level": "INFO", "program": "svc", "n": 100})
    tables = t.spectral_tables(rate)
    peaks = tables["peaks"].filter(pl.col("service") == "svc").sort("rank")
    assert abs(peaks["period"][0] - 50) < 2
    acf = tables["acf"].filter(pl.col("service") == "svc")
    assert acf["acf"][0] == pytest.approx(1.0) and acf["acf"][50] > 0.8
    assert t.spectral_tables(rate.clear())["acf"].is_empty()
    flat = rate.with_columns(pl.lit(datetime(2023, 1, 1)).alias("bin")).head(1)
    assert t.spectral_tables(flat)["acf"].is_empty()
    assert len(t.autocorrelation(np.ones(5))) == 0 and len(t.periodogram(np.ones(5))[0]) == 0
    p, w = t.log_bin_spectrum(np.arange(1.0, 2000), np.ones(1999), 50)
    assert len(p) <= 50


def test_pick_table_and_range(tmp):
    assert t.pick_table(Filters(), "level", 1) == ("rate_1.parquet", 1)
    assert t.pick_table(Filters(), "level", 900) == ("rate_300.parquet", 300)
    assert t.pick_table(Filters(), "host", 1) == ("host_10.parquet", 10)
    assert t.pick_table(Filters(hosts=("a",)), "level", 60) == ("host_60.parquet", 60)
    meta = tmp.read_json("temporal", "meta.json")
    lo, hi = t.visible_range(Filters(), meta)
    assert hi > lo
    lo2, hi2 = t.visible_range(
        Filters(start="2023-05-20 22:00:00", end="2023-05-20 22:00:00"), meta
    )
    assert hi2 > lo2


def test_rate_figures(tmp):
    for split in t.SPLITS:
        fig = Temporal.rate(tmp, Filters(), split, "dark")
        assert fig.data and sum(sum(tr.y) for tr in fig.data) > 0
    levels = [tr.name for tr in Temporal.rate(tmp, Filters(), "level", None).data]
    assert levels.index("INFO") < levels.index("UNKNOWN")
    assert len(Temporal.rate(tmp, Filters(), "host", None).data) <= t.TOP_SPLIT + 1
    filtered = Temporal.rate(tmp, Filters(services=("haproxy",), levels=("INFO",)), "service", None)
    assert [tr.name for tr in filtered.data] == ["haproxy"]
    note = Temporal.rate(tmp, Filters(hosts=("eselda14u05s02",), levels=("INFO",)), "level", None)
    assert "ignored" in note.layout.annotations[0].text and len(note.data) == 1
    hosts = Temporal.rate(tmp, Filters(hosts=("eselda14u05s02",)), "host", None)
    assert [tr.name for tr in hosts.data] == ["eselda14u05s02"]
    empty = Temporal.rate(tmp, Filters(services=("nope",)), "level", None)
    assert empty.layout.annotations


def test_rate_uses_finer_bins_when_zoomed(tmp, synthetic):
    start = naive(synthetic.truth.spike_start).replace(microsecond=0)
    f = Filters(start=start.isoformat(" "), end=(start + timedelta(minutes=2)).isoformat(" "))
    fig = Temporal.rate(tmp, f, "service", None)
    assert fig.layout.annotations[0].text.startswith("bin 1 s")
    assert max(max(tr.y) for tr in fig.data) > 10


def test_heatmap(tmp):
    fig = Temporal.heatmap(tmp, Filters(), 60, "light")
    assert len(fig.data[0].y) == 7
    only = Temporal.heatmap(tmp, Filters(hosts=("eselda14u05s02",)), 300, None)
    assert list(only.data[0].y) == ["eselda14u05s02"]
    assert Temporal.heatmap(tmp, Filters(hosts=("nope",)), 300, None).layout.annotations


def test_spectrum_figures(tmp):
    acf = tmp.read_parquet("temporal", "acf.parquet")
    spec = tmp.read_parquet("temporal", "spectrum.parquet")
    peaks = tmp.read_parquet("temporal", "peaks.parquet")
    assert t.ALL in acf["service"].to_list()
    assert t.acf_figure(acf, "haproxy", None).data
    assert t.spectrum_figure(spec, peaks, "haproxy", "dark").data
    assert t.acf_figure(acf, "nope", None).layout.annotations
    assert t.spectrum_figure(spec, peaks, "nope", None).layout.annotations


def test_brush_range_and_callback():
    assert t.brush_range(None) is None and t.brush_range({"xaxis.autorange": True}) == "reset"
    lo, hi = t.brush_range(
        {"xaxis.range[0]": "2023-05-21 01:00:00.5", "xaxis.range[1]": "2023-05-21 02:00:00"}
    )
    assert hi - lo == timedelta(minutes=59, seconds=59, microseconds=500000)
    assert t.brush_range({"xaxis.range": ["2023-05-21 01:00:00", "2023-05-21 01:10:00"]})
    assert t.brush_range({"xaxis.range": ["bad", "worse"]}) is None
    assert (
        t.brush_range(
            {"xaxis.range[0]": "2023-05-21 01:00:00", "xaxis.range[1]": "2023-05-21 01:00:00.2"}
        )
        is None
    )
    assert t.brush_range({"other": 1}) is None
    current = {"levels": ["INFO"], "start": None, "end": None}
    out = Temporal.brushed(
        "temporal-rate",
        {"xaxis.range[0]": "2023-05-21 01:00:00", "xaxis.range[1]": "2023-05-21 01:30:00"},
        current,
    )
    assert out["start"] == "2023-05-21 01:00:00" and list(out["levels"]) == ["INFO"]
    assert Temporal.brushed("temporal-reset", None, out)["start"] is None
    assert Temporal.brushed("temporal-rate", {"xaxis.autorange": True}, out)["end"] is None
    assert (
        Temporal.brushed("temporal-rate", {"autosize": True}, out).__class__.__name__ == "NoUpdate"
    )


def test_anomaly_view_and_table(tmp, synthetic):
    an = tmp.read_parquet("temporal", "anomalies.parquet")
    host = synthetic.truth.spike_host
    only_host = t.anomaly_view(an, Filters(hosts=(host,)))
    assert set(only_host.filter(pl.col("entity_type") == "host")["entity"]) <= {host}
    only_svc = t.anomaly_view(an, Filters(services=("haproxy",)))
    assert set(only_svc.filter(pl.col("entity_type") == "service")["entity"]) <= {"haproxy"}
    late = t.anomaly_view(an, Filters(start="2030-01-01 00:00:00"))
    assert late.is_empty() and "no bursts" in str(t.anomaly_table(late))
    early = t.anomaly_view(an, Filters(end="2000-01-01 00:00:00"))
    assert early.is_empty()
    html = str(t.anomaly_table(an))
    assert "/explorer?" in html and "start=" in html
    row = an.row(0, named=True)
    assert t.explorer_href({**row, "entity_type": "all"}).startswith("/explorer?start=")


def test_layout_and_callbacks_over_http(tmp, built):
    from logdash.app.main import create_app

    assert "temporal-split" in str(Temporal().layout(tmp, extra="x"))
    app = create_app(tmp, build_with(built, Temporal()))
    client = app.server.test_client()
    assert client.get("/temporal").status_code == 200

    def call(outputs, inputs, states=(), changed=()):
        body = {
            "output": ".." + "...".join(f"{o}.{p}" for o, p in outputs) + ".."
            if len(outputs) > 1
            else f"{outputs[0][0]}.{outputs[0][1]}",
            "outputs": [{"id": o, "property": p} for o, p in outputs]
            if len(outputs) > 1
            else {"id": outputs[0][0], "property": outputs[0][1]},
            "inputs": [{"id": i, "property": p, "value": v} for i, p, v in inputs],
            "state": [{"id": i, "property": p, "value": v} for i, p, v in states],
            "changedPropIds": list(changed),
        }
        resp = client.post(
            "/_dash-update-component", data=json.dumps(body), content_type="application/json"
        )
        assert resp.status_code == 200, resp.get_data()
        return resp.get_json()

    base = [("filters", "data", {"levels": ["INFO"]})]
    call(
        [("temporal-rate", "figure")],
        [*base, ("temporal-split", "value", "service"), ("theme", "data", "dark")],
    )
    heat = [("temporal-heatmap", "figure"), ("temporal-heatmap", "style")]
    out = call(
        heat,
        [
            *base,
            ("temporal-hbin", "value", "60"),
            ("temporal-hscale", "value", "absolute"),
            ("theme", "data", "dark"),
        ],
    )["response"]
    fig = out["temporal-heatmap"]["figure"]
    assert out["temporal-heatmap"]["style"]["height"] == fig["layout"]["height"] > 320
    out = call(
        heat,
        [
            ("filters", "data", {"hosts": ["nope"]}),
            ("temporal-hbin", "value", "60"),
            ("temporal-hscale", "value", "relative"),
            ("theme", "data", None),
        ],
    )["response"]
    assert out["temporal-heatmap"]["style"]["height"] == 320  # empty figure: the fallback
    call(
        [("temporal-acf", "figure"), ("temporal-spectrum", "figure")],
        [("temporal-service", "value", "haproxy"), ("theme", "data", None)],
    )
    call([("temporal-anomalies", "children")], base)


def test_rate_figure_with_range_not_aligned_to_bins():
    lo = datetime(2023, 5, 21, 0, 1, 10, 665000)
    df = pl.DataFrame(
        {
            "bin": [datetime(2023, 5, 21, 0, 1), datetime(2023, 5, 21, 0, 2)],
            "series": ["INFO"] * 2,
            "n": [5, 7],
        }
    )
    fig = t.rate_figure(df, lo, lo + timedelta(minutes=3), 60, "level", None)
    assert list(fig.data[0].y)[:2] == [5, 7]


def test_relative_scale_centres_on_host_median_and_marks_gaps():
    z = np.array([[10.0, 10.0, 80.0, 0.0, 10.0, 6.0]])
    out = t.relative_scale(z)[0]
    assert out[0] == 0.0
    assert out[2] == 3.0  # x8 hits the clip
    assert out[3] == -3.0  # a gap is the strongest negative value
    assert -3.0 < out[5] < 0.0
    assert np.all(np.abs(out) <= t.REL_CLIP)
    assert t.relative_ratio(np.zeros((1, 3))).max() == 0.0


def test_heatmap_scales(tmp):
    rel = Temporal.heatmap(tmp, Filters(), 300, "light")
    assert rel.data[0].zmid == 0 and rel.data[0].colorbar.ticktext[3] == "×1"
    ab = Temporal.heatmap(tmp, Filters(), 300, "light", "absolute")
    assert ab.data[0].zmid is None and ab.data[0].colorbar.title.text == "log(1+n)"


def test_shorten_middle():
    from logdash.app.figures import shorten_middle

    assert shorten_middle("short") == "short"
    out = shorten_middle("neutron-openvswitch-agent-very-long-name", 20)
    assert len(out) == 20 and "…" in out and out.startswith("neutron-op")


def test_heatmap_height_follows_row_count():
    assert t.heatmap_height(3) == 360
    assert t.heatmap_height(162) >= 15 * 162
    assert t.heatmap_height(1000) > 720


def _many_hosts(n=162):
    hosts = [f"host{i:03d}" for i in range(n)]
    bins = [datetime(2023, 5, 21, 0, 0), datetime(2023, 5, 21, 0, 5)]
    return pl.DataFrame(
        {"bin": bins * n, "host": [h for h in hosts for _ in bins], "n": [3, 5] * n}
    ), hosts


def test_heatmap_labels_every_host():
    df, hosts = _many_hosts()
    fig = t.heatmap_figure(df, 300, "light")
    yaxis = fig.layout.yaxis
    assert yaxis.type == "category" and yaxis.autorange == "reversed"
    assert yaxis.tickmode == "array"
    assert list(fig.data[0].y) == list(yaxis.tickvals) == hosts
    assert list(yaxis.ticktext) == hosts  # short names are not shortened
    assert fig.layout.height >= 15 * 162


def test_heatmap_hover_and_ticks_use_full_and_short_names():
    long = "neutron-openvswitch-agent-very-long-host-name"
    df = pl.DataFrame({"bin": [datetime(2023, 5, 21)] * 2, "host": [long, "b"], "n": [3, 5]})
    fig = t.heatmap_figure(df, 300, None)
    assert list(fig.data[0].y) == ["b", long]  # hover shows %{y}: the full name
    assert "%{y}" in fig.data[0].hovertemplate and "%{text}" not in fig.data[0].hovertemplate
    assert list(fig.layout.yaxis.ticktext) == ["b", t.figures.shorten_middle(long, 30)]
    assert len(fig.layout.yaxis.ticktext[1]) == 30


def test_heatmap_order():
    df, hosts = _many_hosts(5)
    order = ["host003", "host001", "ghost"]
    fig = t.heatmap_figure(df, 300, None, order=order)
    expected = ["host003", "host001", "host000", "host002", "host004"]  # unlisted hosts kept
    assert list(fig.data[0].y) == list(fig.layout.yaxis.tickvals) == expected
    assert list(t.heatmap_figure(df, 300, None).data[0].y) == hosts
    assert fig.data[0].z.shape[0] == 5


def test_heatmap_empty_figure_has_no_rows():
    empty = pl.DataFrame(schema={"bin": pl.Datetime, "host": pl.String, "n": pl.Int64})
    fig = t.heatmap_figure(empty, 300, None)
    assert not fig.data and fig.layout.annotations


def test_heatmap_uses_hosts_stage_order(tmp, tmp_path):
    class NoHosts:
        def exists(self, key, name):
            return False

    assert t.host_order(NoHosts()) is None  # the hosts stage is not built
    shutil.copytree(tmp.module_dir("temporal"), tmp_path / "temporal")
    store = CacheStore(tmp_path)  # own store: the shared fixture stays untouched
    hosts = store.read_json("temporal", "meta.json")["hosts"]
    leaf = sorted(hosts, reverse=True)
    frame = pl.DataFrame({"host": hosts, "leaf": [leaf.index(h) for h in hosts]})
    store.write_parquet("hosts", "hosts.parquet", frame)
    fig = Temporal.heatmap(store, Filters(), 300, None)
    shown = set(fig.data[0].y)
    assert list(fig.data[0].y) == [h for h in leaf if h in shown]
    assert t.host_order(store) == leaf


def test_heatmap_sits_in_a_scroll_area(tmp):
    def walk(node):
        yield node
        children = getattr(node, "children", None)
        for child in children if isinstance(children, list | tuple) else [children]:
            if child is not None and not isinstance(child, str):
                yield from walk(child)

    areas = [c for c in walk(Temporal().layout(tmp)) if isinstance(c, dmc.ScrollArea)]
    assert [a.h for a in areas] == [ui.TALL_PX]
