"""Novel and rare template detectors on hand-made template tables."""

import math
from datetime import datetime, timedelta

import polars as pl

from logdash.analyses import templates as tpl
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors.base import DetectContext
from logdash.detectors.novel import SCHEMA, NovelTemplate, RareTemplate, novel_rows, rare_rows

LO = datetime(2023, 5, 21, 0, 0)
HI = LO + timedelta(minutes=1000)  # cutoff at 0.1: minute 900
NO_NOVEL = pl.DataFrame(schema=SCHEMA)


def templates(*specs: tuple[int, int, int]) -> pl.DataFrame:
    """(template_id, count, first minute) -> the template table."""
    return pl.DataFrame(
        {
            "template_id": [s[0] for s in specs],
            "template": [f"text {s[0]}" for s in specs],
            "count": [s[1] for s in specs],
            "first_seen": [LO + timedelta(minutes=s[2]) for s in specs],
        },
        schema={
            "template_id": pl.Int64,
            "template": pl.String,
            "count": pl.Int64,
            "first_seen": pl.Datetime("us"),
        },
    )


def counts(*specs: tuple[int, int, str, int]) -> pl.DataFrame:
    """(template_id, minute, program, n) -> the per-minute counts."""
    return pl.DataFrame(
        {
            "template_id": [s[0] for s in specs],
            "bin": [LO + timedelta(minutes=s[1]) for s in specs],
            "program": [s[2] for s in specs],
            "n": [s[3] for s in specs],
        },
        schema={
            "template_id": pl.Int64,
            "bin": pl.Datetime("us"),
            "program": pl.String,
            "n": pl.Int64,
        },
    )


def test_novel_gate_is_strict_at_the_cutoff():
    t = templates((1, 3, 900), (2, 3, 901), (3, 3, 100))
    c = counts((1, 900, "a", 3), (2, 901, "a", 3), (3, 100, "a", 3))
    rows = novel_rows(t, c, LO, HI, 0.1)
    assert rows["template_id"].to_list() == [2]  # exactly at the cutoff is out
    assert novel_rows(t, c, LO, HI, 0.2)["template_id"].to_list() == [1, 2]  # sorted by time


def test_novel_raw_counts_runs_not_minutes_or_events():
    t = templates((1, 11, 950), (2, 11, 950), (3, 11, 910))
    c = counts(
        (1, 950, "a", 11),
        *[(2, 950 + i, "a", 1) for i in range(11)],  # one contiguous burst of 11 minutes
        *[(3, 910 + 12 * i, "a", 1) for i in range(4)],  # 4 minutes, gaps over the cluster limit
    )
    rows = novel_rows(t, c, LO, HI, 0.1).sort("template_id")
    one = -math.log2(1 - 0.95)
    assert abs(rows["raw"][0] - one) < 1e-9 and abs(rows["raw"][1] - one) < 1e-9
    assert abs(rows["raw"][2] - 4 * -math.log2(1 - 0.91)) < 1e-9
    assert "11 events in 1 min (1 run)" in rows["evidence"][0]
    assert "11 events in 11 min (1 run)" in rows["evidence"][1]
    assert "4 runs" in rows["evidence"][2]
    assert "1 other new templates" in rows["evidence"][0]


def test_contiguous_burst_scores_below_the_same_minutes_spread_out():
    t = templates((1, 4, 940), (2, 4, 940))
    c = counts(
        *[(1, 940 + i, "a", 1) for i in range(4)],
        *[(2, 940 + 11 * i, "a", 1) for i in range(4)],
    )
    rows = novel_rows(t, c, LO, HI, 0.1).sort("template_id")
    assert rows["raw"][0] < rows["raw"][1]
    assert "1 other new templates" in rows["evidence"][0]


def test_novel_threshold_is_the_gate_for_one_minute():
    detector = NovelTemplate()
    assert abs(detector.threshold_for(Settings(novel_fraction=0.1)) - math.log2(10)) < 1e-12
    t = templates((1, 3, 901))
    raw = novel_rows(t, counts((1, 901, "a", 3)), LO, HI, 0.1)["raw"][0]
    assert raw >= detector.threshold  # first seen after the cutoff, one minute: above
    late = templates((1, 3, 1000))  # u = 1: clipped, finite
    assert math.isfinite(novel_rows(late, counts((1, 1000, "a", 3)), LO, HI, 0.1)["raw"][0])


