import json
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import numpy as np
import polars as pl
import pytest
from scipy import sparse

from logdash.analyses import anomalies as an
from logdash.analyses import features as ft
from logdash.analyses.anomalies import Anomalies
from logdash.analyses.base import ComputeContext
from logdash.analyses.explorer import fetch_page, link_from_query, merge_link
from logdash.analyses.features import Features
from logdash.analyses.hosts import Hosts
from logdash.analyses.pca import Pca
from logdash.analyses.templates import Templates
from logdash.analyses.temporal import Temporal
from logdash.app.filters import Filters
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors import _episodes as episodes
from logdash.detectors import base
from logdash.detectors import structural_iforest as iforest
from logdash.detectors.base import (
    ANOMALY_SCHEMA,
    DETECTORS,
    KINDS,
    DetectContext,
    DetectorError,
    DetectorRegistry,
    declare_kind,
)
from tests.conftest import build_with
from tests.synthetic import GroundTruth, utc


@pytest.fixture(scope="module")
def stage(built):
    build_with(built, Templates(), Features(), Pca(), Temporal(), Hosts(), Anomalies())
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


def scored(raws, threshold=2.0, kinds=None):
    """Scores of ``raws`` (one kind unless ``kinds`` says otherwise), as ``finish`` gives them."""
    frame = pl.DataFrame(
        {"raw": raws, "kind": kinds or ["host"] * len(raws)},
        schema={"raw": pl.Float64, "kind": pl.String},
    )
    return frame.select(*base.score_columns(threshold))["score"].to_list()


def test_score_keeps_its_spread_and_stays_in_range():
    raws = [2.0 * 2**k for k in range(0, 12)]  # 1x to 2048x the threshold
    scores = scored(raws)
    assert scores == sorted(scores) and len({round(s, 6) for s in scores}) == len(raws)
    assert all(0 < s <= 1 for s in scores)
    assert scores[-1] - scores[-2] > 0.01  # strong anomalies are not squeezed together
    assert abs(scored([1000.0, 2.0, 2.0], kinds=["a", "b", "b"])[0] - 1.0) < 1e-9  # alone: top


def test_score_is_per_kind_and_clips_infinity():
    # the same raw values rank the same in a kind with a heavier tail
    light = scored([2.0, 4.0, 8.0], kinds=["service"] * 3)
    heavy = scored([2.0, 400.0, 8000.0, 2.0, 4.0, 8.0], kinds=["host"] * 3 + ["service"] * 3)
    assert heavy[3:] == pytest.approx(light)
    low, top = scored([4.0, math.inf])  # infinity is clipped, not propagated
    assert 0 < low < top == pytest.approx(1.0)


def test_finish_validates_filters_and_adds_columns():
    fake = Fake()
    found = fake.detect(None)
    rows, above = base.finish(fake, found, 10)
    assert above == 2 and rows.schema == ANOMALY_SCHEMA and rows["entity"].to_list() == ["a", "b"]
    assert rows["score"].to_list() == sorted(rows["score"].to_list(), reverse=True)
    assert set(rows["detector"]) == {"fake"} and set(rows["type"]) == {"volume"}
    assert rows["template_id"].null_count() == 2
    assert base.finish(fake, found, 1).rows["entity"].to_list() == ["a"]
    assert base.finish(fake, found, 10, threshold=3.0).rows["entity"].to_list() == ["a"]
    dirty = found.with_columns(raw=pl.Series([float("nan"), None, 3.0], dtype=pl.Float64))
    assert base.finish(fake, dirty, 10).rows["entity"].to_list() == ["c"]
    with pytest.raises(DetectorError, match="evidence"):
        base.finish(fake, found.drop("evidence"), 10)
    assert base.empty().schema == ANOMALY_SCHEMA


def test_finish_edge_cases_of_raw():
    fake = Fake(rows=[(math.inf, "a"), (math.nan, "b"), (0.0, "c"), (-3.0, "d"), (-math.inf, "e")])
    rows, above = base.finish(fake, fake.detect(None), 10)
    assert rows["entity"].to_list() == ["a"] and above == 1
    assert rows["raw"].to_list() == [math.inf] and rows["score"][0] == pytest.approx(1.0)
    none, zero = base.finish(fake, fake.detect(None).head(0), 10)
    assert none.is_empty() and none.schema == ANOMALY_SCHEMA and zero == 0


def test_finish_caps_per_entity_then_overall():
    rows = [(5.0 + i, "busy") for i in range(6)] + [(3.0, "quiet"), (2.5, "other")]
    fake = Fake(rows=rows)
    found = fake.detect(None)
    kept, above = base.finish(fake, found, 100, max_per_entity=2)
    assert above == 8 and kept["entity"].to_list().count("busy") == 2
    assert {"quiet", "other"} <= set(kept["entity"])  # an isolated anomaly survives
    assert kept.height == 4
    assert base.finish(fake, found, 3, max_per_entity=2).rows.height == 3


def test_finish_rejects_an_undeclared_kind():
    fake = Fake()
    found = fake.detect(None).with_columns(kind=pl.lit("cluster"))
    with pytest.raises(DetectorError, match=r"kind \['cluster'\].*declare_kind"):
        base.finish(fake, found, 10)
    nulls = fake.detect(None).with_columns(kind=pl.lit(None, dtype=pl.String))
    with pytest.raises(DetectorError, match="null"):
        base.finish(fake, nulls, 10)


