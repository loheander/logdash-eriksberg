"""Latency benchmark on the real cache (``uv run pytest -m slow --no-cov``).

Every callback must answer in under 300 ms; a first ``layout`` or a cold-cache request may
take up to 1 s.
Skipped when ``./cache`` has no built events.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from logdash.analyses import anomalies, features, hosts, load_builtin, pca, triage
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

    def clientside_callback(self, *_args: Any, **_kwargs: Any) -> None:
        """Clientside callbacks run in the browser; there is nothing to time here."""


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
        for hbin, hscale in (("60", "relative"), ("300", "relative"), ("300", "absolute")):
            measure(
                "temporal.draw_heatmap",
                f"{name}, bin={hbin}, scale={hscale}",
                CALLBACK_BUDGET_S,
                lambda f=filters, b=hbin, h=hscale: fns["draw_heatmap"](f, b, h, "light"),  # type: ignore[misc]
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


FEATURE_CASES = (
    ("60", "service", "log1p"),
    ("60", "host", "log1p"),
    ("60", "service", "tfidf"),
    ("60", "host", "tfidf"),
    ("300", "service", "tfidf"),
    ("900", "host", "log1p"),
)


def test_features(store: CacheStore) -> None:
    fns = _recorded(store, "features")
    for name, filters in _cases(store).items():
        for window, group, scale in FEATURE_CASES:
            for top in ("15", "40"):
                measure(
                    "features.draw",
                    f"{name}, {window} s, {group}, {scale}, top {top}",
                    CALLBACK_BUDGET_S,
                    partial(fns["draw"], window, group, scale, top, filters, "light"),
                )


def test_features_cold(store: CacheStore) -> None:
    """First request after start-up: the matrix and the statistics are not in memory."""
    fns = _recorded(store, "features")
    for window, group, scale in FEATURE_CASES:
        features.clear_caches()
        draw = partial(fns["draw"], window, group, scale, "40", None, "light")
        t0 = time.perf_counter()
        draw()
        cold = time.perf_counter() - t0
        warm = _time(draw)
        results.append(("features.draw (cold)", f"{window} s, {group}, {scale}", cold, warm))
        if cold >= LAYOUT_BUDGET_S:  # a first request may take as long as a first layout
            over.append(f"features.draw cold [{window} s, {group}, {scale}] {cold * 1000:.0f} ms")


PCA_CASES = (
    ("60", "service", "log1p", "time"),
    ("60", "host", "log1p", "entity"),
    ("60", "service", "tfidf", "score"),
    ("60", "host", "tfidf", "time"),
    ("300", "service", "log1p", "entity"),
    ("900", "host", "tfidf", "score"),
)


def test_pca(store: CacheStore) -> None:
    fns = _recorded(store, "pca")
    for name, filters in _cases(store).items():
        for window, group, scale, color in PCA_CASES:
            measure(
                "pca.draw",
                f"{name}, {window} s, {group}, {scale}, {color}",
                CALLBACK_BUDGET_S,
                partial(fns["draw"], window, group, scale, color, "2", filters, "light"),
            )


def test_pca_cold(store: CacheStore) -> None:
    """First request after start-up: the fit is not in memory, only read from the cache."""
    fns = _recorded(store, "pca")
    for window, group, scale, color in PCA_CASES:
        pca.clear_caches()
        features.clear_caches()
        draw = partial(fns["draw"], window, group, scale, color, "1", None, "light")
        t0 = time.perf_counter()
        draw()
        cold = time.perf_counter() - t0
        warm = _time(draw)
        results.append(("pca.draw (cold)", f"{window} s, {group}, {scale}", cold, warm))
        if cold >= LAYOUT_BUDGET_S:
            over.append(f"pca.draw cold [{window} s, {group}, {scale}] {cold * 1000:.0f} ms")


HOSTS_CASES = (("full", "count"), ("full", "share"), ("collapsed", "count"))


def test_hosts(store: CacheStore) -> None:
    fns = _recorded(store, "hosts")
    for name, filters in _cases(store).items():
        for view, mode in HOSTS_CASES:
            measure(
                "hosts.draw",
                f"{name}, {view}, {mode}",
                CALLBACK_BUDGET_S,
                partial(fns["draw"], view, mode, filters, "light"),
            )


def test_hosts_cold(store: CacheStore) -> None:
    """First request after start-up: the stage files are read from the cache."""
    fns = _recorded(store, "hosts")
    hosts.clear_caches()
    features.clear_caches()
    draw = partial(fns["draw"], "full", "count", None, "light")
    t0 = time.perf_counter()
    draw()
    cold = time.perf_counter() - t0
    results.append(("hosts.draw (cold)", "full, count", cold, _time(draw)))
    if cold >= LAYOUT_BUDGET_S:
        over.append(f"hosts.draw cold {cold * 1000:.0f} ms")


ANOMALY_CASES = (
    ([], "overall"),
    (["structural"], "time"),
    ([], "type"),
    (["structural"], "entity"),
)


def test_anomalies(store: CacheStore) -> None:
    fns = _recorded(store, "anomalies")
    for name, filters in _cases(store).items():
        for types, sort in ANOMALY_CASES:
            measure(
                "anomalies.draw",
                f"{name}, types={types or 'all'}, sort={sort}",
                CALLBACK_BUDGET_S,
                partial(fns["draw"], types, sort, filters, "light"),
            )


def test_anomalies_top_100_is_diverse(store: CacheStore) -> None:
    """The plan's bounds on the combined top 100 by ``overall`` (not a latency measurement)."""
    rows = anomalies.sort_rows(anomalies.load_anomalies(store), "overall")
    top = rows.head(anomalies.TABLE_ROWS)
    # a fleet row stands for many entities, so it is not one of them; per detector and grouping
    # there are only a handful of fleet minutes
    own = top.filter(pl.col("kind") != "fleet")
    per_entity = own.group_by("kind", "entity").len()
    assert per_entity["len"].max() <= 0.10 * anomalies.TABLE_ROWS
    stray = own.filter(pl.col("entity").str.contains("u39|u40")).group_by("entity").len()
    assert stray["len"].max() <= 0.05 * anomalies.TABLE_ROWS  # the two chronic host groups
    assert set(rows["detector"]) <= set(top["detector"])  # no detector is crowded out
    assert (top["kind"] == "fleet").sum() <= 0.25 * anomalies.TABLE_ROWS