def test_novel_entity_is_the_busiest_service_of_the_first_minute():
    t = templates((1, 9, 950))
    c = counts((1, 950, "b", 2), (1, 950, "a", 5), (1, 951, "c", 2))
    assert novel_rows(t, c, LO, HI, 0.1)["entity"].to_list() == ["a"]


def test_empty_and_unmatched_inputs():
    empty = templates()
    assert novel_rows(empty, counts(), LO, HI, 0.1).height == 0
    assert rare_rows(empty, counts(), 5, novel_rows(empty, counts(), LO, HI, 0.1)).height == 0
    t = templates((1, 3, 950))
    assert novel_rows(t, counts(), LO, HI, 0.1).height == 0  # no counts rows: skipped
    assert rare_rows(t, counts(), 5, NO_NOVEL).height == 0
    assert novel_rows(t, counts((1, 950, "a", 3)), LO, LO, 0.1).height == 0  # zero span


def test_rare_boundary_and_raw():
    t = templates((1, 5, 10), (2, 6, 10), (3, 1, 20))
    c = counts((1, 10, "a", 5), (2, 10, "a", 6), (3, 20, "b", 1))
    nothing = NO_NOVEL
    rows = rare_rows(t, c, 5, nothing).sort("template_id")
    assert rows["template_id"].to_list() == [1, 3]  # count == threshold in, threshold + 1 out
    assert rows["raw"].to_list() == [1.1, 5.5]
    assert rows["kind"].unique().to_list() == ["service"]
    assert rare_rows(t, c, 6, nothing)["template_id"].to_list() == [1, 2, 3]
    assert rare_rows(t, c, 0, nothing).height == 0


def test_rare_leaves_out_the_first_window_of_a_novel_template():
    t = templates((1, 2, 950))
    c = counts((1, 950, "a", 1), (1, 960, "a", 1))
    novel = novel_rows(t, c, LO, HI, 0.1)
    assert novel.height == 1
    rows = rare_rows(t, c, 5, novel)
    assert rows["bin"].to_list() == [LO + timedelta(minutes=960)]
    assert rare_rows(t, c, 5, NO_NOVEL).height == 2  # not novel under another fraction


def test_rare_drop_covers_only_the_service_of_the_novel_row():
    t = templates((1, 4, 950))
    c = counts((1, 950, "a", 2), (1, 950, "b", 1), (1, 960, "a", 1))
    novel = novel_rows(t, c, LO, HI, 0.1)
    assert novel["entity"].to_list() == ["a"]
    rows = rare_rows(t, c, 5, novel).sort("bin", "entity")
    assert [(r["bin"], r["entity"]) for r in rows.iter_rows(named=True)] == [
        (LO + timedelta(minutes=950), "b"),
        (LO + timedelta(minutes=960), "a"),
    ]


def test_detect_reads_the_cache_and_settings_change_the_rows(tmp_path):
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path))
    t = templates((1, 3, 950), (2, 8, 500), (3, 9, 940))
    c = counts((1, 950, "a", 3), (2, 500, "a", 8), (3, 940, "b", 9))
    store.write_parquet(tpl.KEY, "templates.parquet", t)
    store.write_parquet(tpl.KEY, "template_counts.parquet", c)
    store.write_json(tpl.KEY, "meta.json", {"ts_min": str(LO), "ts_max": str(HI)})

    def run(detector, **overrides):
        settings = Settings(cache_dir=tmp_path, **overrides)
        return detector.detect(DetectContext(store, settings, lambda _: None))

    assert sorted(run(NovelTemplate())["template_id"].to_list()) == [1, 3]
    assert run(NovelTemplate(), novel_fraction=0.06)["template_id"].to_list() == [1]
    assert run(RareTemplate())["template_id"].to_list() == []  # 3 and 8 and 9 > 5 events
    assert run(RareTemplate(), rare_threshold=8)["template_id"].to_list() == [2]
    both = run(RareTemplate(), rare_threshold=9)
    assert both["template_id"].to_list() == [2]  # 1 (novel first window) and 3 are novel
    assert RareTemplate.threshold == 1.0
