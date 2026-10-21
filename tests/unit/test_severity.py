"""Severity detector on hand-made rate tables: Poisson tail, floors, periodic merge, levels."""

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest
from scipy import special, stats

from logdash.analyses import temporal
from logdash.analyses.temporal import find_bursts
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors import base, severity
from logdash.detectors.base import DetectContext
from logdash.detectors.severity import Severity, p_text, tail_z

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
    z, log_p = tail_z(20, severity.MIN_RATE)
    assert row["raw"] == pytest.approx(z) and z != 20
    assert row["bin"] == T0 + timedelta(minutes=300) and row["window_s"] == 180
    assert row["evidence"].startswith("ERROR burst: peak 20/min vs baseline 0/min (Poisson P=")
    assert f"z={z:.1f}" in row["evidence"] and f"P{p_text(log_p)}" in row["evidence"]
    assert row["kind"] == "service" and row["entity"] == "api"


def exact_log_tail(dist, peak: int) -> float:
    """log P(X >= peak) as a logsumexp of the pmf over the tail (far beyond where it matters)."""
    k = np.arange(peak, peak + 20_000)
    return float(special.logsumexp(dist.logpmf(k)))


def test_tail_z_matches_the_tail_and_survives_underflow():
    z, log_p = tail_z(10, 2.0)
    p = stats.poisson.sf(9, 2.0)
    assert np.exp(log_p) == pytest.approx(p) and z == pytest.approx(stats.norm.isf(p))
    huge, log_tiny = tail_z(40_000, 4_000.0)  # scipy's logsf is -inf here
    assert np.isfinite(huge) and huge > 300 and np.exp(log_tiny) == 0.0
    assert tail_z(5, 5.0)[0] < tail_z(8, 5.0)[0] < tail_z(20, 5.0)[0]


@pytest.mark.parametrize(
    ("peak", "rate", "variance"),
    [(188, 0.56, None), (442, 11.0, None), (1500, 300.0, None), (5000, 1.0, None)]
    + [(442, 11.0, 60.0), (3000, 50.0, 400.0), (900, 5.0, 30.0)],
)
def test_the_first_term_bound_matches_an_exact_logsumexp_tail(peak, rate, variance):
    z, log_p = tail_z(peak, rate, variance)
    if variance is None:
        dist = stats.poisson(rate)
    else:
        size = rate * rate / (variance - rate)
        dist = stats.nbinom(size, size / (size + rate))
    exact = exact_log_tail(dist, peak)
    assert log_p == pytest.approx(exact, abs=0.05)
    assert z == pytest.approx(-float(special.ndtri_exp(exact)), abs=0.05)


def test_a_tiny_p_is_printed_as_a_power_of_ten():
    assert p_text(np.log(3.2e-19)) == "=3.2e-19"
    assert p_text(-950.0) == "≈1e-413"


def test_a_busy_baseline_is_scored_by_the_over_dispersed_tail(tmp_path):
    x = poisson(1, 50)
    x[300:303] = 400
    rows = detect(tmp_path, {("api", "WARNING"): x})
    assert rows.height == 1
    text = rows["evidence"][0]
    assert "negative binomial P=" in text and "robust" not in text
    assert f"z={rows['raw'][0]:.1f}" in text and rows["raw"][0] > 6.0


def test_a_larger_excess_never_ranks_below_a_smaller_one_across_the_old_median_10_switch(tmp_path):
    """One scale: the z of a burst grows with its peak at every baseline from 8 to 12 per min."""
    for baseline in (8, 9.9, 11, 12):
        previous = 0.0
        for peak in (150, 300, 600, 1200, 2400):
            x = poisson(5, baseline)
            x[300:303] = peak
            raw = detect(tmp_path / f"{baseline}-{peak}", {("api", "WARNING"): x})["raw"][0]
            assert raw > previous
            previous = raw
    # and across baselines: the larger excess over a similar baseline ranks higher
    small = poisson(5, 9.0)
    small[300:303] = 200
    big = poisson(5, 11.0)
    big[300:303] = 800
    a = detect(tmp_path / "a", {("api", "WARNING"): small})["raw"][0]
    b = detect(tmp_path / "b", {("api", "WARNING"): big})["raw"][0]
    assert b > a


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


def test_irregular_bursts_stay_separate_unless_they_are_close(tmp_path):
    offsets = [100, 380, 790, 1010, 1420, 1650, 1960]
    rows = detect(tmp_path, {("agent", "WARNING"): bursts_every(0, 0, offsets)})
    assert rows.height == 7 and (rows["raw"] > 6.0).all()


def test_nearby_episodes_of_a_service_are_one_incident_scored_without_itself(tmp_path):
    x = poisson(6, 2)
    for start, peak in ((200, 150), (230, 120), (262, 120), (290, 150)):  # gaps of 25 to 29 min
        x[start : start + 3] = peak
    x[480:483] = 150  # 190 minutes later: its own incident
    rows = detect(tmp_path, {("barbican", "WARNING"): x}).sort("bin")
    assert rows.height == 2
    first = rows.row(0, named=True)
    assert first["bin"] == T0 + timedelta(minutes=200) and first["window_s"] == 93 * 60
    assert "in 4 bursts over 93 min (03:20 to 04:52)" in first["evidence"]
    assert "burst" in rows["evidence"][1] and "bursts" not in rows["evidence"][1]


def test_the_incident_is_interpolated_out_of_its_own_baseline():
    x = np.full(300, 2.0)
    x[100:200] = 500  # an incident that fills most of the rolling window
    spans = [{"first": 100, "last": 199}]
    assert temporal.rolling_baseline(x)[0][150] == 500  # scored against itself: no burst at all
    assert (severity.without(x, spans) == 2.0).all()
    assert severity.without(x, []) is x


def test_periodic_bursts_closer_than_the_merge_gap_are_still_one_periodic_row(tmp_path):
    rows = detect(tmp_path, {("agent", "WARNING"): bursts_every(30, 12)})
    assert (
        rows.height == 1
        and "12 episodes in all, repeating about every 30 min" in rows["evidence"][0]
    )


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
    for part in (severity.MIN_RATE, severity.MERGE_GAP, temporal.MIN_EXCESS):
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


def test_meta_counts_distinct_services_not_rows(tmp_path):
    warn, err = np.zeros(MINUTES), np.zeros(MINUTES)
    warn[300:303], err[300:303] = 500, 40  # one service, a WARNING and an ERROR burst, same minute
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path))
    frame = pl.concat(
        [
            pl.DataFrame(
                {
                    "bin": [T0 + timedelta(minutes=i) for i in (300, 301, 302)],
                    "level": lv,
                    "program": "api",
                    "n": [n, n, n],
                }
            )
            for lv, n in (("WARNING", 500), ("ERROR", 40))
        ]
        + [info(MINUTES)]
    )
    store.write_parquet(temporal.KEY, "rate_60.parquet", frame)
    ctx = DetectContext(store, store.settings, lambda _: None)
    assert Severity().detect(ctx).height == 2
    assert ctx.meta["max_services_starting_in_one_minute"] == 1