def test_declare_kind_is_validated():
    with pytest.raises(DetectorError, match="already"):
        declare_kind("host")
    with pytest.raises(DetectorError, match="explorer"):
        declare_kind("bad", link_param="nope")
    with pytest.raises(DetectorError, match="Filters"):
        declare_kind("bad", filter_field="nope")
    assert "bad" not in KINDS
    assert KINDS["request"].link_window is False and KINDS["host"].filter_field == "hosts"


def test_threshold_of_uses_threshold_for_when_present():
    fake = Fake()
    assert base.threshold_of(fake, None) == 2.0  # type: ignore[arg-type]
    fake.threshold_for = lambda settings: 7.5  # type: ignore[attr-defined]
    assert base.threshold_of(fake, None) == 7.5  # type: ignore[arg-type]


def test_finish_keeps_a_template_id():
    fake = Fake()
    found = fake.detect(None).with_columns(template_id=pl.Series([3, 4, 5]))
    assert base.finish(fake, found, 10).rows["template_id"].to_list() == [3, 4]


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
    with pytest.raises(DetectorError, match="protocol"):
        registry.add(object())  # type: ignore[arg-type]


def test_builtin_detectors_are_registered_by_file():
    for key in ("structural_pca", "structural_iforest"):
        assert key in DETECTORS and DETECTORS.get(key).type == "structural"
    assert not any(key.startswith("_") for key in DETECTORS._items)  # helper modules: no detector


def test_structural_detectors_flag_a_few_windows_per_entity(stage):
    ctx = DetectContext(stage, stage.settings, lambda _: None)
    for key, evidence in (("structural_pca", "SPE "), ("structural_iforest", "isolation score ")):
        detector = DETECTORS.get(key)
        found = detector.detect(ctx)
        own = found.filter(pl.col("kind") != "fleet")
        assert own["evidence"].str.starts_with(evidence).all()
        assert (found["raw"] >= detector.threshold).all() and own.height > 0
        assert (found["window_s"] % 60 == 0).all()
        kept, above = base.finish(detector, found, 20_000, an.MAX_PER_ENTITY)
        assert above == found.height == kept.height  # episodes are few, nothing is cut
        assert {"service", "host", "fleet"} >= set(found["kind"])
        assert ctx.meta["host"]["episodes"] > 0


def test_structural_detectors_are_deterministic(stage):
    for key in ("structural_pca", "structural_iforest"):
        detector = DETECTORS.get(key)
        first = detector.detect(DetectContext(stage, stage.settings, lambda _: None))
        second = detector.detect(DetectContext(stage, stage.settings, lambda _: None))
        assert first.equals(second)


def test_structural_iforest_meta_lists_skipped_entities_and_fleet_minutes(stage):
    ctx = DetectContext(stage, stage.settings, lambda _: None)
    DETECTORS.get("structural_iforest").detect(ctx)
    for group in ("service", "host"):
        info = ctx.meta[group]
        assert info["windows"] > 0 and info["episodes"] <= info["flagged"] - info["fleet_windows"]
        assert info["skipped_entities"] >= 0 and isinstance(info["worst_fleet_minutes"], list)


# ------------------------------------------------------------------ structural helpers


def windows(entities: dict[str, list[float]], start=datetime(2023, 5, 21)) -> pl.DataFrame:
    rows = [
        (start + timedelta(minutes=i), name, x)
        for name, xs in entities.items()
        for i, x in enumerate(xs)
    ]
    return pl.DataFrame(rows, schema=["bin", "entity", "x"], orient="row").with_row_index("row")


def test_entity_level_is_per_entity_and_floored():
    rng = np.random.default_rng(0)
    quiet = rng.uniform(0.1, 0.9, 2000)
    loud = rng.uniform(100, 200, 2000)
    frame = windows({"quiet": list(quiet), "loud": list(loud)})
    level = frame.with_columns(level=episodes.entity_level("x", 1.0))
    flagged = level.filter(pl.col("x") > pl.col("level"))
    assert level.filter(pl.col("entity") == "quiet")["level"].unique().to_list() == [1.0]
    assert flagged.filter(pl.col("entity") == "quiet").is_empty()  # chronic or small: never
    assert 0 < flagged.filter(pl.col("entity") == "loud").height <= math.ceil(0.005 * 2000)


def test_iid_noise_flags_at_most_the_budget():
    rng = np.random.default_rng(1)
    n = 3000
    frame = windows({"a": list(rng.poisson(5, n) + 1.0)}).with_columns(
        level=episodes.entity_level("x", 1.0)
    )
    assert frame.filter(pl.col("x") > pl.col("level")).height <= math.ceil(0.005 * n)


def test_constant_entity_flags_nothing():
    frame = windows({"a": [50.0] * 500}).with_columns(level=episodes.entity_level("x", 1.0))
    assert frame.filter(pl.col("x") > pl.col("level")).is_empty()


