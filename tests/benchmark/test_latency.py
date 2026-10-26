"""Latency benchmark on the real cache (``uv run pytest -m slow --no-cov``).

Every callback must answer in under 300 ms; a first ``layout`` or a cold-cache request may
take up to 1 s.
Skipped when ``./cache`` has no built events.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from logdash.analyses import anomalies, features, hosts, load_builtin, pca
from logdash.analyses.base import REGISTRY
from logdash.analyses.explorer import TagRestriction, fetch_page, search_page, tag_restriction
from logdash.analyses.tags import tags_frame
from logdash.analyses.temporal import Temporal
from logdash.app.filters import from_dict
from logdash.app.main import create_app, filter_options
from logdash.app.tag_filters import TagFilter, load_tags
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.forecast import job as forecast_job
from logdash.forecast import runs
from logdash.forecast.params import RunParams
from logdash.tagging.model import COMPONENTS, Tag

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
    for analysis in REGISTRY.pages():
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


TAG_FILTER_CASES = (
    ("no tag filter", False, False, []),
    ("hide noise", True, False, []),
    ("actionable only", False, True, []),
    ("component network", False, False, ["network"]),
    ("all three", True, True, ["network", "storage"]),
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
    for case, hide, actionable, components in TAG_FILTER_CASES:
        measure(
            "templates.filter_grid",
            case,
            CALLBACK_BUDGET_S,
            lambda h=hide, a=actionable, c=components: fns["filter_grid"](h, a, c, rows),  # type: ignore[misc]
        )
    measure(
        "templates.draw_volume",
        "no selection",
        CALLBACK_BUDGET_S,
        lambda: fns["draw_volume"]([], None, "light"),
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


def test_forecast(store: CacheStore) -> None:
    fns = _recorded(store, "forecast")
    for theme in ("light", "dark"):
        measure(
            "forecast.draw_spikes",
            theme,
            CALLBACK_BUDGET_S,
            lambda t=theme: fns["draw_spikes"](t),  # type: ignore[misc]
        )


@pytest.fixture(scope="module")
def forecast_run(
    store: CacheStore, tmp_path_factory: pytest.TempPathFactory
) -> tuple[CacheStore, str]:
    """A store whose runs_dir is temporary, and one real run (job called directly).

    Later forecast benchmarks (#52, #54) reuse it.
    """
    runs_dir = tmp_path_factory.mktemp("forecast_runs")
    run_store = CacheStore(store.root, dataclasses.replace(store.settings, runs_dir=runs_dir))
    t0 = time.perf_counter()
    run_id = forecast_job.run_forecast_job(lambda _v: None, run_store, runs_dir, RunParams())
    results.append(("forecast job", "horizon 5", time.perf_counter() - t0, 0.0))
    return run_store, run_id


def test_forecast_results(forecast_run: tuple[CacheStore, str]) -> None:
    """The threshold callback on a real run (cold, then warm)."""
    run_store, run_id = forecast_run
    fns = _recorded(run_store, "forecast")
    runs.clear_caches()
    for threshold in (0.5, 0.25):
        measure(
            "forecast.show_results",
            f"threshold {threshold}",
            CALLBACK_BUDGET_S,
            lambda t=threshold: fns["show_results"](run_id, t),  # type: ignore[misc]
        )
    measure(
        "forecast.show_results",
        "no run",
        CALLBACK_BUDGET_S,
        lambda: fns["show_results"](None, 0.5),
    )


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


ANOMALY_TAG_CASES = (
    ("hide noise", True, False, []),
    ("actionable only", False, True, []),
    ("component network", False, False, ["network"]),
    ("all three", True, True, ["network", "storage"]),
)


def _anomaly_tags(store: CacheStore) -> tuple[str, pl.DataFrame]:
    """("real", cached tags) or ("stand-in", tags for every template).

    Stand-ins: template 1 (the largest) is noise, texts that read like a failure are
    actionable, and the components cycle by id, so every case keeps and drops some rows.
    """
    real = load_tags(store)
    if real is not None:
        return "real", real
    templates = store.read_parquet("templates", "templates.parquet")
    failure = re.compile(r"(?i)error|fail|exception|refused|timeout")
    stand_in = {
        text: Tag(bool(failure.search(text)), COMPONENTS[tid % len(COMPONENTS)], tid == 1, "-")
        for tid, text in templates.select("template_id", "template").iter_rows()
    }
    return "stand-in", tags_frame(templates, stand_in)


def test_anomalies_tag_filters(store: CacheStore, monkeypatch: pytest.MonkeyPatch) -> None:
    source, tags = _anomaly_tags(store)
    monkeypatch.setattr(anomalies, "load_tags", lambda _store: tags)
    fns = _recorded(store, "anomalies")
    for name, filters in _cases(store).items():
        for case, hide, actionable, components in ANOMALY_TAG_CASES:
            measure(
                "anomalies.draw",
                f"tags ({source}) {case}, {name}",
                CALLBACK_BUDGET_S,
                partial(fns["draw"], [], "overall", filters, "light", hide, actionable, components),
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


ACTIONABLE_PROXY = r"(?i)error|fail|exception|refused|timeout"


def _tag_restrictions(
    store: CacheStore,
) -> tuple[str, TagRestriction | None, TagRestriction | None]:
    """(source, hide noise, actionable only): real tags if cached, else stand-ins.

    Stand-ins: template 1 (the largest) as noise, and the templates whose text reads like a
    failure as actionable (53 templates, about 119 000 lines on the real cache).
    """
    if load_tags(store) is not None:
        return (
            "real",
            tag_restriction(store, TagFilter(hide_noise=True)),
            tag_restriction(store, TagFilter(actionable_only=True)),
        )
    t = store.read_parquet("templates", "templates.parquet")
    ids = t.filter(pl.col("template").str.contains(ACTIONABLE_PROXY))["template_id"].to_list()
    return "proxy", TagRestriction((1,), keep=False), TagRestriction(tuple(sorted(ids)), keep=True)


def test_explorer_tags(store: CacheStore) -> None:
    con = store.connect()
    src, hide, act = _tag_restrictions(store)
    hour = _hour_filters(store)
    host = _cases(store)["host"]
    cases: dict[str, tuple[dict[str, Any] | None, dict[str, Any]]] = {
        "hide noise": (None, {"tags": hide}),
        "hide noise, page 50": (None, {"tags": hide, "page": 50}),
        "hide noise, template 5": (None, {"tags": hide, "template": 5}),
        "actionable": (None, {"tags": act}),
        "actionable, page 5": (None, {"tags": act, "page": 5}),
        "actionable, 1 hour": (hour, {"tags": act}),
        "actionable, host": (host, {"tags": act}),
        "actionable, text 'error'": (None, {"tags": act, "text": "error"}),
    }
    for name, (filters, kw) in cases.items():
        measure(
            "explorer.fetch_page",
            f"tags ({src}): {name}",
            CALLBACK_BUDGET_S,
            lambda f=filters, k=kw: (search_page if "text" in k else fetch_page)(  # type: ignore[misc]
                con.cursor(), store, from_dict(f), **k
            ),
        )
    measure(
        "explorer.tag_restriction",
        "all three",
        CALLBACK_BUDGET_S,
        lambda: tag_restriction(store, TagFilter(True, True, ("network", "storage"))),
    )


def test_zz_print_table() -> None:
    print()
    print(f"{'what':<28}{'case':<32}{'cold ms':>9}{'warm ms':>9}")
    for what, case, cold, warm in results:
        print(f"{what:<28}{case:<32}{cold * 1000:>9.0f}{warm * 1000:>9.0f}")
    assert not over, "over budget:\n" + "\n".join(over)
