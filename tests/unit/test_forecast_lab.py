"""Forecast lab: samples, persistence, metrics, run files and the job (no built cache)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import multiprocess
import numpy as np
import polars as pl
import pytest

from logdash.analyses.forecast import split_bounds, targets
from logdash.cache.store import CacheStore
from logdash.forecast import baselines, data, job, metrics, runs
from logdash.forecast.params import RunParams

RUN_ID = "20261023T120000Z-abcdef"


def write_forecast_cache(root: Path, spike: np.ndarray, hosts: list[str]) -> CacheStore:
    """A minimal ``cache/forecast/`` (frame.parquet and meta.json) for a spike array."""
    n_hosts, minutes = spike.shape
    start = datetime(2023, 5, 21)
    bins = [start + timedelta(minutes=i) for i in range(minutes)]
    frame = pl.DataFrame(
        {
            "bin": bins * n_hosts,
            "host": [h for h in hosts for _ in range(minutes)],
            "spike": spike.reshape(-1),
        }
    )
    train_end, val_end = split_bounds(minutes)

    def iso(i: int) -> str:
        return (start + timedelta(minutes=i)).isoformat()

    meta = {
        "start": iso(0),
        "end": iso(minutes),
        "minutes": minutes,
        "hosts": n_hosts,
        "burst_k": 6.0,
        "split": {
            "train_end": train_end,
            "validation_end": val_end,
            "periods": {
                "train": [iso(0), iso(train_end)],
                "validation": [iso(train_end), iso(val_end)],
                "test": [iso(val_end), iso(minutes)],
            },
        },
    }
    store = CacheStore(root)
    store.write_parquet("forecast", "frame.parquet", frame)
    store.write_json("forecast", "meta.json", meta)
    return store


def hand_made_spike() -> np.ndarray:
    spike = np.zeros((3, 48), dtype=bool)
    spike[0, 34:37] = True  # test period starts at minute 32
    spike[1, 40] = True
    spike[2, 5] = True  # train period only
    return spike


def test_period_samples_are_the_full_grid_without_crossings():
    spike = np.random.default_rng(0).random((2, 24)) < 0.2
    assert split_bounds(24) == (14, 16)
    test = data.period_samples(spike, 2, "test")
    assert len(test) == 12
    assert test.host.tolist() == [0] * 6 + [1] * 6
    assert test.minute.tolist() == list(range(16, 22)) * 2
    assert np.array_equal(test.labels, targets(spike, 2)[test.host, test.minute])
    train = data.period_samples(spike, 1, "train")
    assert train.minute.max() == 12  # minute 13 would reach into validation
    assert data.period_samples(spike, 1, "validation").minute.tolist() == [14, 14]
    empty = data.period_samples(spike, 2, "validation")
    assert len(empty) == 0
    assert empty.host.dtype == empty.minute.dtype == np.int32
    assert empty.labels.dtype == bool
    with pytest.raises(ValueError):
        data.period_samples(spike, 1, "nope")


def test_persistence_repeats_the_last_target():
    spike = np.array([[0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0, 0]], dtype=bool)
    samples = data.Samples(
        np.zeros(12, np.int32), np.arange(12, dtype=np.int32), np.zeros(12, dtype=bool)
    )
    scores = baselines.persistence_scores(spike, 2, samples)
    assert scores.tolist() == [0, 0, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0]
    assert scores.dtype == np.float32
    rng = np.random.default_rng(1)
    big = rng.random((3, 60)) < 0.1
    full = data.Samples(
        np.repeat(np.arange(3), 60).astype(np.int32),
        np.tile(np.arange(60), 3).astype(np.int32),
        np.zeros(180, dtype=bool),
    )
    for horizon in (1, 3, 5):
        got = baselines.persistence_scores(big, horizon, full).reshape(3, 60)
        assert np.array_equal(got[:, horizon:] > 0, targets(big, horizon)[:, : 60 - horizon])


def test_metrics_known_values():
    labels = np.array([1, 0, 0, 1, 0], dtype=bool)
    scores = np.array([0.9, 0.1, 0.4, 0.35, 0.8])
    m = metrics.score_metrics(labels, scores)
    assert (m["samples"], m["positives"], m["positive_rate"]) == (5, 2, 0.4)
    assert m["auroc"] == pytest.approx(4 / 6)
    assert m["pr_auc"] == pytest.approx(0.75)
    at_half = metrics.threshold_metrics(labels, scores, 0.5)
    assert at_half["accuracy"] == pytest.approx(0.6) and at_half["f1"] == pytest.approx(0.5)
    at_low = metrics.threshold_metrics(labels, scores, 0.3)
    assert at_low["accuracy"] == pytest.approx(0.6) and at_low["f1"] == pytest.approx(2 / 3)


def test_metrics_without_positives():
    scores = np.array([0.2, 0.7, 0.1])
    none = np.zeros(3, dtype=bool)
    m = metrics.score_metrics(none, scores)
    assert m["auroc"] is None and m["pr_auc"] is None
    t = metrics.threshold_metrics(none, scores, 0.5)
    assert t["f1"] is None and isinstance(t["accuracy"], float)
    ones = np.ones(3, dtype=bool)
    m_all = metrics.score_metrics(ones, scores)
    assert m_all["auroc"] is None and m_all["pr_auc"] == 1.0
    empty = metrics.score_metrics(np.zeros(0, dtype=bool), np.zeros(0))
    assert empty["samples"] == 0
    assert all(v is None for k, v in empty.items() if k != "samples" and k != "positives")
    t_empty = metrics.threshold_metrics(np.zeros(0, dtype=bool), np.zeros(0), 0.5)
    assert t_empty == {"accuracy": None, "f1": None}
    for value in (m, m_all, empty, t, t_empty):
        json.dumps(value, allow_nan=False)


def test_progress_value_per_stage():
    assert job.progress_value("labels") == (
        0,
        "Computing targets and the time split (stage 1 of 2)",
    )
    assert job.progress_value("scoring")[0] == 50
    assert job.progress_value("scoring", fraction=0.5)[0] == 75
    with pytest.raises(ValueError):
        job.progress_value("nope")


def test_run_params_validate_the_horizon():
    for bad in (0, 16):
        with pytest.raises(ValueError, match="between 1 and 15"):
            RunParams(bad)
    assert RunParams().horizon == 5


def test_write_and_load_run(tmp_path):
    runs.clear_caches()
    arrays = {
        "labels": np.array([True, False]),
        "score_persistence": np.array([0.0, 1.0], dtype=np.float32),
    }
    info = {"models": ["persistence"], "metrics": {}}
    runs.write_run(tmp_path, RUN_ID, info, arrays)
    assert not list(tmp_path.glob(".tmp-*"))
    run = runs.load_run(tmp_path, RUN_ID)
    assert run is not None and run.info == info and run.models == ["persistence"]
    assert np.array_equal(run.labels, arrays["labels"])
    assert np.array_equal(run.scores("persistence"), arrays["score_persistence"])
    assert runs.load_run(tmp_path, RUN_ID) is run
    for bad in (None, "20261023T120001Z-abcdef", "../etc", "x"):
        assert runs.load_run(tmp_path, bad) is None
    with pytest.raises(FileExistsError):
        runs.write_run(tmp_path, RUN_ID, info, arrays)
    other = "20261023T120002Z-abcdef"
    with pytest.raises(ValueError):
        runs.write_run(tmp_path, other, {"x": float("nan")}, arrays)
    assert not (tmp_path / other).exists() and not list(tmp_path.glob(".tmp-*"))
    with pytest.raises(ValueError, match="invalid run id"):
        runs.write_run(tmp_path, "../x", info, arrays)
    broken = "20261023T120003Z-abcdef"
    runs.write_run(tmp_path, broken, {"models": ["other"]}, {"labels": arrays["labels"]})
    assert runs.load_run(tmp_path, broken) is None
    runs.clear_caches()
    again = runs.load_run(tmp_path, RUN_ID)
    assert again is not run and np.array_equal(again.labels, run.labels)  # type: ignore[union-attr]


def test_run_ids():
    now = datetime(2026, 10, 23, 12, 0, 0).astimezone()
    assert runs.valid_run_id(runs.new_run_id(now))
    assert not runs.valid_run_id(5)


def test_cleanup_partial(tmp_path):
    runs.cleanup_partial(tmp_path / "missing")
    old, fresh, done = tmp_path / ".tmp-a", tmp_path / ".tmp-b", tmp_path / RUN_ID
    for d in (old, fresh, done):
        d.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))
    runs.cleanup_partial(tmp_path)
    assert not old.exists() and fresh.exists() and done.exists()


def test_job_with_a_fake_set_progress(tmp_path):
    runs.clear_caches()
    spike = hand_made_spike()
    hosts = ["a", "b", "c"]
    store = write_forecast_cache(tmp_path / "cache", spike, hosts)
    calls: list[Any] = []
    runs_dir = tmp_path / "runs"
    got = job.run_forecast_job(calls.append, store, runs_dir, RunParams(horizon=2), run_id=RUN_ID)
    assert got == RUN_ID
    assert calls == [
        job.progress_value("labels"),
        job.progress_value("scoring"),
        (100, "Saving the run"),
    ]
    assert [c[0] for c in calls] == sorted(c[0] for c in calls)
    run = runs.load_run(runs_dir, RUN_ID)
    assert run is not None
    assert run.models == ["persistence"] and run.info["settings"] == {"horizon": 2}
    assert run.info["hosts"] == hosts
    test = data.period_samples(spike, 2, "test")
    assert np.array_equal(run.labels, test.labels)
    assert np.array_equal(run.arrays["host"], test.host)
    assert np.array_equal(run.scores("persistence"), baselines.persistence_scores(spike, 2, test))
    expected = metrics.score_metrics(test.labels, run.scores("persistence"))
    assert run.info["metrics"]["persistence"] == expected
    assert expected["positives"] > 0


def test_stale_cache_is_an_error(tmp_path):
    store = write_forecast_cache(tmp_path / "cache", hand_made_spike(), ["a", "b", "c"])
    meta = store.read_json("forecast", "meta.json")
    store.write_json("forecast", "meta.json", {**meta, "minutes": 50})
    with pytest.raises(ValueError, match="rebuild"):
        data.load_spikes(store)


def test_cancelled_job_saves_nothing(tmp_path, monkeypatch):
    runs.clear_caches()
    store = write_forecast_cache(tmp_path / "cache", hand_made_spike(), ["a", "b", "c"])
    runs_dir = tmp_path / "runs"

    class Cancelled(Exception):
        pass

    seen = []

    def cancel_on_third(value: Any) -> None:
        seen.append(value)
        if len(seen) == 3:
            raise Cancelled

    with pytest.raises(Cancelled):
        job.run_forecast_job(cancel_on_third, store, runs_dir, RunParams(), run_id=RUN_ID)
    assert runs.load_run(runs_dir, RUN_ID) is None
    assert not runs_dir.exists() or not list(runs_dir.glob("*"))

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(np, "savez_compressed", boom)
    with pytest.raises(OSError):
        job.run_forecast_job(lambda _v: None, store, runs_dir, RunParams(), run_id=RUN_ID)
    assert not list(runs_dir.glob("*"))


def _job_in_child(root: str, runs_dir: str, run_id: str) -> None:
    job.run_forecast_job(
        lambda _v: None, CacheStore(Path(root)), Path(runs_dir), RunParams(), run_id=run_id
    )


def test_job_runs_in_a_forked_process(tmp_path):
    """ADR004 guard: the job must not call polars or DuckDB (a fork child would hang)."""
    runs.clear_caches()
    root = tmp_path / "cache"
    write_forecast_cache(root, hand_made_spike(), ["a", "b", "c"])
    n = 2_000_000
    pl.DataFrame({"g": np.arange(n) % 1000, "a": np.ones(n)}).group_by("g").agg(pl.col("a").sum())
    runs_dir = tmp_path / "runs"
    proc = multiprocess.get_context("fork").Process(
        target=_job_in_child, args=(str(root), str(runs_dir), RUN_ID)
    )
    proc.start()
    proc.join(60)
    if proc.is_alive():
        proc.kill()
        pytest.fail("the job hangs in a forked process: it must not call polars or DuckDB (ADR004)")
    assert proc.exitcode == 0
    assert runs.load_run(runs_dir, RUN_ID) is not None