def test_fleet_wide_minutes_are_dropped_and_counted():
    names = [f"h{i}" for i in range(8)]
    every = windows({n: [1.0] * 10 for n in names})
    flagged = every.filter(pl.col("bin") == datetime(2023, 5, 21, 0, 3))  # all 8 at minute 3
    flagged = flagged.with_columns(raw=pl.Series([1.0 + i for i in range(8)]))
    flagged = pl.concat(
        [
            flagged,
            every.filter(
                (pl.col("bin") == datetime(2023, 5, 21, 0, 7)) & (pl.col("entity") == "h0")
            ).with_columns(raw=pl.lit(2.0)),
        ]
    )
    kept, drop = episodes.drop_fleet_wide(flagged, every, 60, "hosts")
    assert kept["entity"].to_list() == ["h0"] and kept["bin"][0].minute == 7
    assert (drop.windows, drop.minutes) == (8, 1)
    assert drop.worst == [{"minute": "2023-05-21 00:03:00", "share": 1.0}]
    # the minute stays visible as one row of kind fleet: severity share / 25 %, worst entities
    row = drop.rows.row(0, named=True)
    assert drop.rows.height == 1 and row["kind"] == "fleet" and row["entity"] == "all hosts"
    assert (row["bin"], row["window_s"], row["raw"]) == (datetime(2023, 5, 21, 0, 3), 60, 4.0)
    assert row["evidence"] == "8 of 8 hosts flagged (100%); highest: h7, h6, h5"
    assert base.finish(DETECTORS.get("structural_pca"), drop.rows, 10).rows.height == 1
    # a small fleet is never fleet-wide: two of two entities flagged is two anomalies
    small = every.filter(pl.col("entity").is_in(["h0", "h1"]))
    kept, drop = episodes.drop_fleet_wide(
        small.filter(pl.col("bin") == datetime(2023, 5, 21, 0, 3)).with_columns(raw=pl.lit(2.0)),
        small,
        60,
        "hosts",
    )
    assert kept.height == 2 and drop.minutes == 0 and drop.rows.is_empty()


def test_episodes_merge_close_windows_per_entity():
    start = datetime(2023, 5, 21)
    rows = [  # a: minutes 0, 2, 5 (gaps <= 5: one episode, 6 windows), 20 (another)
        (start + timedelta(minutes=m), "a", raw, i)
        for i, (m, raw) in enumerate([(0, 1.5), (2, 4.0), (5, 2.0), (20, 1.2)])
    ] + [(start + timedelta(minutes=1), "b", 3.0, 10), (start + timedelta(minutes=8), "b", 2.0, 11)]
    merged = episodes.merge_episodes(
        pl.DataFrame(rows, schema=["bin", "entity", "raw", "row"], orient="row"), 60
    ).sort("entity", "bin")
    assert merged["entity"].to_list() == ["a", "a", "b", "b"]
    assert merged["minutes"].to_list() == [6, 1, 1, 1]
    assert merged["raw"].to_list() == [4.0, 1.2, 3.0, 2.0]
    assert merged["row"].to_list() == [1, 3, 10, 11]  # the peak window
    assert merged["bin"][0] == start


def test_forest_skips_short_and_constant_entities_and_scores_an_outlier():
    rng = np.random.default_rng(2)
    x = rng.normal(size=(300, 4))
    x[17] = 12.0
    s = iforest.isolation_scores(x)
    assert int(np.argmax(s)) == 17 and s[17] > 0.6
    assert (iforest.raw_score(np.array([0.5, 1.0, 0.25])) == [1.0, math.inf, 0.5]).all()
    scaled = sparse.csr_matrix(np.vstack([np.ones((150, 3)), rng.integers(0, 3, (150, 3))]))
    assert iforest.entity_windows(scaled, np.arange(99)) is None  # too short
    assert iforest.entity_windows(scaled, np.arange(150)) is None  # identical rows
    found = iforest.entity_windows(scaled, np.arange(150, 300))
    assert found is not None and found[0].shape == (150, 3)


def test_iforest_scores_an_entity_on_its_own_history():
    # entity "a" always logs template 0 and 1 in a fixed mix; "b" always the opposite mix: a
    # pooled forest would flag one of them everywhere, per-entity forests flag neither
    rng = np.random.default_rng(3)
    n = 300
    a = np.column_stack([rng.integers(8, 12, n), rng.integers(0, 2, n)])
    b = np.column_stack([rng.integers(0, 2, n), rng.integers(8, 12, n)])
    a[100] = [0, 30]  # the one unusual window of a
    counts = sparse.csr_matrix(np.log1p(np.vstack([a, b]).astype(float)))
    bins = np.tile(
        np.datetime64("2023-05-21T00:00", "us") + np.arange(n) * np.timedelta64(60, "s"), 2
    )
    matrix = ft.Matrix(counts, bins, np.repeat([0, 1], n), ["a", "b"], np.array([7, 8]), 60)
    scores, skipped, _ = iforest.score_grid(matrix, counts)
    assert skipped == 0
    top = scores.sort("s", descending=True).head(1)
    assert top["entity"][0] == "a" and top["row"][0] == 100
    assert scores.group_by("entity").agg(pl.col("s").median())["s"].max() < 0.55


# ------------------------------------------------------------------ ranking


