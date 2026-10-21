"""Volume detector on hand-made rate tables: drops, bounded gaps, periodic repeats."""

from datetime import datetime, timedelta

import numpy as np
import polars as pl

from logdash.analyses import temporal
from logdash.analyses.temporal import find_drops
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors.base import DetectContext
from logdash.detectors.volume import Volume, merge_periodic, periodic_members

T0 = datetime(2023, 5, 21)
MINUTES = 600


def table(series: dict[str, np.ndarray], column: str) -> pl.DataFrame:
    frames = [
        pl.DataFrame(
            {
                "bin": [T0 + timedelta(minutes=int(i)) for i in np.flatnonzero(x)],
                column: name,
                "n": x[x > 0].astype(int),
            }
        )
        for name, x in series.items()
    ]
    return pl.concat(frames)


def detect(tmp_path, services: dict[str, np.ndarray], hosts: dict[str, np.ndarray]) -> pl.DataFrame:
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path))
    rate = table(services, "program").with_columns(
        pl.lit("INFO").alias("level"), pl.lit(1).alias("template_id")
    )
    store.write_parquet(temporal.KEY, "rate_60.parquet", rate)
    store.write_parquet(temporal.KEY, "host_60.parquet", table(hosts, "host"))
    return Volume().detect(DetectContext(store, store.settings, lambda _: None))


def busy(rng_seed: int = 1) -> np.ndarray:
    return np.random.default_rng(rng_seed).poisson(1000, MINUTES).astype(float)


def test_partial_drop_of_a_host_and_a_busy_service_are_reported(tmp_path):
    host, service = busy(1), busy(2)
    host[300:360] = 20
    service[200:240] = 0  # a busy service going silent is a drop
    rows = detect(tmp_path, {"api": service, "quiet": np.full(MINUTES, 3.0)}, {"h1": host})
    drop = rows.filter(pl.col("entity") == "h1")
    assert drop.height == 1 and drop["kind"][0] == "host"
    assert drop["evidence"][0].startswith("drop: trough 20 events/min vs baseline 10")
    assert 300 <= (drop["bin"][0] - T0).total_seconds() / 60 <= 330
    assert drop["raw"][0] > 6
    silent = rows.filter(pl.col("entity") == "api")
    assert silent.height == 1 and silent["kind"][0] == "service"
    assert silent["evidence"][0].startswith("drop: trough 0 events/min")
    assert rows.filter(pl.col("entity") == "quiet").is_empty()  # too sparse for a drop


def test_a_host_silent_for_gap_minutes_is_a_gap_not_also_a_drop(tmp_path):
    host = busy(3)
    host[100:140] = 0
    rows = detect(tmp_path, {"api": busy(4)}, {"h1": host})
    assert rows["evidence"].to_list()[0].startswith("silent for 40 min")
    assert rows.height == 1


def test_no_gap_or_drop_before_the_first_or_after_the_last_line(tmp_path):
    late, early = busy(5), busy(6)
    late[:200] = 0
    early[400:] = 0
    rows = detect(tmp_path, {"api": busy(7)}, {"late": late, "early": early})
    assert rows.is_empty()


def test_drops_in_arrays():
    x = np.full(300, 500.0)
    x[100:130] = 5
    found = find_drops(x, 6)
    assert len(found) == 1 and found[0]["first"] == 100 and found[0]["last"] >= 105
    assert found[0]["peak"] == 5 and found[0]["baseline"] == 500
    assert find_drops(np.full(300, 20.0), 6) == []  # baseline too small
    y = np.full(300, 500.0)
    y[100:130] = 400
    assert find_drops(y, 6) == []  # a dip is not a drop


def test_periodic_repeats_are_merged_into_the_strongest(tmp_path):
    x = busy(8)
    for i, start in enumerate(range(20, MINUTES - 20, 60)):
        x[start : start + 3] = 20000 + 1000 * i
    x[333:336] = 9000  # irregular: kept
    rows = detect(tmp_path, {"cron": x}, {"h1": busy(9)})
    cron = rows.filter(pl.col("entity") == "cron").sort("bin")
    assert cron.height == 2
    assert "repeating about every 60 min" in cron["evidence"].to_list()[0] + cron["evidence"][1]
    assert cron.filter(pl.col("evidence").str.contains("every 60"))["raw"][0] == cron["raw"].max()


def test_periodicity_helpers():
    members, period = periodic_members(np.array([0.0, 60, 120, 180, 240, 300, 303]))
    assert period == 60 and members.sum() == 6 and not members[-1]
    members, period = periodic_members(np.array([0.0, 17, 90, 400, 401, 733]))
    assert not members.any() and period == 0
    assert merge_periodic([]) == []


def test_empty_rate_table_gives_no_rows(tmp_path):
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path))
    empty = pl.DataFrame(schema={"bin": pl.Datetime("us"), "program": pl.String, "n": pl.Int64})
    store.write_parquet(temporal.KEY, "rate_60.parquet", empty)
    store.write_parquet(temporal.KEY, "host_60.parquet", empty.rename({"program": "host"}))
    rows = Volume().detect(DetectContext(store, store.settings, lambda _: None))
    assert rows.is_empty() and "evidence" in rows.columns
