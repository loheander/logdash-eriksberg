import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import polars as pl
import pytest

from logdash.analyses import anomalies as an
from logdash.analyses import features as ft
from logdash.analyses.anomalies import Anomalies
from logdash.analyses.base import ComputeContext
from logdash.analyses.explorer import fetch_page, link_from_query, merge_link
from logdash.analyses.features import Features
from logdash.analyses.pca import Pca
from logdash.analyses.templates import Templates
from logdash.app.filters import Filters
from logdash.cache.store import CacheStore
from logdash.detectors import base
from logdash.detectors.base import (
    ANOMALY_SCHEMA,
    DETECTORS,
    DetectContext,
    DetectorError,
    DetectorRegistry,
)
from tests.conftest import build_with
from tests.synthetic import GroundTruth, utc


@pytest.fixture(scope="module")
def stage(built):
    build_with(built, Templates(), Features(), Pca(), Anomalies())
    return built.store


def naive(value: datetime) -> datetime:
    return utc(value).replace(tzinfo=None)


class Fake:
    """A detector in one class: all a new detector needs."""

    type = "volume"
    title = "fake"
    version = "1"
    depends_on: tuple[str, ...] = ("temporal",)
    threshold = 2.0
    setting_names = ("burst_k",)

    def __init__(self, key="fake", rows=None):
        self.key = key
        self.rows = rows if rows is not None else [(4.0, "a"), (2.0, "b"), (1.0, "c")]

    def detect(self, ctx):
        return pl.DataFrame(
            {
                "bin": [datetime(2023, 5, 21, 1, i) for i in range(len(self.rows))],
                "window_s": [60] * len(self.rows),
                "kind": ["host"] * len(self.rows),
                "entity": [e for _, e in self.rows],
                "raw": [r for r, _ in self.rows],
                "evidence": ["why"] * len(self.rows),
            }
        )


# ------------------------------------------------------------ framework


def test_normalise_maps_the_threshold_to_one_half():
    raw = pl.Series([2.0, 4.0, 20.0, math.inf])
    assert base.normalise(raw, 2.0).to_list() == pytest.approx([0.5, 0.75, 0.95, 1.0])


def test_finish_validates_filters_and_adds_columns():
    fake = Fake()
    found = fake.detect(None)
    rows = base.finish(fake, found, 10)
    assert rows.schema == ANOMALY_SCHEMA and rows["entity"].to_list() == ["a", "b"]
    assert rows["score"].to_list() == pytest.approx([0.75, 0.5])
    assert set(rows["detector"]) == {"fake"} and set(rows["type"]) == {"volume"}
    assert rows["template_id"].null_count() == 2
    assert base.finish(fake, found, 1)["entity"].to_list() == ["a"]
    dirty = found.with_columns(raw=pl.Series([float("nan"), None, 3.0], dtype=pl.Float64))
    assert base.finish(fake, dirty, 10)["entity"].to_list() == ["c"]
    with pytest.raises(DetectorError, match="evidence"):
        base.finish(fake, found.drop("evidence"), 10)
    assert base.empty().schema == ANOMALY_SCHEMA


def test_finish_keeps_a_template_id():
    fake = Fake()
    found = fake.detect(None).with_columns(template_id=pl.Series([3, 4, 5]))
    assert base.finish(fake, found, 10)["template_id"].to_list() == [3, 4]


def test_registry():
    registry = DetectorRegistry()
    first = registry.add(Fake("one"))
    registry.add(Fake("two"))
    assert len(registry) == 2 and "one" in registry and registry.get("one") is first
    assert [d.key for d in registry.all()] == ["one", "two"] and registry.types() == ["volume"]
    with pytest.raises(DetectorError, match="already"):
        registry.add(Fake("one"))
    bad = Fake("bad")
    bad.threshold = 0
    with pytest.raises(DetectorError, match="threshold"):
        registry.add(bad)
    with pytest.raises(DetectorError, match="unknown"):
        registry.get("zzz")
    registry.clear()
    assert len(registry) == 0
    assert isinstance(Fake(), base.Detector) and not isinstance(object(), base.Detector)


def test_builtin_detectors_are_registered_by_file():
    assert "structural_pca" in DETECTORS
    assert DETECTORS.get("structural_pca").type == "structural"