def test_overall_is_reciprocal_rank_within_detector_and_kind():
    class Two(Fake):
        key = "two"
        type = "structural"

    found = pl.DataFrame(
        {
            "bin": [datetime(2023, 5, 21, 0, i) for i in range(6)],
            "window_s": [60] * 6,
            "kind": ["host"] * 4 + ["service"] * 2,
            "entity": ["h"] * 4 + ["s"] * 2,
            "raw": [5.0, 4.0, 3.0, 2.0, 9.0, 8.0],
            "evidence": [""] * 6,
        }
    )
    rows = base.finish(Two(), found, 100, 100).rows
    by_kind = {k: part.sort("score", descending=True) for (k,), part in rows.group_by("kind")}
    assert by_kind["host"]["overall"].to_list() == pytest.approx([1 / 61, 1 / 62, 1 / 63, 1 / 64])
    assert by_kind["service"]["overall"].to_list() == pytest.approx([1 / 61, 1 / 62])
    # more rows in a detector do not lower its best row: ranks, not percentiles
    assert rows["overall"].max() == pytest.approx(1 / 61)


def test_combined_top_mixes_types_and_entities(stage):
    rows = an.load_anomalies(stage)
    ranked = an.sort_rows(rows, "overall")
    assert set(rows["type"]) <= set(ranked.head(20)["type"])  # no type is crowded out
    top = ranked.head(an.TABLE_ROWS)
    # the synthetic stage has 12 entities in its 23 rows: the plan's 10 % per entity (of 100 rows)
    # is checked on the real cache (``tests/benchmark``); here no entity may have a fifth
    assert top.group_by("kind", "entity").len()["len"].max() <= 0.2 * top.height
    assert an.DEFAULT_SORT == "overall"


def test_analysis_follows_the_registered_detectors():
    registry = DetectorRegistry()
    analysis = Anomalies(registry)
    assert analysis.depends_on == () and analysis.setting_names == ()
    registry.add(Fake())
    version = analysis.version
    assert analysis.depends_on == ("temporal",) and analysis.setting_names == ("burst_k",)
    registry.add(Fake("other"))
    assert analysis.version != version and "other=1" in analysis.version
    assert analysis.version.startswith(f"{base.FRAMEWORK_VERSION}:{an.MAX_PER_ENTITY}:")
    assert Anomalies().depends_on == ("features", "hosts", "pca", "templates", "temporal")