def test_anomalies_cold(store: CacheStore) -> None:
    """First request after start-up: the table is read from the cache, not from memory."""
    fns = _recorded(store, "anomalies")
    anomalies.clear_caches()
    draw = partial(fns["draw"], [], "score", None, "light")
    t0 = time.perf_counter()
    draw()
    cold = time.perf_counter() - t0
    results.append(("anomalies.draw (cold)", "no filters", cold, _time(draw)))
    if cold >= LAYOUT_BUDGET_S:
        over.append(f"anomalies.draw cold {cold * 1000:.0f} ms")


def fake_jev(_key: str, body: dict[str, Any], _timeout_s: float) -> dict[str, Any]:
    """Stands in for the model: the benchmark covers the page, not the live call."""
    answers = {}
    for qid, q in body["questions"].items():
        if q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.7}
        elif q["type"] == "choice":
            first = next(iter(q["criteria"]))
            probs = {o: (1.0 if o == first else 0.0) for o in q["criteria"]}
            answers[qid] = {"type": "choice", "choice": first, "probabilities": probs,
                            "confidence": 1.0}  # fmt: skip
        else:
            probs = {str(i): (1.0 if i == 1 else 0.0) for i in range(len(q["criteria"]))}
            answers[qid] = {"type": "score", "score": 1.0, "probabilities": probs,
                            "confidence": 1.0}  # fmt: skip
    return {"answers": answers, "usage": {"cost": 0.0}}


def test_triage(store: CacheStore) -> None:
    rec = Recorder()
    triage.Triage(poster=fake_jev, env={"OPENROUTER_API_KEY": "fake"}).register_callbacks(
        rec,  # type: ignore[arg-type]
        store,
    )
    for name, filters in _cases(store).items():
        for types, sort in ANOMALY_CASES:
            measure(
                "triage.draw",
                f"{name}, types={types or 'all'}, sort={sort}",
                CALLBACK_BUDGET_S,
                partial(rec.funcs["draw"], types, sort, filters, "light", {}),
            )
    rows = triage.load_rows(store)
    for kind in sorted(set(rows["kind"])):
        # the widest window of each kind: the slowest state query
        row = (
            rows.filter(pl.col("kind") == kind).sort("window_s", descending=True).row(0, named=True)
        )
        out = measure(
            "triage.ask (fake jev)",
            f"{kind}, {row['window_s']} s",
            CALLBACK_BUDGET_S,
            partial(rec.funcs["ask"], {"key": row["row_key"], "n": 1}),
        )
        assert out["error"] is None, out["error"]


def test_triage_cold(store: CacheStore) -> None:
    """First request after start-up: the table is read from the cache, not from memory."""
    rec = Recorder()
    triage.Triage(poster=fake_jev, env={}).register_callbacks(rec, store)  # type: ignore[arg-type]
    triage.clear_caches()
    anomalies.clear_caches()
    draw = partial(rec.funcs["draw"], [], "overall", None, "light", {})
    t0 = time.perf_counter()
    draw()
    cold = time.perf_counter() - t0
    results.append(("triage.draw (cold)", "no filters", cold, _time(draw)))
    if cold >= LAYOUT_BUDGET_S:
        over.append(f"triage.draw cold {cold * 1000:.0f} ms")


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
