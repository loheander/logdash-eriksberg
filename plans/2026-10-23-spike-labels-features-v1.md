### 1. Goal

A new precompute stage `forecast` writes, for every host and minute, the spike label and the five input feature groups of the forecasting lab (`cache/forecast/frame.parquet` + `meta.json`). A new **Forecasting** page (`/forecast`) shows what the stage found. Nothing is trained. The contract in the issue body is binding; this plan fills in every detail it leaves open.

Acceptance criteria (from the issue):
- [ ] `uv run logdash build` builds the `forecast` stage after `hosts`; `logdash status` lists it; changing `LOGDASH_BURST_K` makes it stale.
- [ ] On the forecast synthetic dataset (`forecast_spikes=24`) the minutes of every injected spike (the 24 new ones and the existing `spike_host` spike) are spike minutes in `frame.parquet`, and no minute more than one minute away from an injected spike is; the precursor minutes are not spike minutes.
- [ ] The spike minutes equal the minutes covered by the volume detector's host episodes before `merge_periodic` (a test compares them on the synthetic cache).
- [ ] `frame.parquet` has exactly hosts × minutes rows, no nulls or NaN, and every feature column named in `meta.json`.
- [ ] The `lags` z feature at minute `t` does not change when data after `t` changes (a test proves it).
- [ ] The `/forecast` page shows the number of host-minutes and spike episodes, a chart of spike minutes over time with the train, validation and test periods shaded, and a table of the positive rate for horizons 1, 5 and 15 minutes in each period. Light and dark themes both work.
- [ ] The page layout answers within the latency benchmark's budget on the real cache (`uv run pytest -m slow --no-cov`), and the benchmark covers it.
- [ ] `adrs/ADR002-spike-forecasting-lab.md` records the contract; README lists the page and its stage.
- [ ] The quality gate passes with coverage at or above 90 %.

### 2. Depends on