def run_stage(built, tmp_path, registry, log=None):
    """Run the anomalies stage over the built upstream stages; returns (store, ctx)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in ("templates", "features", "pca"):
        (cache / name).symlink_to(built.store.module_dir(name), target_is_directory=True)
    progress = log.append if log is not None else (lambda _: None)
    ctx = ComputeContext("anomalies", None, built.settings, cache / "anomalies", progress)  # type: ignore[arg-type]
    (cache / "anomalies").mkdir()
    Anomalies(registry).compute(ctx)
    return CacheStore(cache), ctx


def test_compute_runs_every_registered_detector(built, stage, tmp_path):
    registry = DetectorRegistry()
    registry.add(Fake("one"))
    registry.add(Fake("two", [(9.0, "z")]))
    store, ctx = run_stage(built, tmp_path, registry)
    rows = store.read_parquet("anomalies", "anomalies.parquet")
    assert rows.schema == ANOMALY_SCHEMA
    assert rows["score"].to_list() == sorted(rows["score"].to_list(), reverse=True)
    assert rows["entity"][-1] == "b" and set(rows["entity"]) == {"z", "a", "b"}
    assert sorted(rows["detector"].to_list()) == ["one", "one", "two"]
    meta = store.read_json("anomalies", "meta.json")
    assert [d["key"] for d in meta["detectors"]] == ["one", "two"]
    one = meta["detectors"][0]
    assert (one["above_threshold"], one["rows"], one["dropped"]) == (2, 2, 0)
    assert meta["max_per_entity"] == an.MAX_PER_ENTITY
    assert ctx.rows == {"detector_one": 2, "detector_two": 1}
    assert an.load_types(store) == ["volume"]


class Stub:
    """A detector of a new type on a new kind, in one class and nothing else changed."""

    key = "stub"
    type = "cluster_drift"
    title = "stub"
    version = "1"
    depends_on: tuple[str, ...] = ()
    threshold = 1.0
    setting_names: tuple[str, ...] = ()

    def detect(self, ctx):
        return pl.DataFrame(
            {
                "bin": [datetime(2023, 5, 21, 3, 0), datetime(2023, 5, 21, 4, 0)],
                "window_s": [300, 300],
                "kind": ["cluster", "cluster"],
                "entity": ["7", "8"],
                "raw": [30.0, 3.0],
                "evidence": ["drifted", "drifted a little"],
                "template_id": [7, 8],
            }
        )


@pytest.fixture
def cluster_kind():
    declare_kind("cluster", link_param="template", filter_field="services", link_window=True)
    yield KINDS["cluster"]
    del KINDS["cluster"]


def test_a_new_detector_type_and_kind_reach_the_page(built, tmp_path, cluster_kind):
    registry = DetectorRegistry()
    registry.add(Fake("one"))
    registry.add(Stub())
    store, _ = run_stage(built, tmp_path, registry)
    assert an.load_types(store) == ["volume", "cluster_drift"]
    rows = an.load_anomalies(store)
    assert set(rows["type"]) == {"volume", "cluster_drift"}
    # filters: the cluster kind declared the service filter, host rows do not follow it
    chosen = an.select(rows, Filters(services=("7",)), [])
    assert set(chosen.filter(pl.col("kind") == "cluster")["entity"]) == {"7"}
    assert (chosen["kind"] == "host").any()
    # links follow the declaration
    table, hrefs = an.table_rows(rows.filter(pl.col("type") == "cluster_drift"))
    assert table["type"].to_list() == ["cluster_drift"] * 2
    query = dict(parse_qsl(urlsplit(hrefs[0]).query))
    assert query == {"start": "2023-05-21 03:00:00", "end": "2023-05-21 03:05:00", "template": "7"}
    fig, _, hint = an.draw_anomalies(store, Filters(), [], "type", None)
    assert [t.name for t in fig.data] == ["volume", "cluster_drift"]
    assert "2 of 2 cluster_drift" in hint and "2 of 2 volume" in hint
    # without the declaration the kind is rejected loudly, naming the detector
    del KINDS["cluster"]
    try:
        with pytest.raises(DetectorError, match="stub.*cluster"):
            base.finish(Stub(), Stub().detect(None), 10)
    finally:
        KINDS["cluster"] = cluster_kind


class Broken:
    key = "broken"
    type = "volume"
    title = "broken"
    version = "1"
    depends_on: tuple[str, ...] = ()
    threshold = 1.0
    setting_names: tuple[str, ...] = ()

    def detect(self, ctx):
        raise ValueError("boom")


class Malformed(Broken):
    key = "malformed"

    def detect(self, ctx):
        return pl.DataFrame({"bin": [datetime(2023, 5, 21)]})


class WithMeta(Fake):
    """Fills the meta with what ``json.dumps`` cannot take: numpy values and NaN."""

    def detect(self, ctx):
        ctx.meta.update(
            {
                "count": np.int64(3),
                "ratio": np.float32(0.5),
                "missing": float("nan"),
                "names": {"b", "a"},
                "nested": {"values": np.array([1, 2]), "flag": np.bool_(True), "inf": math.inf},
                "note": "text",
            }
        )
        return super().detect(ctx)


class BrokenWithMeta(Broken):
    key = "broken_meta"

    def detect(self, ctx):
        ctx.meta["seen"] = np.int64(7)
        raise ValueError("boom")


def test_detector_meta_is_stored_as_json_safe_info(stage):
    registry = DetectorRegistry()
    for detector in (WithMeta("meta"), BrokenWithMeta(), Fake("plain")):
        registry.add(detector)
    ctx = DetectContext(stage, stage.settings, lambda _: None)
    _, info = an.run_detectors(registry, ctx)
    by_key = {entry["key"]: entry for entry in info}
    json.dumps(info, allow_nan=False)  # numpy values and NaN cannot fail the write
    assert by_key["meta"]["info"] == {
        "count": 3,
        "ratio": 0.5,
        "missing": None,
        "names": ["a", "b"],
        "nested": {"values": [1, 2], "flag": True, "inf": None},
        "note": "text",
    }
    assert by_key["broken_meta"]["info"] == {"seen": 7} and "error" in by_key["broken_meta"]
    assert "info" not in by_key["plain"]  # nothing to say: no key
    assert ctx.meta == {}  # each detector gets a fresh dict
    text = an.kept_text(info)
    assert " text." in text  # the page shows the note of the detectors that have one


def test_a_failing_detector_is_skipped_and_reported(built, tmp_path):
    registry = DetectorRegistry()
    for detector in (Broken(), Fake("one"), Malformed()):
        registry.add(detector)
    log: list[str] = []
    store, ctx = run_stage(built, tmp_path, registry, log)
    rows = store.read_parquet("anomalies", "anomalies.parquet")
    assert rows["detector"].unique().to_list() == ["one"]  # the others' output is kept
    meta = {d["key"]: d for d in store.read_json("anomalies", "meta.json")["detectors"]}
    assert meta["broken"]["error"] == "ValueError: boom" and meta["broken"]["rows"] == 0
    assert "did not return" in meta["malformed"]["error"] and "error" not in meta["one"]
    assert any("'broken' failed and was skipped: ValueError: boom" in line for line in log)
    assert ctx.rows["detector_broken"] == 0 and ctx.rows["detector_one"] == 2
    hint = an.draw_anomalies(store, Filters(), [], "score", None)[2]
    assert "Failed and skipped: broken (ValueError: boom)" in hint


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
    assert rows["score"].is_between(0, 1).all() and rows["score"].median() < 0.9
    assert rows["score"].n_unique() > rows.height // 2  # the scores keep their spread
    assert rows["score"].to_list() == sorted(rows["score"].to_list(), reverse=True)
    assert (rows["raw"] >= DETECTORS.get("structural_pca").threshold).all()
    pca_rows = rows.filter(pl.col("detector") == "structural_pca")
    assert (
        set(pca_rows["kind"]) == {"service", "host"}
        and pca_rows["evidence"].str.contains("SPE").all()
    )
    # the shared data has no busy host, so the contextual detector runs but finds nothing
    assert set(rows["detector"]) == {
        "structural_pca",
        "structural_iforest",
        "volume",
        "novel_template",
        "rare_template",
        "sequence",
        "severity",
    }
    meta = {d["key"]: d for d in stage.read_json("anomalies", "meta.json")["detectors"]}
    assert set(meta) == {
        "structural_pca",
        "structural_iforest",
        "volume",
        "contextual",
        "novel_template",
        "rare_template",
        "sequence",
        "severity",
    }
    for key, entry in meta.items():
        assert entry["rows"] == rows.filter(pl.col("detector") == key).height
        assert entry["above_threshold"] >= entry["rows"]
    assert rows.group_by("kind", "entity").len()["len"].max() <= an.MAX_PER_ENTITY


# Injected anomalies of the synthetic data (``tests/synthetic.py``) per anomaly type. A case says
# which row of that type must exist and how high it must rank (0 is the highest score). A type
# that no detector emits yet is an expected failure (strict), not a skip: the case turns into an
# unexpected pass, and so a failure, as soon as issues 4, 7 and 8 register their detectors, and
# the mark then has to go. A registered detector therefore always has its case checked.
Rank = int | None  # position among the type's rows of the best matching row, None if none matches
Found = Callable[[GroundTruth, pl.DataFrame], Rank]
MINUTE = timedelta(minutes=1)
SECOND = timedelta(seconds=1)


def first_match(rows: pl.DataFrame, match: pl.Expr) -> Rank:
    """Position (rows are sorted by descending score) of the first row that satisfies ``match``."""
    hit = rows.with_row_index("rank").filter(match)
    return None if hit.is_empty() else int(hit["rank"][0])


def during(lo: datetime, hi: datetime) -> pl.Expr:
    return (pl.col("bin") >= ft.floor_window(naive(lo), 60)) & (pl.col("bin") <= naive(hi))


def spike_service(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    in_spike = during(truth.spike_start, truth.spike_end)
    return first_match(
        rows,
        in_spike
        & (pl.col("kind") == "service")
        & (pl.col("entity") == "neutron-openvswitch-agent"),
    )


def novel_template_window(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    """haproxy (where the novel template appears) within ten minutes of its first line."""
    return first_match(
        rows,
        during(truth.novel_first_seen, truth.novel_first_seen + 10 * MINUTE)
        & (pl.col("kind") == "service")
        & (pl.col("entity") == "haproxy"),
    )


def spike_host(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    return first_match(
        rows, during(truth.spike_start, truth.spike_end) & (pl.col("entity") == truth.spike_host)
    )


def silent_host_gap(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    return first_match(
        rows, during(truth.gap_start, truth.gap_end) & (pl.col("entity") == truth.silent_host)
    )


def context_host_window(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    return first_match(
        rows,
        during(truth.context_start, truth.context_end) & (pl.col("entity") == truth.context_host),
    )


def novel_first_seen(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    """The novel template's own row: haproxy at its first minute."""
    return first_match(
        rows,
        during(truth.novel_first_seen, truth.novel_first_seen)
        & (pl.col("entity") == "haproxy")
        & pl.col("evidence").str.contains("novel template"),
    )


