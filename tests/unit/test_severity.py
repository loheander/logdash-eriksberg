"""Severity detector on hand-made rate tables: Poisson tail, floors, periodic merge, levels."""

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest
from scipy import stats

from logdash.analyses import temporal
from logdash.analyses.temporal import find_bursts
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors import base, severity
from logdash.detectors.base import DetectContext
from logdash.detectors.severity import Severity, poisson_z

T0 = datetime(2023, 5, 21)
MINUTES = 600


def detect(tmp_path, series: dict[tuple[str, str], np.ndarray], **settings) -> pl.DataFrame:
    """Rows for ``{(program, level): counts per minute}``."""
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path, **settings))
    frames = [
        pl.DataFrame(
            {
                "bin": [T0 + timedelta(minutes=int(i)) for i in np.flatnonzero(x)],
                "level": level,
                "program": program,
                "n": x[x > 0].astype(int),
            }
        )
        for (program, level), x in series.items()
    ]
    frames.append(info(max(len(x) for x in series.values())))
    store.write_parquet(temporal.KEY, "rate_60.parquet", pl.concat(frames))
    return Severity().detect(DetectContext(store, store.settings, lambda _: None))


def info(minutes: int) -> pl.DataFrame:
    """An INFO line in the first and last minute: the table spans the whole day, as the real one."""
    bins = [T0, T0 + timedelta(minutes=minutes - 1)]
    return pl.DataFrame(
        {"bin": bins, "level": "INFO", "program": "other", "n": [1, 1]},
        schema={"bin": pl.Datetime("us"), "level": pl.String, "program": pl.String, "n": pl.Int64},
    )


def poisson(seed: int, rate: float) -> np.ndarray:
    return np.random.default_rng(seed).poisson(rate, MINUTES).astype(float)


def test_error_burst_on_a_zero_baseline_scores_the_poisson_tail(tmp_path):
    x = np.zeros(MINUTES)
    x[300:303] = [8, 20, 8]  # 36 errors in three minutes, nothing else all day
    rows = detect(tmp_path, {("api", "ERROR"): x})
    assert rows.height == 1
    row = rows.row(0, named=True)
    z, p = poisson_z(20, severity.MIN_RATE)
    assert row["raw"] == pytest.approx(z) and z != 20
    assert row["bin"] == T0 + timedelta(minutes=300) and row["window_s"] == 180
    assert row["evidence"].startswith("ERROR burst: peak 20/min vs baseline 0/min (Poisson P=")
    assert f"z={z:.1f}" in row["evidence"] and f"P={p:.1e}" in row["evidence"]
    assert row["kind"] == "service" and row["entity"] == "api"


def test_poisson_z_matches_the_tail_and_survives_underflow():
    z, p = poisson_z(10, 2.0)
    assert p == pytest.approx(stats.poisson.sf(9, 2.0)) and z == pytest.approx(stats.norm.isf(p))
    huge, tiny = poisson_z(40_000, 4_000.0)  # scipy's logsf is -inf here
    assert np.isfinite(huge) and huge > 300 and tiny == 0.0
    assert poisson_z(5, 5.0)[0] < poisson_z(8, 5.0)[0] < poisson_z(20, 5.0)[0]


def test_a_busy_baseline_keeps_the_robust_z(tmp_path):
    x = poisson(1, 50)
    x[300:303] = 400
    rows = detect(tmp_path, {("api", "WARNING"): x})
    assert rows.height == 1
    assert "robust z=" in rows["evidence"][0] and "Poisson" not in rows["evidence"][0]
    found = find_bursts(x, 6.0)
    assert rows["raw"][0] == pytest.approx(found[0]["score"])
    assert f"robust z={found[0]['score']:.1f}" in rows["evidence"][0]


def test_level_floors_let_a_small_error_burst_through_but_not_warnings(tmp_path):
    x = np.zeros(MINUTES)
    x[200:203] = [6, 12, 6]  # 24 lines
    assert detect(tmp_path, {("api", "WARNING"): x}).is_empty()  # needs 10 / 300
    big = np.zeros(MINUTES)
    big[200:203] = [10, 15, 10]  # 35 lines
    assert detect(tmp_path, {("api", "ERROR"): big}).height == 1  # needs 5 / 30
    assert detect(tmp_path, {("api", "ERROR"): x}).is_empty()  # 24 lines: below the 30 floor
    stray = np.zeros(MINUTES)
    stray[400] = 4  # a single stray minute
    assert detect(tmp_path, {("api", "ERROR"): stray}).is_empty()


