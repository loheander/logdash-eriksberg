"""The background job of one forecasting run. No polars, no DuckDB here (ADR004)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from logdash.cache.store import CacheStore
from logdash.forecast import baselines, data, metrics, runs
from logdash.forecast.params import RunParams

ProgressFn = Callable[[tuple[Any, ...]], None]  # Dash's set_progress: one value per progress output

STAGES: tuple[str, ...] = ("labels", "scoring")  # #60 and #52 insert their stages before "scoring"
STAGE_TEXT = {
    "labels": "Computing targets and the time split",
    "scoring": "Scoring the test period",
}
SAVING_TEXT = "Saving the run"


def progress_value(
    stage: str, stages: Sequence[str] = STAGES, fraction: float = 0.0
) -> tuple[int, str]:
    """(bar value 0..100, label) at the start of ``stage`` plus ``fraction`` of it."""
    i = list(stages).index(stage)  # ValueError for an unknown stage
    value = round(100 * (i + fraction) / len(stages))
    return value, f"{STAGE_TEXT[stage]} (stage {i + 1} of {len(stages)})"


def run_forecast_job(
    set_progress: ProgressFn,
    store: CacheStore,
    runs_dir: Path,
    params: RunParams,
    run_id: str | None = None,
) -> str:
    """Score every model on the test samples and write the run directory; return the run id.

    Runs in the background-callback process (forked): no polars, no DuckDB (ADR004). Tests call
    it directly with a fake ``set_progress``.
    """
    run_id = run_id or runs.new_run_id()
    runs.cleanup_partial(runs_dir)

    set_progress(progress_value("labels"))
    grid = data.load_spikes(store)
    test = data.period_samples(grid.spike, params.horizon, "test")

    set_progress(progress_value("scoring"))
    scores = {baselines.PERSISTENCE: baselines.persistence_scores(grid.spike, params.horizon, test)}
    model_metrics = {k: metrics.score_metrics(test.labels, s) for k, s in scores.items()}

    set_progress((100, SAVING_TEXT))
    info = {
        "run_id": run_id,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "settings": dataclasses.asdict(params),
        "data": {k: grid.meta.get(k) for k in ("start", "end", "minutes", "burst_k")},
        "hosts": grid.hosts,
        "test_period": grid.meta["split"]["periods"].get("test"),
        "models": list(scores),
        "metrics": model_metrics,
    }
    arrays = {
        "labels": test.labels,
        "host": test.host,
        "minute": test.minute,
        **{runs.score_key(k): v for k, v in scores.items()},
    }
    runs.write_run(runs_dir, run_id, info, arrays)
    return run_id