def rare_window(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    return first_match(
        rows,
        during(truth.rare_first_seen, truth.rare_first_seen + 10 * SECOND)
        & (pl.col("entity") == "libvirt")
        & pl.col("evidence").str.contains("rare template"),
    )


def error_burst(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    return first_match(
        rows,
        during(truth.error_start, truth.error_end)
        & (pl.col("kind") == "service")
        & (pl.col("entity") == truth.error_service)
        & pl.col("evidence").str.starts_with("ERROR burst"),
    )


def out_of_order_requests(truth: GroundTruth, rows: pl.DataFrame) -> Rank:
    ranks = [first_match(rows, pl.col("entity") == r) for r in truth.out_of_order_request_ids]
    return None if None in ranks else max(r for r in ranks if r is not None)


INJECTED = [
    pytest.param("volume", spike_host, 20, id="volume spike"),
    pytest.param("volume", silent_host_gap, 20, id="silent host gap"),
    pytest.param("structural", spike_service, 10, id="spike seen structurally"),
    pytest.param("structural", novel_template_window, 15, id="novel template seen structurally"),
    pytest.param("novel", novel_first_seen, 10, id="novel template"),
    pytest.param("novel", rare_window, 15, id="rare template"),
    pytest.param("sequence", out_of_order_requests, 20, id="out-of-order request"),
    pytest.param("severity", error_burst, 3, id="error burst"),
]
# Types whose injection changes the structural fits of the shared data (a busy host), so their
# case lives in tests/unit/test_contextual.py on a dataset of its own.
OWN_DATASET = {"contextual"}


@pytest.mark.parametrize(("kind", "found", "max_rank"), INJECTED)
def test_injected_anomaly_is_found_with_its_type_and_rank(stage, synthetic, kind, found, max_rank):
    assert kind in DETECTORS.types(), f"no registered detector of type {kind!r}"
    rows = an.load_anomalies(stage).filter(pl.col("type") == kind)
    rank = found(synthetic.truth, rows)
    assert rank is not None, f"not found among {rows.height} {kind} rows"
    assert rank < max_rank, f"found at rank {rank}, expected within the top {max_rank}"


# The structural type has two detectors, each of which must find its injections on its own.
STRUCTURAL = [
    pytest.param("structural_pca", spike_service, 10, id="pca spike"),
    pytest.param("structural_pca", novel_template_window, 15, id="pca novel template"),
    pytest.param("structural_iforest", spike_service, 15, id="iforest spike"),
    pytest.param("structural_iforest", novel_template_window, 15, id="iforest novel template"),
    pytest.param("structural_iforest", spike_host, 15, id="iforest host spike"),
]


@pytest.mark.parametrize(("key", "found", "max_rank"), STRUCTURAL)
def test_each_structural_detector_finds_the_injections(stage, synthetic, key, found, max_rank):
    rows = an.load_anomalies(stage).filter(pl.col("detector") == key)
    rank = found(synthetic.truth, rows)
    assert rank is not None, f"not found among {rows.height} {key} rows"
    assert rank < max_rank, f"found at rank {rank}, expected within the top {max_rank}"


def test_novel_and_rare_rows_do_not_overlap(stage, synthetic):
    truth = synthetic.truth
    rows = an.load_anomalies(stage).filter(pl.col("type") == "novel")
    novel = rows.filter(pl.col("detector") == "novel_template")
    rare = rows.filter(pl.col("detector") == "rare_template")
    assert novel["template_id"].is_not_null().all() and rare["template_id"].is_not_null().all()
    assert novel["evidence"].str.contains(truth.novel_template[:20].replace("{", "")).any()
    assert not rare["evidence"].str.contains("bgp").any()  # 20 events: not rare
    assert not novel["evidence"].str.contains("Watchdog").any()  # mid-run: not novel
    cutoff = truth.end - (truth.end - truth.start) * Settings().novel_fraction
    assert (novel["bin"] >= ft.floor_window(naive(cutoff), 60)).all()
    # no (template, minute, service) is both a novel and a rare row (exact keys)
    keys = ["template_id", "bin", "entity"]
    assert novel.join(rare, on=keys, how="inner").is_empty()
    assert rare["raw"].min() >= 1.0


def test_sequence_rows_are_the_out_of_order_request_only(stage, synthetic):
    truth = synthetic.truth
    rows = an.load_anomalies(stage).filter(pl.col("type") == "sequence")
    assert sorted(rows["entity"]) == sorted(truth.out_of_order_request_ids)
    assert (rows["kind"] == "request").all() and (rows["template_id"].is_not_null()).all()
    # the 5 swapped requests are a variant seen in 3 % of the requests: a first-order chain
    # cannot call that anomalous at alpha 0.01 (a known limit, see the README)
    assert not set(truth.swapped_request_ids) & set(rows["entity"])
    meta = {d["key"]: d for d in stage.read_json("anomalies", "meta.json")["detectors"]}
    assert "0 longer requests" in meta["sequence"]["info"]["note"]


def test_severity_rows_are_the_error_burst_and_the_spike_warnings_only(stage, synthetic):
    truth = synthetic.truth
    rows = an.load_anomalies(stage).filter(pl.col("type") == "severity")
    assert (rows["kind"] == "service").all() and rows["raw"].is_finite().all()
    error = rows.filter(pl.col("evidence").str.starts_with("ERROR"))
    assert error["entity"].to_list() == [truth.error_service]  # baseline ERROR (1 %): no row
    assert error.filter(during(truth.error_start, truth.error_end)).height == 1
    # the volume spike of the synthetic data is also a surge of WARNING lines (the one level
    # the data has in bulk); nothing else: the steady WARNING rate (about 5/min) is no burst
    warning = rows.filter(pl.col("evidence").str.starts_with("WARNING"))
    assert warning["entity"].to_list() == ["neutron-openvswitch-agent"]
    assert warning.filter(during(truth.spike_start, truth.spike_end)).height == 1
    assert rows.height == 2
    assert error["evidence"][0].count("z=") == 1 and "Poisson" in error["evidence"][0]


def test_every_registered_type_has_an_expectation():
    expected = {case.values[0] for case in INJECTED} | OWN_DATASET
    assert set(DETECTORS.types()) <= expected


# ------------------------------------------------------------------ page


def test_select_by_range_type_and_entity(stage):
    rows = an.load_anomalies(stage)
    some = rows.row(0, named=True)
    start = some["bin"]
    window = Filters(start=stamp(start), end=stamp(start + timedelta(minutes=1)))
    inside = an.select(rows, window, [])
    assert 0 < inside.height < rows.height and (inside["bin"] == start).all()
    assert an.select(rows, Filters(), ["structural"]).height == (rows["type"] == "structural").sum()
    assert an.select(rows, Filters(), ["nonexistent"]).is_empty()
    by_service = an.select(rows, Filters(services=("haproxy",)), [])
    assert set(by_service.filter(pl.col("kind") == "service")["entity"]) == {"haproxy"}
    assert (by_service["kind"] == "host").any()  # the service filter leaves host rows alone
    by_host = an.select(rows, Filters(hosts=("nope",)), [])
    assert (by_host["kind"] != "host").all()  # fleet rows are not restricted by a host filter


def stamp(value):
    return an.stamp(value)


def test_sort_rows(stage):
    rows = an.load_anomalies(stage).head(500)
    assert an.sort_rows(rows, "time")["bin"].is_sorted()
    assert an.sort_rows(rows, "type")["type"].is_sorted()
    assert an.sort_rows(rows, "entity")["entity"].is_sorted()
    assert an.sort_rows(rows, "score")["score"].is_sorted(descending=True)
    assert an.sort_rows(rows, "overall")["overall"].is_sorted(descending=True)


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
    assert "service=" not in an.explorer_url(start, 60, "unknown", "x", None)
    table, hrefs = an.table_rows(an.load_anomalies(stage).head(5))
    assert table.height == 5 == len(hrefs) and table.columns[:4] == [
        "window",
        "type",
        "overall",
        "score",
    ]


def test_request_links_do_not_restrict_the_time_range():
    start = datetime(2023, 5, 21, 1, 2, 0)
    query = dict(parse_qsl(urlsplit(an.explorer_url(start, 60, "request", "r-1", None)).query))
    assert query == {"request_id": "r-1"}  # a request's lines may lie outside the window
    with_template = an.explorer_url(start, 60, "request", "r-1", 4)
    assert dict(parse_qsl(urlsplit(with_template).query)) == {"request_id": "r-1", "template": "4"}


def test_select_keeps_windows_that_overlap_the_range():
    rows = pl.DataFrame(
        {
            "bin": [datetime(2023, 5, 21, 1, 0), datetime(2023, 5, 21, 1, 10)],
            "window_s": [900, 60],
            "kind": ["host", "host"],
            "entity": ["a", "b"],
            "type": ["t", "t"],
        }
    )
    window = Filters(start="2023-05-21 01:10:00", end="2023-05-21 01:11:00")
    assert an.select(rows, window, [])["entity"].to_list() == ["a", "b"]  # a ends at 01:15
    assert an.select(rows, Filters(start="2023-05-21 01:15:00"), [])["entity"].to_list() == []


def test_the_table_sorts_the_whole_selection_not_its_top_rows(stage, monkeypatch):
    monkeypatch.setattr(an, "TABLE_ROWS", 5)
    rows = an.load_anomalies(stage)
    assert rows.height > 5
    earliest = rows["bin"].min()
    _, table, _ = an.draw_anomalies(stage, Filters(), [], "time", None)
    first = an.stamp(earliest)
    assert first in str(table)  # the earliest anomaly is shown, wherever it ranks by score


def test_the_table_shows_min_of_the_default_rows_and_the_selection(stage):
    rows = an.load_anomalies(stage)
    _, table, _ = an.draw_anomalies(stage, Filters(), [], "score", None)
    assert str(table).count("/explorer") == min(an.TABLE_ROWS, rows.height)


def test_timeline_samples_every_type(monkeypatch):
    def part(kind, n, base_score):
        return pl.DataFrame(
            {
                "bin": [datetime(2023, 5, 21, 1, i % 60) for i in range(n)],
                "kind": ["host"] * n,
                "entity": ["h"] * n,
                "detector": [kind] * n,
                "type": [kind] * n,
                "score": [base_score - i * 1e-4 for i in range(n)],
                "evidence": ["why"] * n,
            }
        )

    rows = pl.concat([part("big", 50, 1.0), part("small", 5, 0.2)]).sort("score", descending=True)
    monkeypatch.setattr(an, "MAX_POINTS", 20)
    fig = an.timeline_figure(rows, ["big", "small", "unused"], None)
    counts = {t.name: len(t.x) for t in fig.data}
    assert counts == {"big": 10, "small": 5}  # the low-scoring type is not crowded out
    assert "15 of 55" in fig.layout.annotations[0].text


def test_hints_name_the_kept_counts_and_the_filters_that_do_not_apply():
    meta = [
        {"key": "a", "type": "t", "rows": 3, "above_threshold": 10},
        {"key": "b", "type": "t", "rows": 1, "above_threshold": 1},
        {"key": "c", "type": "u", "error": "ValueError: boom"},
    ]
    text = an.hint(Filters(services=("s",), levels=("ERROR",)), 4, "time", meta)
    assert "first 100 by time" in text and "Kept 4 of 11 t; 0 of 0 u" in text
    assert "services filter applies to service rows only" in text and "level filter" in text
    assert "Failed and skipped: c (ValueError: boom)" in text


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
    structural = rows.filter(pl.col("type") == "structural")
    assert len(fig.data) == 1 and len(fig.data[0].x) == min(structural.height, an.MAX_POINTS)
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
    nothing = an.draw_anomalies(stage, Filters(), ["nonexistent"], "score", None)
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
    assert an.sort_value("time") == "time" and an.sort_value("score") == "score"
    assert an.sort_value("x") == "overall" and an.sort_value(None) == "overall"
    assert an.type_values(None) == [] and an.type_values(["a", 3]) == ["a"]  # type: ignore[list-item]


def test_layout_and_callback_over_http(stage, built):
    from logdash.app.main import create_app

    assert "anomalies-timeline" in str(Anomalies().layout(stage))
    app = create_app(
        stage, build_with(built, Templates(), Features(), Pca(), Temporal(), Hosts(), Anomalies())
    )
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


def test_volume_rows_say_spike_or_gap_and_gaps_start_at_the_threshold(stage, synthetic):
    truth = synthetic.truth
    volume = an.load_anomalies(stage).filter(pl.col("detector") == "volume")
    detector = DETECTORS.get("volume")
    assert (volume["raw"] >= detector.threshold).all() and volume["window_s"].min() >= 60
    spike = volume.filter(
        during(truth.spike_start, truth.spike_end) & (pl.col("entity") == truth.spike_host)
    )
    assert spike.height and spike["evidence"].str.starts_with("spike: peak").all()
    gap = volume.filter(
        during(truth.gap_start, truth.gap_end) & (pl.col("entity") == truth.silent_host)
    )
    assert gap.height and (gap["kind"] == "host").all()
    assert gap["evidence"].str.starts_with("silent for").any()
    assert set(volume["kind"]) <= {"service", "host"}
    assert (
        not volume.filter(pl.col("kind") == "service")["evidence"].str.starts_with("silent").any()
    )