def test_error_and_critical_are_one_series_and_unknown_is_ignored(tmp_path):
    err, crit = np.zeros(MINUTES), np.zeros(MINUTES)
    err[100:102], crit[100:102] = 10, 10
    rows = detect(tmp_path, {("api", "ERROR"): err, ("api", "CRITICAL"): crit})
    assert rows.height == 1 and rows["evidence"][0].startswith("ERROR burst: peak 20/min")
    unknown = np.zeros(MINUTES)
    unknown[100:103] = 5000
    assert detect(tmp_path, {("api", "UNKNOWN"): unknown, ("api", "INFO"): unknown}).is_empty()


def test_warning_and_error_in_the_same_minute_are_two_rows(tmp_path):
    warn, err = poisson(2, 20), np.zeros(MINUTES)
    warn[300:303] = 500
    err[300:303] = 20
    rows = detect(tmp_path, {("api", "WARNING"): warn, ("api", "ERROR"): err})
    assert sorted(e.split(" burst")[0] for e in rows["evidence"]) == ["ERROR", "WARNING"]
    assert rows["bin"].n_unique() == 1


def bursts_every(period: int, n: int, offsets=None) -> np.ndarray:
    x = np.zeros(2000)
    starts = [100 + i * period for i in range(n)] if offsets is None else offsets
    for s in starts:
        x[s : s + 3] = 200
    return x


def test_periodic_warning_bursts_are_one_demoted_row(tmp_path):
    rows = detect(tmp_path, {("agent", "WARNING"): bursts_every(40, 9)})
    assert rows.height == 1
    assert rows["raw"][0] == 6.0  # the threshold: visible, not ranked among real bursts
    text = rows["evidence"][0]
    assert "9 episodes in all, repeating about every 40 min" in text and "z=" in text
    assert "ranked at the threshold" in text


def test_the_same_bursts_at_irregular_spacing_stay_separate(tmp_path):
    offsets = [100, 155, 260, 301, 470, 640, 653, 900, 1180]
    rows = detect(tmp_path, {("agent", "WARNING"): bursts_every(0, 0, offsets)})
    assert rows.height == 9 and (rows["raw"] > 6.0).all()


def test_a_constant_high_rate_is_no_burst(tmp_path):
    rows = detect(tmp_path, {("api", "WARNING"): np.full(MINUTES, 900.0)})
    assert rows.is_empty()
    assert detect(tmp_path, {("api", "ERROR"): poisson(3, 2)}).is_empty()  # steady errors


def test_empty_and_unlevelled_data_give_an_empty_frame(tmp_path):
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path))
    empty = pl.DataFrame(
        schema={"bin": pl.Datetime("us"), "level": pl.String, "program": pl.String, "n": pl.Int64}
    )
    store.write_parquet(temporal.KEY, "rate_60.parquet", empty)
    rows = Severity().detect(DetectContext(store, store.settings, lambda _: None))
    assert rows.is_empty() and set(base.REQUIRED) == set(rows.columns)
    only_info = detect(tmp_path / "b", {("api", "INFO"): poisson(4, 100)})
    assert only_info.is_empty() and set(base.REQUIRED) == set(only_info.columns)


def test_min_excess_default_is_unchanged_for_find_bursts():
    x = np.zeros(MINUTES)
    x[100:103] = [8, 20, 8]  # excess 36
    assert find_bursts(x, 6.0) == []  # the volume floor of 300
    assert len(find_bursts(x, 6.0, 5, 30)) == 1
    assert find_bursts(x, 6.0) == find_bursts(x, 6.0, temporal.MIN_BURST, temporal.MIN_EXCESS)


def test_version_embeds_the_tunable_constants_and_the_threshold_follows_burst_k():
    version = Severity().version
    for part in (severity.POISSON_BELOW, severity.MIN_RATE, temporal.MIN_EXCESS):
        assert str(part) in version
    assert "ERROR" in version and "30" in version
    assert Severity().threshold_for(Settings(burst_k=9.0)) == 9.0


def test_missing_program_is_not_a_service(tmp_path):
    x = np.zeros(MINUTES)
    x[100:103] = 30
    assert detect(tmp_path, {(temporal.MISSING, "ERROR"): x}).is_empty()


def test_meta_counts_concurrent_services(tmp_path):
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path))
    frames = [
        pl.DataFrame(
            {
                "bin": [T0 + timedelta(minutes=i) for i in (100, 101, 102)],
                "level": "ERROR",
                "program": name,
                "n": [30, 30, 30],
            }
        )
        for name in ("a", "b", "c")
    ] + [info(MINUTES)]
    store.write_parquet(temporal.KEY, "rate_60.parquet", pl.concat(frames))
    ctx = DetectContext(store, store.settings, lambda _: None)
    rows = Severity().detect(ctx)
    assert rows.height == 3 and ctx.meta["max_services_starting_in_one_minute"] == 3
