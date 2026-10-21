"""Latency benchmark on the real cache (``uv run pytest -m slow --no-cov``).

Every callback must answer in under 300 ms; a first ``layout`` may take up to 1 s.
Skipped when ``./cache`` has no built events.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from logdash.analyses import load_builtin
from logdash.analyses.base import REGISTRY
from logdash.analyses.explorer import fetch_page, search_page
from logdash.analyses.temporal import Temporal
from logdash.app.filters import from_dict
from logdash.app.main import create_app, filter_options
from logdash.cache.store import CacheStore
from logdash.config import Settings

pytestmark = pytest.mark.slow

CACHE = Path("cache")
CALLBACK_BUDGET_S = 0.3
LAYOUT_BUDGET_S = 1.0


class Recorder:
    """Stands in for ``Dash`` and keeps the callback functions by name."""

    def __init__(self) -> None:
        self.funcs: dict[str, Callable[..., Any]] = {}

    def callback(self, *_args: Any, **_kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.funcs[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture(scope="module")
def store() -> CacheStore:
    if not (CACHE / "events").exists() or not (CACHE / "manifest.json").exists():
        pytest.skip("no real cache in ./cache")
    load_builtin()
    return CacheStore(CACHE, Settings.from_env().with_overrides(cache_dir=CACHE))


results: list[tuple[str, str, float, float]] = []  # (what, case, cold s, warm s)
over: list[str] = []


def measure(what: str, case: str, budget: float, fn: Callable[[], Any]) -> Any:
    t0 = time.perf_counter()
    out = fn()
    cold = time.perf_counter() - t0
    warm = min(_time(fn) for _ in range(2))
    results.append((what, case, cold, warm))
    if warm >= budget:
        over.append(f"{what} [{case}] warm {warm * 1000:.0f} ms over {budget * 1000:.0f} ms")
    return out


def _time(fn: Callable[[], Any]) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def test_app_start(store: CacheStore) -> None:
    t0 = time.perf_counter()
    create_app(store)
    results.append(("create_app", "start-up", time.perf_counter() - t0, 0.0))
    t0 = time.perf_counter()
    filter_options(store)
    results.append(("filter_options", "start-up", time.perf_counter() - t0, 0.0))


def test_layouts(store: CacheStore) -> None:
    for analysis in REGISTRY.ordered():
        measure(f"{analysis.key}.layout", "-", LAYOUT_BUDGET_S, lambda a=analysis: a.layout(store))  # type: ignore[misc]


def _hour_filters(store: CacheStore) -> dict[str, Any]:
    meta = store.read_json("temporal", "meta.json")
    start = str(meta["ts_min"]).replace(" ", "T")[:13] + ":00:00"
    h = int(start[11:13]) + 6
    return {"start": f"{start[:11]}{h:02d}:00:00", "end": f"{start[:11]}{h + 1:02d}:00:00"}


def _recorded(store: CacheStore, key: str) -> dict[str, Callable[..., Any]]:
    rec = Recorder()
    REGISTRY.get(key).register_callbacks(rec, store)  # type: ignore[arg-type]
    return rec.funcs


def _cases(store: CacheStore) -> dict[str, dict[str, Any] | None]:
    hosts = store.read_json("temporal", "meta.json")["hosts"]
    hour = _hour_filters(store)
    return {
        "no filters": None,
        "1 hour": hour,
        "host": {"hosts": [hosts[len(hosts) // 2]]},
        "1 hour + host": {**hour, "hosts": [hosts[len(hosts) // 2]]},
    }


def test_overview_distributions(store: CacheStore) -> None:
    over = _recorded(store, "overview")
    measure("overview.missing", "light", CALLBACK_BUDGET_S, lambda: over["draw_missing"]("light"))
    dist = _recorded(store, "distributions")
    for name, filters in _cases(store).items():
        for scale in ("linear", "log"):
            measure(
                "distributions.update",
                f"{name}, {scale}",
                CALLBACK_BUDGET_S,
                lambda f=filters, s=scale: dist["update"](f, s, "light"),  # type: ignore[misc]
            )


def test_temporal(store: CacheStore) -> None:
    fns = _recorded(store, "temporal")
    for name, filters in _cases(store).items():
        for split in ("level", "service", "host"):
            measure(
                "temporal.draw_rate",
                f"{name}, split={split}",
                CALLBACK_BUDGET_S,
                lambda f=filters, s=split: fns["draw_rate"](f, s, "light"),  # type: ignore[misc]
            )
        for hbin in ("60", "300"):
            measure(
                "temporal.draw_heatmap",
                f"{name}, bin={hbin}",
                CALLBACK_BUDGET_S,
                lambda f=filters, b=hbin: fns["draw_heatmap"](f, b, "light"),  # type: ignore[misc]
            )
        measure(
            "temporal.draw_anomalies",
            name,
            CALLBACK_BUDGET_S,
            lambda f=filters: fns["draw_anomalies"](f),  # type: ignore[misc]
        )
    meta = store.read_json("temporal", "meta.json")
    measure(
        "temporal.draw_spectra",
        meta["services"][1],
        CALLBACK_BUDGET_S,
        lambda: fns["draw_spectra"](meta["services"][1], "light"),
    )
    hour = _hour_filters(store)
    relayout = {
        "xaxis.range[0]": hour["start"].replace("T", " "),
        "xaxis.range[1]": hour["end"].replace("T", " "),
    }
    measure(
        "temporal.brush",
        "1 hour brush",
        CALLBACK_BUDGET_S,
        lambda: Temporal.brushed(None, relayout, None),
    )


def test_templates(store: CacheStore) -> None:
    fns = _recorded(store, "templates")
    rows = [{"template_id": 0}]
    for name, filters in _cases(store).items():
        measure(
            "templates.draw_volume",
            f"{name}, template 0",
            CALLBACK_BUDGET_S,
            lambda f=filters: fns["draw_volume"](rows, f, "light"),  # type: ignore[misc]
        )
    measure(
        "templates.pick_params", "template 0", CALLBACK_BUDGET_S, lambda: fns["pick_params"](rows)
    )
    options, value = fns["pick_params"](rows)
    measure(
        "templates.draw_params",
        str(value),
        CALLBACK_BUDGET_S,
        lambda: fns["draw_params"](value, rows, "light"),
    )


def test_explorer(store: CacheStore) -> None:
    con = store.connect()

    cases: dict[str, tuple[dict[str, Any] | None, dict[str, Any]]] = {}
    for name, filters in _cases(store).items():
        cases[name] = (filters, {})
    base_hour = _hour_filters(store)
    cases["template 0, no filters"] = (None, {"template": 0})
    cases["template 5, 1 hour"] = (base_hour, {"template": 5})
    cases["text 'error', no filters"] = (None, {"text": "error"})
    cases["text 'error', 1 hour"] = (base_hour, {"text": "error"})
    cases["text rare, no filters"] = (None, {"text": "no valid host was found"})
    cases["page 50, no filters"] = (None, {"page": 50})
    for name, (filters, kw) in cases.items():
        measure(
            "explorer.fetch_page",
            name,
            CALLBACK_BUDGET_S,
            lambda f=filters, k=kw: (search_page if "text" in k else fetch_page)(  # type: ignore[misc]
                con.cursor(), store, from_dict(f), **k
            ),
        )


def test_zz_print_table() -> None:
    print()
    print(f"{'what':<28}{'case':<32}{'cold ms':>9}{'warm ms':>9}")
    for what, case, cold, warm in results:
        print(f"{what:<28}{case:<32}{cold * 1000:>9.0f}{warm * 1000:>9.0f}")
    assert not over, "over budget:\n" + "\n".join(over)