None. (Later stories #51 to #54 depend on this one.)

### 3. Context

- `src/logdash/analyses/__init__.py:6`: `BUILTIN`, registration order breaks ties in the topological build order (`analyses/base.py:107`). Insert `"forecast"` after `"hosts"`.
- `src/logdash/analyses/base.py:23`: the `Analysis` protocol (`key, title, version, depends_on, compute, layout, register_callbacks`), `ComputeContext` (`con` with an `events` view, `settings`, `out_dir`, `progress`, `note_rows`). `setting_names` is read by `build.compute_fingerprints` (`build.py:50`), so `("burst_k",)` puts `burst_k` into the stage fingerprint.
- `src/logdash/analyses/temporal.py:71`: writes `temporal/host_60.parquet` (`bin` naive `Datetime("us")`, `host`, `n`; host `(missing)` = `MISSING` for malformed rows) and `rate_60.parquet`. `span` (`:100`), `dense_series` (`:107`), `rolling_baseline(x, trailing=True)` (`:211`, window ends at the bin: verified that changing `x[t+1:]` leaves `(x - median) / scale` at `0..t` unchanged), `find_bursts(x, k)` (`:226`, returns dicts with inclusive `first`, `last` minute indices).
- `src/logdash/detectors/volume.py:40` `episodes()`: per host, `dense_series(...)` on the span `temporal.span(pl.concat([rate["bin"], hosts["bin"]]))` (`:174`), then `find_bursts`, then `merge_periodic` (`:101`). The forecast stage must use the same span and the same series, so its labels equal the pre-merge episodes.
- `src/logdash/analyses/templates.py:190`: `template_counts_host.parquet` (`template_id, bin, host, n`, 1-minute bins); `templates.parquet` (`template_id, template, count, ...`); `short(text, limit)` (`:243`).
- `src/logdash/analyses/hosts.py:38`: `hosts/peers.parquet` (`host, peer, distance`), only hosts with a valid peer group are in it (real cache: 139 of 162 hosts).
- `src/logdash/ingest/normalize.py:7`: `LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "UNKNOWN")`; every line gets one of them.
- `src/logdash/analyses/anomalies.py:395`: how a stage reads upstream files in `compute`: `CacheStore(ctx.out_dir.parent, ctx.settings)`.
- `src/logdash/app/ui.py` (`page`, `card`, `stat_card`, `grid`, `graph`, `table`, `fmt_int`), `src/logdash/app/figures.py` (`style`, `empty_figure`, `PALETTE`), `src/logdash/app/shell.py:17` `THEME_STORE_ID`.
- `tests/synthetic.py`: generator; extra injections use their own `random.Random(spec.seed + N)` so the rest stays identical (`:239`, `:263`, `:270`, `:297`). `tests/unit/test_contextual.py:157` shows a module fixture with a dataset of its own.
- `tests/benchmark/test_latency.py:87` `test_layouts` already times `layout()` of every registered analysis against `LAYOUT_BUDGET_S` (1 s), so the new page layout is covered once it is registered; callbacks are timed per module with `_recorded(...)`.

Measured on the real cache (`host_60`, `find_bursts` with `burst_k=6`): 162 hosts (without `(missing)`), 1,439 minutes (00:01 to 23:59 UTC), so 233,118 host-minutes; **270 episodes on 16 hosts, 1,059 spike minutes**. The figures in the contract (271 on 17 hosts, 1,079 minutes) include the `(missing)` host, which the contract itself drops; the ADR states the corrected numbers. The events aggregate below takes 2.2 s and returns 385 k rows.

### 4. Design

One module, `src/logdash/analyses/forecast.py`, split into pure functions (testable on hand-made arrays and tables) and a thin `Forecast` class.

- **Inputs** are gathered into a frozen dataclass `Sources` by `load_sources`, and `build(sources, burst_k)` turns them into the frame, the meta dict and a small per-minute spike count table. `compute` only loads, calls `build` and writes. Tests call `build` directly with altered inputs (the causality test) and on the synthetic cache.
- **Dense cubes**: every count table is scattered into a NumPy array `(keys, hosts, minutes)` with `np.add.at`, then reshaped into host-major columns. Rows of the frame are ordered by host (sorted) and then by minute, so each host's series is one contiguous block of `minutes` rows; later stories slice windows from it without sorting.
- **Levels and services per host-minute** do not exist in any cache file (`rate_60` has no host, `service_host` no time), so the stage runs one DuckDB aggregate over `events` (`bin, host, level, program, n`).
- **Split and targets** (`split_bounds`, `period_index`, `targets`, `sample_mask`, `positive_rates`) live in this module as the single definition; later stories import them from `logdash.analyses.forecast`. The stage stores the positive rates and period boundaries in `meta.json`, so the page reads only `meta.json` and a 1,439-row parquet file.
- **Extra stored file** `spike_counts.parquet` (`bin, hosts`: hosts in a spike minute, one row per minute) feeds the chart, so the callback does not read the 233 k-row frame.

Rejected:
- *Calling `volume.episodes()` and undoing `merge_periodic`*: it also returns drops and gaps and builds evidence strings; the contract names `find_bursts` directly. The test compares against the detector instead (AC3).
- *Computing the positive rates in the layout from `frame.parquet`*: cheap enough, but precompute is the project convention and keeps the layout to a JSON read.
- *Sorting the frame by bin then host*: makes per-host windows (next stories) need a sort or a gather.
- *Column names by index (`svc_00`)*: names carrying the template id and service name are readable in later coefficient tables and need no lookup; meta still lists what is behind each column.

### 5. Steps

#### Commit 1: synthetic data for forecasting (steps 1 to 2)

**Step 1. `tests/synthetic.py`.**
- New module constants, after `CONTEXT_TEMPLATE`:
  ```python
  # Spikes to forecast (``SyntheticSpec.forecast_spikes``): each one is preceded by a short burst
  # of a WARNING template, too small to be a spike itself, so a model can see it coming. They skip
  # the silent host (a spike inside its gap would make the precursor a spike too) and the spike
  # host (one of them would land on its spike at 30 % of the span).
  FORECAST_HOSTS = [h for h in HOSTS if h not in (SILENT_HOST, SPIKE_HOST)]
  FORECAST_SERVICE = SERVICES[3]  # libvirt: one template, the spike lines use it
  PRECURSOR_TEMPLATE = "Memory pressure on hypervisor: {n} pages reclaimed in the last interval"
  FORECAST_SPIKE_ROWS = 600  # over two minutes: about 300 lines per minute
  PRECURSOR_RATE = 8  # WARNING lines in each precursor minute
  FORECAST_JITTER = 3  # minutes, either way
  ```
- `SyntheticSpec`: add, with a comment like the context fields,
  `forecast_spikes: int = 0` and `precursor_minutes: int = 3`.
- `GroundTruth`: add `forecast_onsets: list[datetime] = field(default_factory=list)` (time-zone aware like the other truth times, whole minutes) and `forecast_hosts: list[str] = field(default_factory=list)` (host names as they appear in the cache, i.e. without the DNS domain: ingest strips it, `ingest/normalize.py:50`).
- `generate_rows`: insert this block directly after the ERROR-burst block (after line 277, before `request_service = ...`). Keep this exact order of random draws; it was checked on seed 7 (all 25 episodes found exactly at onset and onset + 1 min, highest precursor minute 34 lines against a threshold of about 36):
  ```python
  # Spikes to forecast, each after a short WARNING precursor on the same host. Onsets fall on
  # whole minutes so a spike fills exactly two 1-minute bins. A separate generator keeps the rest
  # of the data as it was.
  fc = random.Random(spec.seed + 5000)
  for i in range(spec.forecast_spikes):
      at = START + (end - START) * (0.10 + 0.85 * (i + 0.5) / spec.forecast_spikes)
      jitter = timedelta(minutes=fc.randint(-FORECAST_JITTER, FORECAST_JITTER))
      onset = at.replace(second=0, microsecond=0) + jitter
      host = FORECAST_HOSTS[i % len(FORECAST_HOSTS)]
      truth.forecast_onsets.append(onset)
      truth.forecast_hosts.append(host.split(".", 1)[0])
      for m in range(spec.precursor_minutes):
          lo = onset - timedelta(minutes=spec.precursor_minutes - m)
          for _ in range(PRECURSOR_RATE):
              ts = _random_time(fc, lo, lo + timedelta(minutes=1))
              row = _row(ts, fc, host, FORECAST_SERVICE, _fill(fc, PRECURSOR_TEMPLATE))
              row["log_level"] = "WARNING"
              rows.append(row)
      for _ in range(FORECAST_SPIKE_ROWS):
          ts = _random_time(fc, onset, onset + timedelta(minutes=2))
          payload = _fill(fc, FORECAST_SERVICE[3][0])
          rows.append(_row(ts, fc, host, FORECAST_SERVICE, payload))
  ```
  With `forecast_spikes=0` the loop body never runs and `fc` draws nothing, so the output is unchanged.

**Step 2. `tests/unit/test_synthetic.py`**: tests listed in section 6 (T1, T2). The reference hash in T1 was computed on `origin/main` at `be3ef10`: `0baa89decad8b682c4c422195dcc3cd01621a8b1bf611a3e3ef6a087528b2f49` (sha256 of `rows_to_csv(generate_rows(SyntheticSpec())[0])`, stable across `PYTHONHASHSEED`).

#### Commit 2: the `forecast` stage (steps 3 to 5)

**Step 3. `src/logdash/analyses/forecast.py`** (new). Module docstring: what the stage stores (the frame layout, meta keys, `spike_counts.parquet`) and the definitions of spike, target and split, in the style of `hosts.py:1`. Imports: `analyses.temporal` (`KEY`, `MISSING`, `span`, `find_bursts`, `rolling_baseline`), `analyses.templates` (`KEY`, `short`), `analyses.hosts` (`KEY`), `ingest.normalize.LEVELS`.

Constants:
```python
KEY = "forecast"
STEP_S = 60
TOP_TEMPLATES = 20
TOP_SERVICES = 20
HORIZONS = (1, 5, 15)  # minutes, shown on the page
GROUPS = ("templates", "levels", "services", "peers", "lags")  # checkbox values of later stories
PERIODS = ("train", "validation", "test")
TEMPLATE_TEXT = 80  # characters of a template's text kept in meta.json
PERIOD_COLORS = {"train": figures.PALETTE[0], "validation": figures.PALETTE[3], "test": figures.PALETTE[1]}
HOST_MIX_SQL = f"""
    SELECT time_bucket(INTERVAL '1 minute', ts) AS bin, coalesce(host, '{MISSING}') AS host,
           level, coalesce(program, '{MISSING}') AS program, count(*)::BIGINT AS n
    FROM events GROUP BY ALL"""
```
Column names (stable; later stories rely on them):
- templates: `tpl_<template_id>` (e.g. `tpl_1`), in rank order;
- levels: `lvl_<level.lower()>` for each of `LEVELS`, in `LEVELS` order (`lvl_debug` ... `lvl_unknown`);
- services: `svc_<service name>` (e.g. `svc_neutron-l3-agent`), in rank order;
- peers: `peer_rate`;
- lags: `lag_rate`, `lag_z`.

Data and functions (all typed, mypy-clean):

```python
@dataclass(frozen=True)
class Sources:
    """Upstream tables the stage reads (one place, so tests can hand-make them)."""
    host_60: pl.DataFrame          # temporal/host_60.parquet: bin, host, n
    rate_bins: pl.Series           # temporal/rate_60.parquet["bin"] (for the span, as the volume detector)
    template_counts: pl.DataFrame  # templates/template_counts_host.parquet: template_id, bin, host, n
    template_texts: pl.DataFrame   # templates/templates.parquet: template_id, template
    host_mix: pl.DataFrame         # HOST_MIX_SQL over events: bin, host, level, program, n
    peers: pl.DataFrame            # hosts/peers.parquet: host, peer, distance

def load_sources(con: duckdb.DuckDBPyConnection, store: CacheStore) -> Sources
```
Read with `store.read_parquet(<KEY of the module>, name)`; `host_mix` with `con.execute(HOST_MIX_SQL).pl()`.

```python
def naive_bins(df: pl.DataFrame) -> pl.DataFrame
```
Returns `df` with `bin` cast to naive UTC `Datetime("us")`: if the dtype has a time zone, `convert_time_zone("UTC").dt.replace_time_zone(None)`, then `.cast(pl.Datetime("us"))`. Apply to every source table in `build` (the events query and the parquet files are naive today; this guards a DuckDB `TIMESTAMPTZ` change).

```python
def span_minutes(sources: Sources) -> tuple[datetime | None, int]
```
`(t0, minutes)` exactly as the volume detector: `t0, t1 = temporal.span(pl.concat([rate_bins, host_60["bin"]]))`, `minutes = int((t1 - t0).total_seconds() // STEP_S) + 1`; `(None, 0)` when `host_60` is empty.

```python
def frame_hosts(host_60: pl.DataFrame) -> list[str]
```
Sorted unique hosts of `host_60` other than `MISSING`.

```python
def dense_cube(df: pl.DataFrame, key: str | None, keys: Sequence[Any], hosts: Sequence[str],
               t0: datetime, minutes: int) -> np.ndarray
```
Float64 array `(len(keys), len(hosts), minutes)` (with `key=None`: `(1, hosts, minutes)`, all rows count) of `df["n"]` summed by key, host and minute index `(bin - t0) // 60 s`. Rows whose key or host is not in the lists, or whose minute index is outside `[0, minutes)`, are left out. Map with `replace_strict(..., default=None)` and drop nulls, then `np.add.at`.

```python
def spike_mask(x: np.ndarray, burst_k: float) -> tuple[np.ndarray, int]
```
Boolean mask of minutes inside an episode of `find_bursts(x, burst_k)` (inclusive `first..last`) and the episode count.

```python
def lag_features(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]
```
`(np.log1p(x), (x - median) / scale)` with `median, scale = rolling_baseline(x, trailing=True)`. Docstring: the window ends at the minute, so no future minute enters; `scale >= 1`, so no division by zero.

```python
def peer_rates(rates: np.ndarray, hosts: Sequence[str], peers: pl.DataFrame) -> tuple[np.ndarray, list[str]]
```
`rates` is `(hosts, minutes)` of total lines. For each host, the mean of `rates` over its peers in `peers` that are in `hosts`; when a host has none (not in `peers.parquet`, or none of its peers present), `np.median(rates, axis=0)` (the fleet median of that minute, over all frame hosts, empty minutes included). Returns the raw (not log) array and the sorted list of hosts that got the fleet median.

```python
def top_templates(counts: pl.DataFrame, texts: pl.DataFrame, n: int = TOP_TEMPLATES) -> pl.DataFrame
```
`template_id, lines, text`: templates ranked by summed `n` over rows whose host is not `MISSING`, descending, ties by `template_id` ascending, first `n`; `text = templates.short(template, TEMPLATE_TEXT)` (empty string when the id has no text).

```python
def top_services(host_mix: pl.DataFrame, n: int = TOP_SERVICES) -> pl.DataFrame
```
`service, lines`: programs ranked by summed `n` over rows whose host and program are not `MISSING`, descending, ties by name ascending, first `n`.

Split and targets (the shared definitions for later stories):
```python
def split_bounds(minutes: int) -> tuple[int, int]:
    """End (exclusive) minute index of train and of validation: 7/12 and 8/12 of the span."""
    return minutes * 7 // 12, minutes * 8 // 12

def period_index(minutes: int) -> np.ndarray:
    """0 (train), 1 (validation) or 2 (test) for every minute index."""

def targets(spike: np.ndarray, horizon: int) -> np.ndarray:
    """y[..., t] = any(spike[..., t+1 : t+1+horizon]); False where that window passes the end."""
    # cumulative sum along the last axis with a leading 0; works for 1-D and (hosts, minutes)

def sample_mask(minutes: int, horizon: int) -> np.ndarray:
    """True where t + horizon is inside the span and in the same period as t."""

def positive_rates(spike: np.ndarray, horizons: Sequence[int] = HORIZONS) -> dict[str, dict[str, dict[str, Any]]]:
    """{str(h): {period: {"samples": int, "positives": int, "rate": float | None}}} over all hosts."""
```
`samples` = kept minutes in the period × hosts; `rate = positives / samples`, `None` when `samples == 0` (keeps `meta.json` strict JSON).

```python
def build(sources: Sources, burst_k: float) -> tuple[pl.DataFrame, dict[str, Any], pl.DataFrame]
```
Returns `(frame, meta, spike_counts)`:
1. `naive_bins` on all tables; `t0, minutes = span_minutes(...)`; `hosts = frame_hosts(...)`. If `minutes == 0` or no hosts: return an empty frame with schema `bin: Datetime("us"), host: String, spike: Boolean`, `meta = empty_meta(burst_k)` (same keys as below, counts 0, empty lists, `start`/`end` `None`, `split` `{"train_end": 0, "validation_end": 0, "periods": {}}`, `positive_rate` from `positive_rates(np.zeros((0, 0), bool))`) and an empty `spike_counts` (`bin: Datetime("us"), hosts: Int64`).
2. `rates = dense_cube(host_60, None, [None], hosts, ...)[0]` (`host_60` has no key column: pass `key=None`).
3. Spike: `spike_mask` per host row → `spike (hosts, minutes)`; sum the episode counts.
4. Templates: `top = top_templates(...)`; cube with `key="template_id"`, `keys=top["template_id"]`; `np.log1p`.
5. Levels: `host_mix` grouped by `bin, host, level` → cube over `LEVELS`; `total = cube.sum(axis=0)`; `share = np.divide(cube, total, out=np.zeros_like(cube), where=total > 0)`.
6. Services: `svc = top_services(host_mix)`; `host_mix` filtered to those programs, grouped by `bin, host, program` → cube; `np.log1p`.
7. Peers: `raw, no_peer = peer_rates(rates, hosts, peers)`; `np.log1p(raw)`.
8. Lags: `lag_features(rates[i])` per host.
9. Frame: `bin` = `np.tile(np.datetime64(t0, "us") + np.arange(minutes) * np.timedelta64(STEP_S, "s"), len(hosts))` as `Datetime("us")`, `host = np.repeat(hosts, minutes)`, `spike = spike.reshape(-1)`, then every feature column as `arr.reshape(-1).astype(np.float32)` in the order templates, levels, services, peers, lags. Before returning, check `np.isfinite` on all feature values and raise `ValueError("forecast: non-finite feature values in <column>")` if any (guards the "no NaN" contract instead of writing a bad file).
10. `spike_counts`: `bin` (each minute once), `hosts = spike.sum(axis=0)` as Int64.
11. `meta` (all values JSON-safe; datetimes as `isoformat()` strings, naive UTC):
    ```
    start, end (exclusive: t0 + minutes), minutes, hosts (count), host_minutes, burst_k,
    groups: {group: [column, ...]} for every name in GROUPS,
    templates: [{"column", "template_id", "text", "lines"}], services: [{"column", "service", "lines"}],
    episodes, spike_minutes, spike_hosts (hosts with at least one spike minute),
    no_peer_hosts: [host, ...],
    split: {"train_end": a, "validation_end": b, "periods": {period: [start_iso, end_iso]}},
    horizons: [1, 5, 15], positive_rate: positive_rates(spike)
    ```

Page helpers:
```python
def spike_figure(counts: pl.DataFrame, meta: dict[str, Any], theme: str | None) -> go.Figure
```
`figures.empty_figure("No data in the cache", theme)` when `counts` is empty. Otherwise one `go.Scatter(x=bin, y=hosts, mode="lines", line_shape="hv", name="hosts in a spike", hovertemplate="%{x|%H:%M}: %{y} hosts<extra></extra>")`; for each period in `meta["split"]["periods"]` a `fig.add_vrect(x0=start, x1=end, fillcolor=PERIOD_COLORS[p], opacity=0.12, layer="below", line_width=0, annotation_text=p, annotation_position="top left")`; `figures.style(fig, theme, showlegend=False, yaxis_title="hosts in a spike", xaxis_title="time (UTC)")`.

```python
def period_text(meta: dict[str, Any]) -> str
```
`"; "`-joined `f"{period} {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} ({minutes} min)"` for each period, followed by `" (UTC)."`. Empty string when there are no periods.

```python
def rate_table(meta: dict[str, Any]) -> pl.DataFrame
```
Columns `horizon, train, validation, test`; one row per `meta["horizons"]`, `horizon = f"{h} min"`, cells `f"{100 * rate:.2f} %"` or `"-"` when `rate is None`.

Page text constants (exact):
```python
DESCRIPTION = (
    "What a spike forecast learns from: the spike label and the input features of every host and "
    "minute. A spike minute lies inside a volume spike of its host (the volume detector's "
    "episodes, before periodic repeats are merged). The forecasting lab always uses every host and "
    "the whole span: the global filters do not apply on this page. No model is trained yet."
)
RATE_NOTE = (
    "Share of samples whose next H minutes contain a spike minute of the host. Samples whose "
    "target window reaches into the next period are dropped."
)
```

```python
@register
class Forecast:
    key = KEY
    title = "Forecasting"
    version = "1"
    depends_on: tuple[str, ...] = ("temporal", "templates", "hosts")
    setting_names: tuple[str, ...] = ("burst_k",)

    def compute(self, ctx: ComputeContext) -> None:
        store = CacheStore(ctx.out_dir.parent, ctx.settings)
        frame, meta, counts = build(load_sources(ctx.con, store), ctx.settings.burst_k)
        store.write_parquet(KEY, "frame.parquet", frame)
        store.write_parquet(KEY, "spike_counts.parquet", counts)
        store.write_json(KEY, "meta.json", meta)
        ctx.note_rows("episodes", meta["episodes"])
        ctx.note_rows("spike_minutes", meta["spike_minutes"])
        ctx.progress(f"{meta['host_minutes']} host-minutes, {meta['episodes']} spike episodes on {meta['spike_hosts']} hosts")

    def layout(self, store: CacheStore, **_query: Any) -> Component: ...
    def register_callbacks(self, app: Dash, store: CacheStore) -> None: ...
```
`layout` reads only `meta.json`:
```python
ui.page(
    "Forecasting",
    ui.grid([ui.stat_card("host-minutes", ui.fmt_int(meta["host_minutes"])),
             ui.stat_card("hosts", ui.fmt_int(meta["hosts"])),
             ui.stat_card("spike episodes", ui.fmt_int(meta["episodes"])),
             ui.stat_card("spike minutes", ui.fmt_int(meta["spike_minutes"]))],
            cols={"base": 2, "md": 4}),
    ui.card("Spike minutes over time (hosts in a spike, per minute)",
            ui.graph("forecast-spikes", 320),
            dmc.Text(period_text(meta), size="sm", c="dimmed")),
    ui.card("Positive rate per prediction horizon",
            dmc.Text(RATE_NOTE, size="sm", c="dimmed", mb="xs"),
            ui.table(rate_table(meta))),
    description=DESCRIPTION,
)
```
`register_callbacks`: one callback
```python
@app.callback(Output("forecast-spikes", "figure"), Input(THEME_STORE_ID, "data"))
def draw_spikes(theme: str | None) -> go.Figure:
    return spike_figure(store.read_parquet(KEY, "spike_counts.parquet"), store.read_json(KEY, "meta.json"), theme)
```
The function name `draw_spikes` is what the benchmark looks up. No `FILTER_STORE_ID` input (filters do not apply).

**Step 4. `src/logdash/analyses/__init__.py`**: add `"forecast"` to `BUILTIN` directly after `"hosts"`.

**Step 5. `tests/unit/test_forecast.py`** (new): tests T3 to T14 below. Module fixture:
```python
SPEC = SyntheticSpec(forecast_spikes=24, error_rows=0)

@pytest.fixture(scope="module")
def forecast_cache(tmp_path_factory):
    """A cache of a dataset of its own: injected spikes with precursors (the shared data has none)."""
    data_dir = tmp_path_factory.mktemp("forecast_data")
    truth = write_dataset(data_dir / "synthetic_preprocessed.csv.zst", SPEC)
    settings = Settings(data_dir, tmp_path_factory.mktemp("forecast_cache"), "1GB", 2)
    run_build(settings, registry=Registry(), log=lambda _: None)
    built = Built(settings, CacheStore(settings.cache_dir, settings))
    registry = build_with(built, Temporal(), Templates(), Hosts(), Forecast())
    return built, registry, truth
```
`error_rows=0` because the ERROR burst (600 lines in 2 minutes on `HOSTS[5]`) is itself a host volume spike, which would break the "no other spike minute" check; the comment says so.

#### Commit 3: benchmark (step 6)

**Step 6. `tests/benchmark/test_latency.py`**: add after `test_hosts_cold`:
```python
def test_forecast(store: CacheStore) -> None:
    fns = _recorded(store, "forecast")
    for theme in ("light", "dark"):
        measure("forecast.draw_spikes", theme, CALLBACK_BUDGET_S,
                lambda t=theme: fns["draw_spikes"](t))  # type: ignore[misc]
```
`test_layouts` covers `forecast.layout` already. To run it: link the real cache (protocol), then `LOGDASH_THREADS=4 uv run logdash build --only forecast` (it builds only stale stages; `forecast` adds `cache/forecast/` and its manifest entry), then `uv run pytest -m slow --no-cov -s` and paste the forecast rows of the timing table into the PR description.

#### Commit 4: ADR and README (steps 7 to 8)

**Step 7. `adrs/ADR002-spike-forecasting-lab.md`** (Status: accepted; Context; Decision; Consequences). Context: issue #23 and its split, the 24 h of flat data, about 1 % positives. Decision: copy the contract bullets of #50 (spike label, target, feature groups, stored files, time split, code layout, synthetic data) and add what this plan fixes: the column names; host-major row order; `spike_counts.parquet`; the extra meta keys (`split`, `horizons`, `positive_rate`, `spike_hosts`, `spike_minutes`, `templates[].lines`, `services[].lines`); that `split_bounds`, `period_index`, `targets`, `sample_mask` and `positive_rates` in `logdash.analyses.forecast` are the one definition later stories use; that the fleet median for hosts without peers is taken over all frame hosts including empty minutes; that synthetic forecast spikes avoid the silent and the spike host and start on whole minutes. State the measured real-cache numbers (162 hosts, 1,439 minutes, 233,118 host-minutes, 270 episodes on 16 hosts, 1,059 spike minutes) and note that the 271/17/1,079 of the split comment counted the `(missing)` host. Consequences: one `burst_k` change rebuilds the stage; the frame is about 50 Float32 columns × 233 k rows; a change to `find_bursts` must bump `Forecast.version` as well as `Volume.version`; filters never apply to the lab.

**Step 8. `README.md`**: new section `## Forecasting lab` after `## Configuration`:
> The **Forecasting** page (`/forecast`) is built by the `forecast` stage (after `hosts`; rebuilt when `LOGDASH_BURST_K` changes). The stage writes `cache/forecast/frame.parquet`, one row per host and minute with the spike label and five feature groups (templates, levels, services, peers, lags), and `meta.json`. The page shows the spike minutes over time with the train, validation and test periods (first 7/12, next 1/12, last 1/3 of the span) and the share of positive samples for 1, 5 and 15 minute horizons. Global filters do not apply. Design: [ADR002](adrs/ADR002-spike-forecasting-lab.md).

Also add `forecast` to the benchmark sentence in `## Tests` ("... anomaly types and sorts, forecast chart) ...").

The implementer also commits this plan as `plans/2026-10-23-spike-labels-features-v1.md` (protocol).

### 6. Tests

`tests/unit/test_synthetic.py`:
- **T1** `test_forecast_spikes_off_leaves_the_data_unchanged`: sha256 of `rows_to_csv(generate_rows(SyntheticSpec())[0])` equals the hash in step 2. Proves the byte-for-byte clause of the contract.
- **T2** `test_forecast_spikes_and_precursors_are_injected`: `generate_rows(SyntheticSpec(forecast_spikes=24, error_rows=0))`: 24 onsets, sorted, each with `second == microsecond == 0`; hosts equal `[h.split(".", 1)[0] for h in FORECAST_HOSTS] * 6` in order; rows whose payload starts with `"Memory pressure on hypervisor"` number `24 * 3 * PRECURSOR_RATE`, all with `log_level == "WARNING"`.

`tests/unit/test_forecast.py` (pure functions, no cache):
- **T3** `test_split_bounds_and_periods`: `split_bounds(1440) == (840, 960)`, `split_bounds(1439) == (839, 959)`, `split_bounds(12) == (7, 8)`; `period_index(12).tolist() == [0]*7 + [1] + [2]*4`.
- **T4** `test_targets_look_only_ahead`: `targets(np.array([0,0,0,1,0,0], bool), 1).tolist() == [F,F,T,F,F,F]`, horizon 2 gives `[F,T,T,F,F,F]`; a minute in a spike with no spike after it is `False`; works on a `(2, n)` array row by row.
- **T5** `test_sample_mask_drops_windows_that_cross_a_period`: `sample_mask(12, 1)` is False exactly at 6, 7 and 11; `sample_mask(12, 2)` False at 5, 6, 7, 10, 11.
- **T6** `test_positive_rates_count_kept_samples`: a hand-made `(2, 24)` spike array (`split_bounds(24) == (14, 16)`); check samples, positives and rate for horizon 1 in each period (validation keeps only minute 14, so 2 samples), and `rate is None` with `samples == 0` for validation at horizon 2 (minute 14 + 2 is in test).
- **T7** `test_lag_z_ignores_the_future` (AC5): build `Sources` from a hand-made `host_60` (3 hosts, 300 minutes, Poisson 20) with empty template/mix/peer tables of the right schema; call `build` twice, the second time with the host_60 counts after minute `t` replaced (×10 and new random values) for `t in (0, 60, 150, 298)`; `lag_z` (and `lag_rate`) of every host at minutes `<= t` are equal. Also `lag_features` directly on an array for the same `t` values.
- **T8** `test_peer_rates_use_peers_or_the_fleet_median`: frame hosts `a, b, c, d`; `peers` rows `(a, b), (a, c), (b, e), (c, a)` (host `e` is not in the frame, `d` has no row); check a's rate is the mean of b and c, c's equals a's, b (its only peer is absent) and d get the per-minute median over all four hosts, and the returned list is `["b", "d"]`.
- **T9** `test_levels_shares_and_top_lists`: hand-made `host_mix`: shares in a busy minute sum to 1 (within 1e-6), an empty minute gives 0 for every level; `top_services` leaves out `(missing)` programs and hosts and breaks ties by name; `top_templates` ignores the `(missing)` host and breaks ties by id.
- **T10** `test_build_on_empty_sources`: empty `host_60` gives an empty frame with `bin, host, spike`, `meta["host_minutes"] == 0`, `json.dumps(meta, allow_nan=False)` works, and `spike_figure(empty, meta, "dark")` is the empty figure.

`tests/unit/test_forecast.py` (on `forecast_cache`):
- **T11** `test_injected_spikes_are_spike_minutes` (AC2): with `naive = utc(t).replace(tzinfo=None)`: for each forecast onset, `(host, onset)` and `(host, onset + 1 min)` are spike rows; for the `spike_host` spike, every minute bin overlapping `[spike_start, spike_end)` is a spike row; every spike row lies within one minute of an injected interval of its host (allowed set: from the first bin − 1 min to the last bin + 1 min of each injection); the three precursor bins `onset − 3, − 2, − 1 min` are not spike rows; `meta["episodes"] == 25`.
- **T12** `test_spike_minutes_equal_the_volume_episodes` (AC3): `monkeypatch.setattr(volume, "merge_periodic", lambda found: found)`, run `volume.Volume().detect(DetectContext(store, settings, lambda _: None))`, keep `kind == "host"` rows whose evidence starts with `"spike:"`, expand each to `(entity, bin + k min)` for `k < window_s // 60`; the set equals the frame's `(host, bin)` where `spike`, and the row count equals `meta["episodes"]`. (Use a function-scoped `monkeypatch` inside the test; the fixture is module-scoped and unaffected.)
- **T13** `test_frame_shape_and_columns` (AC4): `frame.height == meta["hosts"] * meta["minutes"] == meta["host_minutes"]`; `(host, bin)` unique; `MISSING` not among hosts; `frame.null_count()` all zero; every feature column has no NaN and dtype `Float32`; `set(frame.columns) - {"bin", "host", "spike"} == {c for cols in meta["groups"].values() for c in cols}`; `list(meta["groups"]) == list(GROUPS)`; rows ordered host-major (each host's block has strictly increasing bins); a precursor template column exists (some `meta["templates"][i]["text"]` starts with `"Memory pressure"`) and is positive at the precursor minutes of the first onset; `json.dumps(meta, allow_nan=False)` works.
- **T14** `test_stage_order_and_staleness` (AC1): after `load_builtin()`, `BUILTIN.index("forecast") == BUILTIN.index("hosts") + 1` and in `[a.key for a in REGISTRY.ordered()]` `forecast` comes after `hosts`; on the fixture, `stage_statuses(settings, registry)` lists `forecast` as `fresh`, and with `dataclasses.replace(settings, burst_k=settings.burst_k + 1)` as `stale` while `templates` stays `fresh`.
- **T15** `test_page_layout_and_themes` (AC6): `str(Forecast().layout(store))` contains `"forecast-spikes"`, `ui.fmt_int(meta["host_minutes"])`, `"spike episodes"`, `"the global filters do not apply"`, `"Positive rate per prediction horizon"`, `"15 min"`; `rate_table(meta)` has 3 rows and columns `horizon, train, validation, test` with `%` cells; `spike_figure(...)` for `"light"` and `"dark"` has 3 shapes (one per period) and their `layout.template.layout.font.color` differ; `period_text(meta)` names the three periods.
- **T16** `test_callback_over_http`: `create_app(store, registry)`; `GET /forecast` is 200; POST `/_dash-update-component` with output `forecast-spikes.figure`, input `theme.data = "dark"` (the single-output body of `tests/unit/test_app.py:87`) returns 200 and a figure with data.

Manual check (AC7): `uv run pytest -m slow --no-cov -s` on the real cache, `forecast.layout` under 1 s and `forecast.draw_spikes` under 300 ms in both themes (step 6). Also open `/forecast` in `logdash serve --no-build` and toggle the theme once.

Coverage: AC9 by the full gate.

### 7. Risks

- **Labels drift from the volume detector.** Same span (`rate_60` + `host_60` bins), same dense series, same `find_bursts` call. T12 compares against the detector itself. A future change to `find_bursts` must bump both versions (ADR consequence).
- **Precursor accidentally a spike.** On seed 7 the highest precursor minute is 34 lines against a threshold of about 36, so the margin is small but deterministic; T11 fails loudly if the generator changes. Do not change the draw order in step 1. If T11 fails on the precursor clause, report it on the issue rather than retuning constants silently.
- **Synthetic data of other tests changes.** T1 pins the default dataset by hash; the forecast block uses its own generator.
- **Host names.** Ingest strips `.xerces.lan`; truth hosts are stored stripped (T2), otherwise `eselda13u21s06` looks like a spike on a host without injections.
- **Future leakage.** Only `lag_z` uses a rolling window; trailing only (T7). Other features are same-minute values. The fleet median and peer mean are same-minute cross-host values, not future ones.
- **NaN or nulls.** log1p of non-negative counts, shares with `where=total > 0`, `scale >= 1`; `build` raises on any non-finite value; empty minutes are zeros by construction of the dense cubes (T13).
- **Hosts without peers** (23 of 162 on the real cache): fleet median, listed in `meta["no_peer_hosts"]` (T8).
- **`(missing)` host and program.** Left out of the frame, the template ranking and the service ranking (T9, T13).
- **UTC.** All bins naive UTC (`naive_bins` guards a tz-aware source); onsets in truth are converted with `utc(...).replace(tzinfo=None)` in tests; the page labels times as UTC.
- **Build time and memory on real data.** One 2.2 s events aggregate; cubes of at most 20 × 162 × 1,439 float64 (37 MB) at a time; frame about 47 MB in memory. No per-row Python loops (only per-host loops over 162 hosts).
- **Latency.** The layout reads one small JSON file; the callback reads a 1,439-row parquet file and draws one trace plus 3 shapes. Benchmarked in step 6.
- **Shared real cache.** `logdash build --only forecast` writes `cache/forecast/` into the linked main cache. It does not touch other stages unless they are stale.
- **Empty cache.** `build` returns an empty frame and meta; the page shows the empty figure and `-` cells (T10).
- **Other tests building every built-in stage** (`test_serve_cli_runs_app` builds the global registry on the shared dataset) now also build `forecast` there; the shared data has no special shape requirements for it, and `build` works on any data.

### 8. Docs

- `adrs/ADR002-spike-forecasting-lab.md` (step 7): binds #51 to #54.
- `README.md` section `## Forecasting lab` and the benchmark sentence (step 8).
- No new configuration variable.

### 9. Open questions (non-blocking, defaults used)

1. **Episode numbers in the contract** (271 on 17 hosts) include the `(missing)` host, which the contract drops. Default: follow the rule (drop it), record 270 on 16 hosts in the ADR.
2. **Forecast spike placement.** The contract says "a host chosen round-robin". Default: round-robin over `FORECAST_HOSTS` (all hosts except the silent host and the spike host), onsets on whole minutes, so the label test is exact.
3. **Forecast dataset sets `error_rows=0`.** The ERROR burst is a host spike of its own; without it "every spike is an injected one" holds.
4. **Extra outputs** beyond the contract (`spike_counts.parquet`, the `split`/`horizons`/`positive_rate` meta keys) and the column naming scheme. Default as in step 3; the ADR records them.
5. **Where the split helpers live.** Default: `logdash.analyses.forecast`; the `logdash.forecast` package of later stories imports them.