def test_structural_pca_keeps_the_highest_ratios_per_grid(stage, monkeypatch):
    from logdash.detectors import structural_pca as sp

    detector = DETECTORS.get("structural_pca")
    ctx = DetectContext(stage, stage.settings, lambda _: None)
    full = detector.detect(ctx)
    monkeypatch.setattr(sp, "MAX_PER_GRID", 2)
    capped = detector.detect(ctx)
    assert capped.height == 4 and capped.schema == full.schema
    top = full.filter(pl.col("kind") == "host").sort("raw", descending=True).head(2)
    assert capped.filter(pl.col("kind") == "host")["raw"].to_list() == top["raw"].to_list()
    assert full["evidence"].str.starts_with("SPE ").all()


def test_analysis_follows_the_registered_detectors():
    registry = DetectorRegistry()
    analysis = Anomalies(registry)
    assert analysis.depends_on == () and analysis.setting_names == ()
    registry.add(Fake())
    version = analysis.version
    assert analysis.depends_on == ("temporal",) and analysis.setting_names == ("burst_k",)
    registry.add(Fake("other"))
    assert analysis.version != version and "other=1" in analysis.version
    assert Anomalies().depends_on == ("pca", "templates")


def test_compute_runs_every_registered_detector(built, stage, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in ("templates", "features", "pca"):
        (cache / name).symlink_to(built.store.module_dir(name), target_is_directory=True)
    registry = DetectorRegistry()
    registry.add(Fake("one"))
    registry.add(Fake("two", [(9.0, "z")]))
    ctx = ComputeContext("anomalies", None, built.settings, cache / "anomalies", lambda _: None)  # type: ignore[arg-type]
    (cache / "anomalies").mkdir()
    Anomalies(registry).compute(ctx)
    store = CacheStore(cache)
    rows = store.read_parquet("anomalies", "anomalies.parquet")
    assert rows.schema == ANOMALY_SCHEMA
    assert rows["entity"].to_list() == ["z", "a", "b"]  # by score
    assert rows["detector"].to_list() == ["two", "one", "one"]
    meta = store.read_json("anomalies", "meta.json")
    assert [d["key"] for d in meta["detectors"]] == ["one", "two"]
    assert ctx.rows == {"detector_one": 2, "detector_two": 1}
    assert an.load_types(store) == ["volume"]


def test_no_detectors_writes_an_empty_table(built, tmp_path):
    cache = tmp_path / "cache"
    (cache / "anomalies").mkdir(parents=True)
    ctx = ComputeContext("anomalies", None, built.settings, cache / "anomalies", lambda _: None)  # type: ignore[arg-type]
    Anomalies(DetectorRegistry()).compute(ctx)
    store = CacheStore(cache)
    assert store.read_parquet("anomalies", "anomalies.parquet").height == 0
    out = an.draw_anomalies(store, Filters(), [], "score", None)
    assert out[0].layout.annotations and "no anomalies" in str(out[1])


# ------------------------------------------------------- the built table


def test_built_table_and_manifest(stage):
    entry = stage.manifest().get("anomalies")
    assert entry is not None and entry.rows["detector_structural_pca"] > 0
    rows = an.load_anomalies(stage)
    assert rows.schema == ANOMALY_SCHEMA and rows.height == entry.rows["anomalies.parquet"]
    assert rows["score"].is_between(0.5, 1).all()
    assert rows["score"].to_list() == sorted(rows["score"].to_list(), reverse=True)
    assert (rows["raw"] >= DETECTORS.get("structural_pca").threshold).all()
    assert set(rows["kind"]) == {"service", "host"} and rows["evidence"].str.contains("SPE").all()
    assert rows.group_by("detector").len().height == 1
    meta = stage.read_json("anomalies", "meta.json")
    assert meta["detectors"][0]["rows"] == rows.height


# Injected anomalies of the synthetic data (``tests/synthetic.py``) per anomaly type. Each entry
# says what the rows of that type must contain; the test is parametrised over them and skipped
# for a type that no registered detector emits yet (issues 4 to 9 add those detectors).
Found = Callable[[GroundTruth, pl.DataFrame], bool]


def in_range(rows: pl.DataFrame, lo: datetime, hi: datetime) -> pl.DataFrame:
    return rows.filter(
        (pl.col("bin") >= ft.floor_window(naive(lo), 60)) & (pl.col("bin") <= naive(hi))
    )


def spike_service(truth: GroundTruth, rows: pl.DataFrame) -> bool:
    hit = in_range(rows, truth.spike_start, truth.spike_end)
    return not hit.filter(
        (pl.col("kind") == "service") & (pl.col("entity") == "neutron-openvswitch-agent")
    ).is_empty()


def novel_template_window(truth: GroundTruth, rows: pl.DataFrame) -> bool:
    hit = rows.filter(pl.col("bin") >= ft.floor_window(naive(truth.novel_first_seen), 60))
    return not hit.filter(pl.col("entity") == "haproxy").is_empty()


def spike_host(truth: GroundTruth, rows: pl.DataFrame) -> bool:
    hit = in_range(rows, truth.spike_start, truth.spike_end)
    return not hit.filter(pl.col("entity") == truth.spike_host).is_empty()


def silent_host_gap(truth: GroundTruth, rows: pl.DataFrame) -> bool:
    hit = in_range(rows, truth.gap_start, truth.gap_end)
    return not hit.filter(pl.col("entity") == truth.silent_host).is_empty()


def novel_first_seen(truth: GroundTruth, rows: pl.DataFrame) -> bool:
    hit = in_range(rows, truth.novel_first_seen, truth.novel_first_seen)
    return not hit.is_empty()


def out_of_order_requests(truth: GroundTruth, rows: pl.DataFrame) -> bool:
    return set(truth.out_of_order_request_ids) <= set(rows["entity"])


INJECTED: list[tuple[str, str, Found]] = [
    ("volume spike", "volume", spike_host),
    ("silent host gap", "volume", silent_host_gap),
    ("spike seen structurally", "structural", spike_service),
    ("novel template seen structurally", "structural", novel_template_window),
    ("novel template", "novel", novel_first_seen),
    ("out-of-order request", "sequence", out_of_order_requests),
]
NO_INJECTION = {"contextual", "severity"}  # the synthetic data has none yet (issues 5 and 9)


@pytest.mark.parametrize(("name", "kind", "found"), INJECTED, ids=[i[0] for i in INJECTED])
def test_injected_anomaly_is_found_with_its_type(stage, synthetic, name, kind, found):
    if kind not in DETECTORS.types():
        pytest.skip(f"no registered detector of type {kind!r} yet")
    rows = an.load_anomalies(stage).filter(pl.col("type") == kind)
    assert found(synthetic.truth, rows), f"{name} not found among {rows.height} {kind} rows"


def test_every_registered_type_has_an_expectation():
    expected = {kind for _, kind, _ in INJECTED} | NO_INJECTION
    assert set(DETECTORS.types()) <= expected


# ------------------------------------------------------------------ page


def test_select_by_range_type_and_entity(stage):
    rows = an.load_anomalies(stage)
    some = rows.row(0, named=True)
    start = some["bin"]
    window = Filters(start=stamp(start), end=stamp(start + timedelta(minutes=1)))
    inside = an.select(rows, window, [])
    assert 0 < inside.height < rows.height and (inside["bin"] == start).all()
    assert an.select(rows, Filters(), ["structural"]).height == rows.height
    assert an.select(rows, Filters(), ["volume"]).is_empty()
    by_service = an.select(rows, Filters(services=("haproxy",)), [])
    assert set(by_service.filter(pl.col("kind") == "service")["entity"]) == {"haproxy"}
    assert (by_service["kind"] == "host").any()  # the service filter leaves host rows alone
    by_host = an.select(rows, Filters(hosts=("nope",)), [])
    assert (by_host["kind"] == "service").all()


def stamp(value):
    return an.stamp(value)


def test_sort_rows(stage):
    rows = an.load_anomalies(stage).head(500)
    assert an.sort_rows(rows, "time")["bin"].is_sorted()
    assert an.sort_rows(rows, "type")["type"].is_sorted()
    assert an.sort_rows(rows, "entity")["entity"].is_sorted()
    assert an.sort_rows(rows, "score")["score"].is_sorted(descending=True)


def test_explorer_url_and_table(stage, synthetic):
    start = datetime(2023, 5, 21, 1, 2, 0)
    url = an.explorer_url(start, 60, "host", "h1", 7)
    assert urlsplit(url).path == "/explorer"
    assert dict(parse_qsl(urlsplit(url).query)) == {
        "start": "2023-05-21 01:02:00",
        "end": "2023-05-21 01:03:00",
        "host": "h1",
        "template": "7",
    }
    assert "request_id=r" in an.explorer_url(start, 60, "request", "r", None)
    assert "service=" not in an.explorer_url(start, 60, "unknown", "x", None)
    table, hrefs = an.table_rows(an.load_anomalies(stage).head(5))
    assert table.height == 5 == len(hrefs) and table.columns[:3] == ["window", "type", "score"]


def test_links_drill_down_to_the_raw_lines(stage, synthetic):
    truth = synthetic.truth
    rows = an.load_anomalies(stage).filter(
        (pl.col("entity") == "neutron-openvswitch-agent")
        & (pl.col("bin") >= ft.floor_window(naive(truth.spike_start), 60))
        & (pl.col("bin") <= naive(truth.spike_end))
    )
    assert not rows.is_empty()
    row = rows.row(0, named=True)
    query = dict(
        parse_qsl(
            urlsplit(
                an.explorer_url(
                    row["bin"], row["window_s"], row["kind"], row["entity"], row["template_id"]
                )
            ).query
        )
    )
    filters = merge_link(Filters(), link_from_query(query))
    lines, _ = fetch_page(stage.connect(), stage, filters)
    assert lines and all(line["program"] == "neutron-openvswitch-agent" for line in lines)


def test_timeline_figure(stage):
    rows = an.load_anomalies(stage)
    fig = an.timeline_figure(rows, ["structural"], "dark")
    assert len(fig.data) == 1 and len(fig.data[0].x) == min(rows.height, an.MAX_POINTS)
    assert fig.data[0].text[0].count("<br>") == 3
    assert an.timeline_figure(rows.head(0), ["structural"], None).layout.annotations
    shown = an.timeline_figure(rows, ["other", "structural"], None)
    assert [t.name for t in shown.data] == ["structural"]  # a type without rows has no trace
    if rows.height > an.MAX_POINTS:
        assert "highest" in fig.layout.annotations[0].text


def test_draw_anomalies(stage):
    fig, table, hint = an.draw_anomalies(stage, Filters(), [], "score", "light")
    assert fig.data and "/explorer?start=" in str(table) and "anomalies in the selection" in hint
    assert (
        "level filter" in an.draw_anomalies(stage, Filters(levels=("ERROR",)), [], "time", None)[2]
    )
    nothing = an.draw_anomalies(stage, Filters(), ["volume"], "score", None)
    assert nothing[0].layout.annotations and "no anomalies" in str(nothing[1])
    assert "0 anomalies" in nothing[2]


def test_degrades_without_the_stage(tmp_path):
    out = an.draw_anomalies(CacheStore(tmp_path), Filters(), [], "score", None)
    assert out[0].layout.annotations and "not built" in str(out[1]) and out[2] == ""
    assert an.load_types(CacheStore(tmp_path)) == DETECTORS.types()


def test_load_follows_the_file(stage):
    an.clear_caches()
    first = an.load_anomalies(stage)
    assert an.load_anomalies(stage) is first


def test_value_helpers():
    assert an.sort_value("time") == "time" and an.sort_value("x") == "score"
    assert an.sort_value(None) == "score"
    assert an.type_values(None) == [] and an.type_values(["a", 3]) == ["a"]  # type: ignore[list-item]


def test_layout_and_callback_over_http(stage, built):
    from logdash.app.main import create_app

    assert "anomalies-timeline" in str(Anomalies().layout(stage))
    app = create_app(stage, build_with(built, Templates(), Features(), Pca(), Anomalies()))
    client = app.server.test_client()
    assert client.get("/anomalies").status_code == 200
    outputs = ["anomalies-timeline", "anomalies-table", "anomalies-hint"]
    props = ["figure", "children", "children"]
    body = {
        "output": ".." + "...".join(f"{o}.{p}" for o, p in zip(outputs, props, strict=True)) + "..",
        "outputs": [{"id": o, "property": p} for o, p in zip(outputs, props, strict=True)],
        "inputs": [
            {"id": "anomalies-types", "property": "value", "value": ["structural"]},
            {"id": "anomalies-sort", "property": "value", "value": "time"},
            {"id": "filters", "property": "data", "value": {}},
            {"id": "theme", "property": "data", "value": "dark"},
        ],
        "changedPropIds": ["anomalies-sort.value"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200, resp.get_data()
    assert resp.get_json()["response"]["anomalies-timeline"]["figure"]["data"]
    body["inputs"][0]["value"] = None
    body["inputs"][1]["value"] = "bogus"
    assert (
        client.post(
            "/_dash-update-component", data=json.dumps(body), content_type="application/json"
        ).status_code
        == 200
    )
