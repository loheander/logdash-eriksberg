"""The Forecasting page's run controls, background callback and results table."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from dash import DiskcacheManager, no_update

from logdash.analyses import forecast as fc
from logdash.analyses.forecast import Forecast
from logdash.app.main import create_app
from logdash.cache.store import CacheStore
from logdash.forecast import job as forecast_job
from logdash.forecast import metrics, runs
from logdash.forecast.params import RunParams
from tests.conftest import Built


class Recorder:
    """Stands in for ``Dash`` and keeps the callback functions by name."""

    def __init__(self) -> None:
        self.funcs: dict[str, Any] = {}

    def callback(self, *_a: Any, **_k: Any) -> Any:
        def deco(fn: Any) -> Any:
            self.funcs[fn.__name__] = fn
            return fn

        return deco


def run_store(built: Built, runs_dir: Path) -> CacheStore:
    settings = dataclasses.replace(built.settings, runs_dir=runs_dir)
    return CacheStore(built.settings.cache_dir, settings)


def find(component: Any, wanted: str) -> Any:
    if getattr(component, "id", None) == wanted:
        return component
    children = getattr(component, "children", None)
    for child in children if isinstance(children, list) else [children]:
        if child is not None and not isinstance(child, str):
            hit = find(child, wanted)
            if hit is not None:
                return hit
    return None


def hand_run(labels: list[int], scores: list[float], model: str = "persistence") -> runs.Run:
    lab = np.array(labels, dtype=bool)
    sc = np.array(scores)
    info = {
        "models": [model],
        "settings": {"horizon": 5},
        "metrics": {model: metrics.score_metrics(lab, sc)},
    }
    return runs.Run("20261023T120000Z-abcdef", info, {"labels": lab, runs.score_key(model): sc})


def test_run_callback_is_a_background_callback(forecast_cache, tmp_path):
    built, registry, _ = forecast_cache
    app = create_app(run_store(built, tmp_path), registry)
    entry = next(v for k, v in app.callback_map.items() if "forecast-run-id.data" in k)
    assert entry["background"]
    cancel = entry["background"]["cancel_inputs"]
    assert [(c.component_id, c.component_property) for c in cancel] == [
        ("forecast-cancel", "n_clicks")
    ]
    progress = [str(o) for o in entry["background"]["progress"]]
    assert progress == ["forecast-progress.value", "forecast-progress-label.children"]
    assert isinstance(entry["manager"], DiskcacheManager)
    assert Path(entry["manager"].handle.directory) == tmp_path / "jobs"
    spec = next(s for s in app._callback_list if "forecast-run-id.data" in s["output"])
    assert spec["running"] == {
        "running": {"forecast-run.disabled": True, "forecast-cancel.disabled": False},
        "runningOff": {"forecast-run.disabled": False, "forecast-cancel.disabled": True},
    }


def test_layout_has_the_run_controls(forecast_cache, tmp_path):
    built, _, _ = forecast_cache
    layout = Forecast().layout(run_store(built, tmp_path))
    horizon = find(layout, fc.HORIZON_ID)
    assert (horizon.min, horizon.max, horizon.step, horizon.value) == (1, 15, 1, 5)
    assert find(layout, fc.CANCEL_BUTTON_ID).disabled is True
    assert find(layout, fc.THRESHOLD_ID).value == 0.5
    assert find(layout, fc.RUN_STORE_ID).storage_type == "session"
    text = str(layout)
    assert "accuracy is misleading" in text
    assert "PR-AUC and F1 are the numbers to read" in text


def test_run_then_results_on_the_forecast_data(forecast_cache, tmp_path):
    built, _, _ = forecast_cache
    store = run_store(built, tmp_path)
    rec = Recorder()
    Forecast().register_callbacks(rec, store)  # type: ignore[arg-type]
    progress: list[Any] = []
    run_id, message = rec.funcs["run_forecast"](progress.append, 1, 5)
    assert message == f"Run {run_id} finished (horizon 5 min)."
    assert len(progress) == 3
    text = str(rec.funcs["show_results"](run_id, 0.5))
    assert "Persistence (repeat the last value)" in text
    assert "test samples" in text and "%" in text
    run = runs.load_run(tmp_path, run_id)
    assert run is not None
    m = run.info["metrics"]["persistence"]
    assert m["positives"] > 0 and 0 < m["pr_auc"] <= 1


def test_results_states_and_failures(forecast_cache, tmp_path, monkeypatch):
    built, _, _ = forecast_cache
    store = run_store(built, tmp_path)
    rec = Recorder()
    Forecast().register_callbacks(rec, store)  # type: ignore[arg-type]
    show = rec.funcs["show_results"]
    assert fc.NO_RUN_TEXT in str(show(None, 0.5))
    for bad in ("20261023T120000Z-abcdef", "../../etc"):
        assert fc.MISSING_RUN_TEXT.format(run_id=bad) in str(show(bad, 0.5))
    run_id = forecast_job.run_forecast_job(lambda _v: None, store, tmp_path, RunParams())
    assert str(show(run_id, None)) == str(show(run_id, 0.5))

    def stale(*_a: Any, **_k: Any) -> str:
        raise ValueError("stale cache")

    monkeypatch.setattr(forecast_job, "run_forecast_job", stale)
    assert rec.funcs["run_forecast"](lambda _v: None, 1, 5) == (
        no_update,
        "The run failed: stale cache",
    )
    assert fc.run_note("forecast-cancel") == fc.CANCELLED_TEXT
    assert fc.run_note("forecast-run") == fc.STARTED_TEXT

    def boom(*_a: Any, **_k: Any) -> str:
        raise KeyError("split")

    monkeypatch.setattr(forecast_job, "run_forecast_job", boom)
    assert rec.funcs["run_forecast"](lambda _v: None, 1, 5)[1].startswith("The run failed:")


def test_threshold_moves_f1_and_accuracy_only():
    run = hand_run([1, 0, 0, 1, 0], [0.9, 0.1, 0.4, 0.35, 0.8])
    half, low = fc.results_table(run, 0.5), fc.results_table(run, 0.3)
    assert half.columns == list(fc.RESULT_COLUMNS)
    assert (half["F1"][0], low["F1"][0]) == ("0.500", "0.667")
    for col, value in (("AUROC", "0.667"), ("PR-AUC", "0.750"), ("positive rate", "40.00 %")):
        assert half[col][0] == low[col][0] == value
    quiet = fc.results_table(hand_run([0, 0, 0], [0.1, 0.2, 0.3]), 0.5)
    assert {quiet[c][0] for c in ("AUROC", "PR-AUC", "F1")} == {fc.NA_NO_SPIKES}
    all_pos = fc.results_table(hand_run([1, 1], [0.1, 0.9]), 0.5)
    assert all_pos["AUROC"][0] == fc.NA_NO_NEGATIVES
    empty = fc.results_table(hand_run([], []), 0.5)
    assert empty["AUROC"][0] == fc.NA_NO_SAMPLES and empty["positive rate"][0] == "-"
    two = hand_run([1, 0], [0.9, 0.1])
    two.info["models"].append("other")
    two.info["metrics"]["other"] = two.info["metrics"]["persistence"]
    two.arrays[runs.score_key("other")] = two.arrays[runs.score_key("persistence")]
    table = fc.results_table(two, 0.5)
    assert table["model"].to_list() == [fc.MODEL_LABELS["persistence"], "other"]


def test_results_callback_over_http(forecast_cache, tmp_path):
    built, registry, _ = forecast_cache
    store = run_store(built, tmp_path)
    run_id = forecast_job.run_forecast_job(lambda _v: None, store, tmp_path, RunParams())
    client = create_app(store, registry).server.test_client()
    body = {
        "output": "forecast-results.children",
        "outputs": {"id": "forecast-results", "property": "children"},
        "inputs": [
            {"id": "forecast-run-id", "property": "data", "value": run_id},
            {"id": "forecast-threshold", "property": "value", "value": 0.5},
        ],
        "changedPropIds": ["forecast-threshold.value"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    assert "Persistence" in resp.get_data(as_text=True)


@pytest.mark.parametrize(
    ("trigger", "text"),
    [("forecast-cancel", fc.CANCELLED_TEXT), ("forecast-run", fc.STARTED_TEXT)],
)
def test_note_run_over_http(forecast_cache, tmp_path, trigger, text):
    built, registry, _ = forecast_cache
    app = create_app(run_store(built, tmp_path), registry)
    out = next(
        k
        for k, v in app.callback_map.items()
        if "forecast-cancel" in str(v["inputs"])
        and "forecast-run" in str(v["inputs"])
        and k.startswith("forecast-message")
    )
    body = {
        "output": out,
        "outputs": {"id": "forecast-message", "property": "children"},
        "inputs": [
            {"id": "forecast-run", "property": "n_clicks", "value": 1},
            {"id": "forecast-cancel", "property": "n_clicks", "value": 1},
        ],
        "changedPropIds": [f"{trigger}.n_clicks"],
    }
    resp = app.server.test_client().post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    assert text in resp.get_data(as_text=True)


def test_page_still_answers_over_http(forecast_cache, tmp_path):
    built, registry, _ = forecast_cache
    client = create_app(run_store(built, tmp_path), registry).server.test_client()
    resp = client.get("/forecast")
    assert resp.status_code == 200


@pytest.mark.parametrize("threshold", [0.05, 0.95])
def test_threshold_extremes_do_not_fail(threshold):
    run = hand_run([1, 0, 0, 1, 0], [0.9, 0.1, 0.4, 0.35, 0.8])
    assert fc.results_table(run, threshold).height == 1
