### 1. Goal

On the Forecasting page the user picks a prediction horizon, presses **Run**, watches a progress bar, can cancel, and then reads accuracy, F1 (at a threshold slider), AUROC, PR-AUC and the positive rate of the persistence baseline on the test period. The run is a Dash background callback in a separate process. The job, the run files, the samples, the metrics and the results table are built so that #60 (logistic regression), #52 (GRU) and #54 (placement) extend them instead of replacing them.

Acceptance criteria (from the issue):
- [ ] A horizon slider (1 to 15 min, default 5).
- [ ] Run starts a background job; a progress bar advances per stage (labels, scoring); Cancel stops the job and the page says the run was cancelled; Run is disabled while a job runs.
- [ ] A results table shows, for each model in the run (here: the persistence baseline), accuracy, F1, AUROC, PR-AUC and the test positive rate, with a note that with about 1 % positives accuracy is misleading and PR-AUC and F1 are the numbers to read. Moving the threshold slider updates F1 and accuracy without rerunning.
- [ ] Unit tests cover the time split and targets (samples crossing a period boundary are dropped), the persistence baseline on a hand-made frame, the metrics including the no-positives case, and the job function called directly with a fake `set_progress`.
- [ ] A test asserts the Run callback is registered with `background=True` and a cancel input.
- [ ] The latency benchmark covers the page layout and the threshold callback.
- [ ] `LOGDASH_RUNS` is in `config.py`, the README table and `.gitignore`.
- [ ] The quality gate passes with coverage at or above 90 %.

### 2. Depends on

#50 (Done, merged as `9d57aa6`). This plan is written against `origin/main` at `77b734b` (the commits after `9d57aa6` change only `.claude/`).

### 3. Context

- `src/logdash/analyses/forecast.py` (from #50): the stage and the page.
  - `:59` constants `KEY = "forecast"`, `PERIODS = ("train", "validation", "test")`.
  - `:243` `split_bounds`, `:248` `period_index`, `:257` `targets(spike, horizon)` (`y[..., t] = any(spike[..., t+1 : t+1+H])`, False past the end), `:268` `sample_mask(minutes, horizon)` (True where `t + H` is inside the span and in the same period as `t`). ADR003 says these are the one definition: import them, do not copy them.
  - `:77` `DESCRIPTION` (ends with "No model is trained yet."), `:521` `layout` (reads only `meta.json`), `:547` `register_callbacks` (one callback `draw_spikes`).
  - `cache/forecast/frame.parquet`: `bin, host, spike, <features>`, host-major (sorted hosts, then minutes), exactly `meta["hosts"] * meta["minutes"]` rows. `meta.json` keys used here: `start`, `end`, `minutes`, `hosts`, `burst_k`, `split.periods.test` (`[start_iso, end_iso]`).
- `adrs/ADR003-spike-forecasting-lab.md`: the contract of the lab (the issue still calls it ADR002; the file is ADR003). Learning code goes in a new package `src/logdash/forecast/`.
- `src/logdash/config.py:45` `Settings` (frozen dataclass, fields are also passed positionally: `Settings(data_dir, cache_dir, "1GB", 2)` in `tests/conftest.py:47` and elsewhere, so a new field must go **last**). `:75` `from_env`, `:95` `with_overrides` (coerces `data_dir`/`cache_dir` to `Path`).
- `src/logdash/cache/store.py:19` `CacheStore` (`path`, `read_json`; `read_parquet` uses polars, see Risks), `store.settings`.
- `src/logdash/app/main.py:58` `create_app` calls `register_callbacks(app, store)` for every page; `app.callback` writes into `app.callback_map` and `app._callback_list`.
- `src/logdash/app/ui.py`: `page`, `card`, `table` (static `dmc.Table` of a polars frame), `fmt_int`.
- `src/logdash/analyses/features.py:135` `Lru` and `clear_caches()` pattern for memoised loaders.
- Dash 4.4.1 (checked in `.venv`): `app.callback(..., background=True, manager=DiskcacheManager(diskcache.Cache(dir)), progress=[...], progress_default=[...], running=[(Output, on, off), ...], cancel=[Input(...)], interval=ms, prevent_initial_call=True)`. The decorated function gets `set_progress` as its first argument; `set_progress` takes a tuple with one value per progress output. `app.callback_map[<id>]["background"]` holds `progress`, `cancel_inputs` and `manager`; `cancel_inputs` is popped at the first HTTP request (`dash.py:1762`), so a test must read it before any request. `running` is stored on the spec in `app._callback_list` as `{"running": {"<id>.<prop>": on}, "runningOff": {...}}`. The job runs in a `multiprocess.Process` (fork on Linux); Cancel kills it with psutil. `progress_default` sets the progress outputs whenever no job runs (default: `None`).
- `tests/conftest.py:59` autouse `_no_model_network` (function scope; patches `config.DOTENV_PATH`). `tests/unit/test_forecast.py:268` module fixture `forecast_cache` (`SyntheticSpec(forecast_spikes=24, error_rows=0)`: 6 hosts × 361 minutes, test period minutes 240 to 360).
- `tests/benchmark/test_latency.py:39` `Recorder`, `:103` `_recorded`, `:65` `measure`, `:91` `test_layouts` (already times every page layout, so the extended Forecasting layout is covered), `:326` `test_forecast`.
- Notes on this issue from the #52 and #54 planners, and their plans, rely on: one job function callable with a fake `set_progress`; one progress helper; a `running` list later stories append to; run files with one label array and one score array per model key and per-model metrics with `samples` and `positives`; an atomic run write; a memoised loader that returns "no run" for a missing directory; a results table built from the models in the run; test samples that are the full grid of hosts × kept test minutes; room for extra `.npz` arrays and `.json` keys before the single write; an importable `MODEL_LABELS`; a reusable real-cache run fixture in the benchmark. This plan provides all of them (names below).

**Measured during planning:** a `multiprocessing` fork child that calls polars after the parent process has used polars hangs (rayon's thread pool does not survive `fork`); the same child reading the file with pyarrow finishes in 0.04 s. The dashboard process uses polars in every callback, so **the job must not call polars or DuckDB**. This shapes the data loader (Design, Risks, ADR004).

### 4. Design

**Package `src/logdash/forecast/`** (new; `__init__.py` holds only a docstring, no imports):

| Module | Holds | Imports from logdash |
|---|---|---|
| `params.py` | `RunParams`, horizon constants | none |
| `data.py` | `SpikeGrid`, `load_spikes` (pyarrow), `Samples`, `period_samples` | `analyses.forecast` (split and target helpers) |
| `baselines.py` | `PERSISTENCE`, `persistence_scores` | `forecast.data` |
| `metrics.py` | `score_metrics`, `threshold_metrics` | none |
| `runs.py` | run ids, atomic `write_run`, memoised `load_run`, `Run`, `cleanup_partial` | none |
| `job.py` | `STAGES`, `progress_value`, `run_forecast_job` | all of the above, `cache.store` |

The page module `analyses/forecast.py` imports `params`, `metrics` and `runs` at the top. It imports `job` **inside** `register_callbacks`, because `job` → `data` → `analyses.forecast` would otherwise be an import cycle.

**Job flow** (`run_forecast_job`): stage `labels` (read the spike column, build the test samples), stage `scoring` (persistence scores and their threshold-free metrics), then "Saving the run" (one atomic write of the run directory), return the run id. The page stores only the run id (`dcc.Store`, session storage). An ordinary callback `show_results` loads the run through the memoised loader and builds the table at the slider threshold: accuracy and F1 are computed from the stored labels and scores on every slider move (77 k samples on the real cache: well under 1 ms); AUROC, PR-AUC and the positive rate come from `run.json`.

**Status text.** The background callback's own outputs are the run-id store and a message ("Run … finished"). A second, ordinary callback on the Run and Cancel clicks writes the same message component (`allow_duplicate=True`): "Run started." on Run, "The run was cancelled. No results were saved." on Cancel. Cancel is enabled only while a job runs, so a Cancel click always means a cancelled job. The progress bar and its label have `progress_default=[0, ""]`, so they reset when a job ends or is cancelled.

**Run directory** `<runs_dir>/<run_id>/`, written once and never changed:
- `scores.npz` (`np.savez_compressed`): `labels` (bool), `host` (int32 host index), `minute` (int32 minute index from the span start), and `score_<model>` (float32) per model key, all in the order of the test samples. Later stories add arrays (#54's `placement_*`) through the same `arrays` mapping.
- `run.json`: `run_id`, `created` (UTC ISO), `settings` (`dataclasses.asdict(RunParams)`), `data` (`start`, `end`, `minutes`, `burst_k` from `meta.json`), `hosts` (host names in index order), `test_period` (`[start_iso, end_iso]`), `models` (model keys in table order), `metrics` (`{model: score_metrics(...)}`). Later stories add keys (#52: `history`, `network`).
- Written to `<runs_dir>/.tmp-<run_id>/`, then renamed. A killed job leaves at most a `.tmp-*` directory, which the next job removes when it is older than an hour.
- Run ids `YYYYMMDDTHHMMSSZ-xxxxxx` (UTC time, 6 hex digits). The id comes back from the browser, so `load_run` accepts only ids matching that pattern (no path traversal).

**Size.** Jev proposes Size L, and the plan touches more than three modules (about 550 lines of code and 450 of tests). It is not split further: this story is already one of #51's two vertical slices, and the parts (job and run files versus the page) give the user nothing on their own. The new modules are small and each has its own tests, so they can be reviewed one commit at a time.

**Rejected:**
- *Splitting into "job without UI" and "page"*: neither half is a vertical slice.
- *Reading `frame.parquet` with `CacheStore.read_parquet` (polars) in the job*: hangs in the forked job process (measured). *Switching multiprocess to `spawn`*: global side effect on Dash, the job closure would have to be pickled, and every job would pay the Dash and logdash import time.
- *Storing the metrics at threshold 0.5 only*: the slider would need a rerun.
- *Keeping results in the `dcc.Store`*: the issue fixes "run id only"; 77 k scores per model would travel to the browser on every update.
- *A `running` entry for the message*: its "off" value would overwrite the job's result message when the job ends.
- *Writing the page's run controls into a new page*: the issue puts them on the Forecasting page.

### 5. Steps

#### Commit 1: dependency and setting (steps 1 to 3)

**Step 1. `pyproject.toml`, `uv.lock`.** Run `uv add "dash[diskcache]>=4.4.1"`. The `dependencies` line becomes `"dash[diskcache]>=4.4.1",`, and the lock gains `diskcache`, `multiprocess` and `psutil`. Check with `uv run python -c "import diskcache, multiprocess, psutil"`.

**Step 2. `src/logdash/config.py`.**
- Constant `DEFAULT_RUNS = "runs"` after `DEFAULT_CACHE`.
- Function (module level, before `Settings`):
  ```python
  def _default_runs_dir() -> Path:
      """``DEFAULT_RUNS`` read at call time, so tests can point it at a temporary directory."""
      return Path(DEFAULT_RUNS)
  ```
- Field, **last** in `Settings` (after `openrouter_api_key`): `runs_dir: Path = field(default_factory=_default_runs_dir)  # forecasting lab runs and their job cache`.
- `from_env`: `runs_dir=Path(env.get("LOGDASH_RUNS", DEFAULT_RUNS)),`.
- `with_overrides`: also coerce `runs_dir` to `Path` (same as `cache_dir`).
- `fingerprint_params` is unchanged: the runs directory never changes a computed result.

**Step 3. `tests/conftest.py`.** Add a session-scoped autouse fixture so that no test writes `./runs`:
```python
@pytest.fixture(scope="session", autouse=True)
def _runs_in_tmp(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Forecast runs and the background-job cache go to a temporary directory, never ./runs.

    Session scope: module and session fixtures build ``Settings`` before function fixtures run.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DEFAULT_RUNS", str(tmp_path_factory.getbasetemp() / "runs"))
    mp.delenv("LOGDASH_RUNS", raising=False)
    yield
    mp.undo()
```
(`from collections.abc import Iterator`.)

Also `.gitignore`: add `/runs/` after `/cache/`.

#### Commit 2: the forecast package (steps 4 to 10)

**Step 4. `src/logdash/forecast/__init__.py`:**
```python
"""Learning code of the forecasting lab (ADR003, ADR004). Runs in the background job process.

Modules here must not call polars or DuckDB on the job's path: the job runs in a forked process
and polars' thread pool does not survive ``fork`` (ADR004).
"""
```

**Step 5. `src/logdash/forecast/params.py`:**
```python
MIN_HORIZON = 1
MAX_HORIZON = 15
DEFAULT_HORIZON = 5  # minutes


@dataclass(frozen=True)
class RunParams:
    """What the user picked for one run; stored as ``run.json["settings"]``.

    Later stories add fields (#60: inputs and lookback) with defaults.
    """

    horizon: int = DEFAULT_HORIZON

    def __post_init__(self) -> None:
        if not MIN_HORIZON <= self.horizon <= MAX_HORIZON:
            raise ValueError(
                f"horizon must be between {MIN_HORIZON} and {MAX_HORIZON} minutes, got {self.horizon}"
            )
```

**Step 6. `src/logdash/forecast/data.py`** (numpy, pyarrow and json only):
```python
import pyarrow.parquet as pq
from logdash.analyses.forecast import KEY, PERIODS, period_index, sample_mask, targets

@dataclass(frozen=True)
class SpikeGrid:
    spike: np.ndarray        # (hosts, minutes) bool
    hosts: list[str]         # index -> host name, as in frame.parquet (sorted)
    meta: dict[str, Any]     # cache/forecast/meta.json

def load_spikes(store: CacheStore) -> SpikeGrid:
```
- `meta = store.read_json(KEY, "meta.json")`, `h, m = int(meta["hosts"]), int(meta["minutes"])`.
- `table = pq.read_table(store.path(KEY, "frame.parquet"), columns=["host", "spike"])`.
- If `table.num_rows != h * m`: `raise ValueError(f"forecast: frame.parquet has {table.num_rows} rows, meta.json says {h} hosts x {m} minutes; rebuild with `logdash build --only forecast`")`.
- `spike = table.column("spike").to_numpy().astype(bool).reshape(h, m)` (host-major per ADR003).
- `hosts = table.column("host").take(np.arange(0, h * m, m)).to_pylist() if h else []`.
- Docstring: never use polars here (ADR004).

```python
@dataclass(frozen=True)
class Samples:
    """(host, t) samples of one period, host-major, with the target y(t)."""
    host: np.ndarray    # int32 host index
    minute: np.ndarray  # int32 minute index t
    labels: np.ndarray  # bool: a spike minute in (t, t+H]

    def __len__(self) -> int: return len(self.labels)

def period_samples(spike: np.ndarray, horizon: int, period: str) -> Samples:
    """Every host at every minute of ``period`` that ``sample_mask`` keeps (the full grid)."""
```
- `hosts, minutes = spike.shape`; `keep = sample_mask(minutes, horizon) & (period_index(minutes) == PERIODS.index(period))`; `ts = np.flatnonzero(keep)`; `host = np.repeat(np.arange(hosts), len(ts))`; `minute = np.tile(ts, hosts)`; `labels = targets(spike, horizon)[host, minute]`; cast `host`, `minute` to int32. Unknown `period` raises `ValueError` (from `PERIODS.index`; let it propagate).

**Step 7. `src/logdash/forecast/baselines.py`:**
```python
PERSISTENCE = "persistence"  # model key in run files and the results table

def persistence_scores(spike: np.ndarray, horizon: int, samples: Samples) -> np.ndarray:
    """Repeat the last value: 1.0 when the host had a spike minute in (t - H, t], else 0.0.

    That is y(t - H), the last fully observed target. The window is clipped at the span start.
    """
```
`cum = np.concatenate([np.zeros((spike.shape[0], 1), np.int64), np.cumsum(spike, axis=1, dtype=np.int64)], axis=1)`; `hi = samples.minute + 1`; `lo = np.maximum(samples.minute + 1 - horizon, 0)`; return `((cum[samples.host, hi] - cum[samples.host, lo]) > 0).astype(np.float32)`.

**Step 8. `src/logdash/forecast/metrics.py`** (numpy and `sklearn.metrics` only):
```python
def score_metrics(labels: np.ndarray, scores: np.ndarray) -> dict[str, Any]:
    """Threshold-free metrics of one model on the test samples (JSON-safe; None when undefined).

    Keys: samples, positives, positive_rate, auroc, pr_auc. AUROC needs both classes, PR-AUC
    (average precision) needs at least one positive.
    """

def threshold_metrics(labels: np.ndarray, scores: np.ndarray, threshold: float) -> dict[str, float | None]:
    """accuracy and f1 when ``scores >= threshold`` predicts a spike.

    accuracy is None without samples; f1 is None without positives (it is undefined there).
    """
```
- `samples = int(len(labels))`, `positives = int(labels.sum())`, `positive_rate = positives / samples if samples else None`.
- `auroc = float(roc_auc_score(labels, scores)) if 0 < positives < samples else None`; `pr_auc = float(average_precision_score(labels, scores)) if positives else None`.
- `pred = scores >= threshold`; `tp = int((pred & labels).sum())`, `fp = int((pred & ~labels).sum())`, `fn = positives - tp`; `accuracy = float((pred == labels).mean()) if samples else None`; `f1 = 2 * tp / (2 * tp + fp + fn) if positives else None`.
- Cast `labels` with `np.asarray(labels, dtype=bool)` at the top of both functions.

**Step 9. `src/logdash/forecast/runs.py`** (stdlib and numpy only):
```python
RUN_FILE = "run.json"
SCORES_FILE = "scores.npz"
JOBS_DIR = "jobs"            # the DiskcacheManager's directory under runs_dir
TMP_PREFIX = ".tmp-"
PARTIAL_MAX_AGE_S = 3600
RUN_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")

def score_key(model: str) -> str: return f"score_{model}"

def new_run_id(now: datetime | None = None) -> str:
    """``YYYYMMDDTHHMMSSZ-xxxxxx``: UTC time and 6 random hex digits."""
    # (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)

def valid_run_id(run_id: object) -> bool:
    return isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None

def write_run(runs_dir: Path, run_id: str, info: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> Path:
    """Write ``<runs_dir>/<run_id>/`` atomically: a temporary directory, then one rename."""
```
- `ValueError(f"invalid run id {run_id!r}")` if not `valid_run_id`. `final = runs_dir / run_id`; `FileExistsError(str(final))` if it exists.
- `tmp = runs_dir / f"{TMP_PREFIX}{run_id}"`; `shutil.rmtree(tmp, ignore_errors=True)`; `tmp.mkdir(parents=True)`.
- In `try`: `np.savez_compressed(tmp / SCORES_FILE, **arrays)`; `(tmp / RUN_FILE).write_text(json.dumps(info, indent=2, allow_nan=False))`; `tmp.rename(final)`. On any exception: `shutil.rmtree(tmp, ignore_errors=True)` and re-raise. Return `final`.

```python
def cleanup_partial(runs_dir: Path, max_age_s: float = PARTIAL_MAX_AGE_S, now: float | None = None) -> None:
    """Remove ``.tmp-*`` directories older than ``max_age_s`` (left by killed jobs)."""
```
No error when `runs_dir` does not exist. Uses `st_mtime`; `now` defaults to `time.time()`.

```python
@dataclass(frozen=True)
class Run:
    run_id: str
    info: dict[str, Any]              # run.json
    arrays: dict[str, np.ndarray]     # every array of scores.npz, materialised

    @property
    def labels(self) -> np.ndarray: return self.arrays["labels"]
    @property
    def models(self) -> list[str]: return list(self.info["models"])
    def scores(self, model: str) -> np.ndarray: return self.arrays[score_key(model)]

def load_run(runs_dir: Path, run_id: str | None) -> Run | None:
    """The run, memoised (runs never change); None when the id is invalid or the run is
    missing or unreadable. Misses are not memoised."""

def clear_caches() -> None
```
- `load_run`: `None` if not `valid_run_id(run_id)`; otherwise `try: return _read(str(runs_dir / run_id)) except (OSError, ValueError, KeyError, zipfile.BadZipFile): return None`.
- `@functools.lru_cache(maxsize=8) def _read(path: str) -> Run`: `info = json.loads(Path(path, RUN_FILE).read_text())`; `with np.load(Path(path, SCORES_FILE), allow_pickle=False) as z: arrays = {k: z[k] for k in z.files}`; check `"labels" in arrays` and every `score_key(m)` for `m in info["models"]` is there (else `raise KeyError(...)`); `return Run(Path(path).name, info, arrays)`. Exceptions are not cached by `lru_cache`.
- `clear_caches` calls `_read.cache_clear()`.

**Step 10. `src/logdash/forecast/job.py`:**
```python
ProgressFn = Callable[[tuple[Any, ...]], None]   # Dash's set_progress: one value per progress output

STAGES: tuple[str, ...] = ("labels", "scoring")   # #60 and #52 insert their stages before "scoring"
STAGE_TEXT = {
    "labels": "Computing targets and the time split",
    "scoring": "Scoring the test period",
}
SAVING_TEXT = "Saving the run"

def progress_value(stage: str, stages: Sequence[str] = STAGES, fraction: float = 0.0) -> tuple[int, str]:
    """(bar value 0..100, label) at the start of ``stage`` plus ``fraction`` of it."""
    i = list(stages).index(stage)      # ValueError for an unknown stage
    value = round(100 * (i + fraction) / len(stages))
    return value, f"{STAGE_TEXT[stage]} (stage {i + 1} of {len(stages)})"

def run_forecast_job(set_progress: ProgressFn, store: CacheStore, runs_dir: Path,
                     params: RunParams, run_id: str | None = None) -> str:
    """Score every model on the test samples and write the run directory; return the run id.

    Runs in the background-callback process (forked): no polars, no DuckDB (ADR004). Tests call
    it directly with a fake ``set_progress``.
    """
```
Body, in order:
1. `run_id = run_id or runs.new_run_id()`; `runs.cleanup_partial(runs_dir)`.
2. `set_progress(progress_value("labels"))`; `grid = data.load_spikes(store)`; `test = data.period_samples(grid.spike, params.horizon, "test")`.
3. `set_progress(progress_value("scoring"))`; `scores = {PERSISTENCE: baselines.persistence_scores(grid.spike, params.horizon, test)}`; `model_metrics = {k: metrics.score_metrics(test.labels, s) for k, s in scores.items()}`.
4. `set_progress((100, SAVING_TEXT))`.
5. `info` as in Design: `{"run_id", "created": datetime.now(UTC).isoformat(timespec="seconds"), "settings": dataclasses.asdict(params), "data": {k: grid.meta.get(k) for k in ("start", "end", "minutes", "burst_k")}, "hosts": grid.hosts, "test_period": grid.meta["split"]["periods"].get("test"), "models": list(scores), "metrics": model_metrics}`.
6. `arrays = {"labels": test.labels, "host": test.host, "minute": test.minute, **{runs.score_key(k): v for k, v in scores.items()}}`; `runs.write_run(runs_dir, run_id, info, arrays)`; `return run_id`.

#### Commit 3: the page (steps 11 to 13)

**Step 11. `src/logdash/analyses/forecast.py`.**

Imports: add `import diskcache`, extend `from dash import Dash, DiskcacheManager, Input, Output, State, ctx, dcc, no_update`, add `from logdash.forecast import metrics as forecast_metrics`, `from logdash.forecast import params as forecast_params`, `from logdash.forecast import runs`.

Constants (exact):
```python
HORIZON_ID = "forecast-horizon"
RUN_BUTTON_ID = "forecast-run"
CANCEL_BUTTON_ID = "forecast-cancel"
PROGRESS_ID = "forecast-progress"
PROGRESS_LABEL_ID = "forecast-progress-label"
MESSAGE_ID = "forecast-message"
RUN_STORE_ID = "forecast-run-id"
THRESHOLD_ID = "forecast-threshold"
RESULTS_ID = "forecast-results"
DEFAULT_THRESHOLD = 0.5
POLL_MS = 500  # how often the page asks for the job's progress
MODEL_LABELS = {"persistence": "Persistence (repeat the last value)"}  # later stories add theirs
RESULT_COLUMNS = ("model", "accuracy", "F1", "AUROC", "PR-AUC", "positive rate")
NA_NO_SPIKES = "n/a (no spikes in the test period)"
NA_NO_SAMPLES = "n/a (no test samples)"
NA_NO_NEGATIVES = "n/a (no spike-free samples in the test period)"
RUN_NOTE = (
    "A run predicts, for every host and every minute of the test period, whether a spike minute "
    "follows within the horizon. The persistence baseline repeats the last value: it predicts a "
    "spike when the host had a spike minute in the last H minutes. The run executes in the "
    "background; the rest of the dashboard keeps answering."
)
ACCURACY_NOTE = (
    "About 1 % of the samples have a spike ahead, so a model that never predicts a spike already "
    "reaches an accuracy near 0.99: accuracy is misleading here. PR-AUC and F1 are the numbers "
    "to read. The threshold applies to F1 and accuracy only."
)
NO_RUN_TEXT = "No run yet. Pick a horizon and press Run."
MISSING_RUN_TEXT = "Run {run_id} is no longer in the runs directory. Press Run to start a new one."
STARTED_TEXT = "Run started."
CANCELLED_TEXT = "The run was cancelled. No results were saved."
```
`DESCRIPTION`: replace its last sentence "No model is trained yet." with "Run scores forecasts on the test period in a background job." (Nothing else in `DESCRIPTION` changes; #50's test checks "the global filters do not apply".)

Pure helpers (module level, tested directly):
```python
def fmt_score(value: float | None, m: Mapping[str, Any]) -> str:
    """3 decimals, or why the number is undefined (no samples / no spikes / no spike-free samples)."""
    # value is not None -> f"{value:.3f}"; m["samples"] == 0 -> NA_NO_SAMPLES;
    # m["positives"] == 0 -> NA_NO_SPIKES; else NA_NO_NEGATIVES

def results_table(run: runs.Run, threshold: float) -> pl.DataFrame:
    """One row per model of the run, in ``run.models`` order; String columns RESULT_COLUMNS."""
```
For each `model`: `m = run.info["metrics"][model]`, `t = forecast_metrics.threshold_metrics(run.labels, run.scores(model), threshold)`; row `model = MODEL_LABELS.get(model, model)`, `accuracy = fmt_score(t["accuracy"], m)`, `F1 = fmt_score(t["f1"], m)`, `AUROC = fmt_score(m["auroc"], m)`, `PR-AUC = fmt_score(m["pr_auc"], m)`, `positive rate = "-" if m["positive_rate"] is None else f"{100 * m['positive_rate']:.2f} %"`. Build with `pl.DataFrame(rows, schema=dict.fromkeys(RESULT_COLUMNS, pl.String))`.

```python
def results_view(run: runs.Run | None, run_id: str | None, threshold: float) -> Component:
```
- `run_id` falsy: `dmc.Text(NO_RUN_TEXT, size="sm", c="dimmed")`.
- `run is None`: `dmc.Text(MISSING_RUN_TEXT.format(run_id=run_id), size="sm", c="dimmed")`.
- Otherwise `m = run.info["metrics"][run.models[0]]` and `dmc.Stack([dmc.Text(f"Run {run.run_id}: horizon {run.info['settings']['horizon']} min, {ui.fmt_int(m['samples'])} test samples, {ui.fmt_int(m['positives'])} with a spike ahead. Threshold {threshold:.2f}.", size="sm"), ui.table(results_table(run, threshold))], gap="xs")`.

```python
def run_note(trigger: str | None) -> str:
    return CANCELLED_TEXT if trigger == CANCEL_BUTTON_ID else STARTED_TEXT
```

`layout`: append two cards after the "Positive rate per prediction horizon" card:
```python
ui.card(
    "Run a forecast",
    dmc.Text(RUN_NOTE, size="sm", c="dimmed", mb="xs"),
    dmc.Text("Prediction horizon (minutes)", size="sm"),
    dmc.Slider(id=HORIZON_ID, min=forecast_params.MIN_HORIZON, max=forecast_params.MAX_HORIZON,
               step=1, value=forecast_params.DEFAULT_HORIZON,
               marks=[{"value": v, "label": str(v)} for v in (1, 5, 10, 15)], mb="xl"),
    dmc.Group([dmc.Button("Run", id=RUN_BUTTON_ID),
               dmc.Button("Cancel", id=CANCEL_BUTTON_ID, variant="default", disabled=True)]),
    dmc.Progress(id=PROGRESS_ID, value=0, size="lg", mt="md"),
    dmc.Text(id=PROGRESS_LABEL_ID, size="sm", c="dimmed"),
    dmc.Text(id=MESSAGE_ID, size="sm"),
    dcc.Store(id=RUN_STORE_ID, storage_type="session"),
),
ui.card(
    "Results on the test period",
    dmc.Text(ACCURACY_NOTE, size="sm", c="dimmed", mb="xs"),
    dmc.Text("Threshold (a score at or above it predicts a spike)", size="sm"),
    dmc.Slider(id=THRESHOLD_ID, min=0.05, max=0.95, step=0.05, precision=2,
               value=DEFAULT_THRESHOLD,
               marks=[{"value": v, "label": f"{v:.1f}"} for v in (0.1, 0.5, 0.9)], mb="xl"),
    html.Div(id=RESULTS_ID),
),
```
(`from dash import html` as well.) The layout still reads only `meta.json`.

`register_callbacks`: keep `draw_spikes` as is, then:
```python
from logdash.forecast import job as forecast_job  # job imports this module: import it here

runs_dir = store.settings.runs_dir
manager = DiskcacheManager(diskcache.Cache(str(runs_dir / runs.JOBS_DIR)))
running = [  # later stories append their own entries
    (Output(RUN_BUTTON_ID, "disabled"), True, False),
    (Output(CANCEL_BUTTON_ID, "disabled"), False, True),
]

@app.callback(
    Output(RUN_STORE_ID, "data"),
    Output(MESSAGE_ID, "children"),
    Input(RUN_BUTTON_ID, "n_clicks"),
    State(HORIZON_ID, "value"),
    background=True,
    manager=manager,
    progress=[Output(PROGRESS_ID, "value"), Output(PROGRESS_LABEL_ID, "children")],
    progress_default=[0, ""],  # keep the same length as progress
    running=running,
    cancel=[Input(CANCEL_BUTTON_ID, "n_clicks")],
    interval=POLL_MS,
    prevent_initial_call=True,
)
def run_forecast(set_progress: forecast_job.ProgressFn, _clicks: int | None, horizon: int | None) -> tuple[Any, str]:
    try:
        params = forecast_params.RunParams(horizon=int(horizon or forecast_params.DEFAULT_HORIZON))
        run_id = forecast_job.run_forecast_job(set_progress, store, runs_dir, params)
    except (OSError, ValueError) as err:
        return no_update, f"The run failed: {err}"
    return run_id, f"Run {run_id} finished (horizon {params.horizon} min)."

@app.callback(
    Output(MESSAGE_ID, "children", allow_duplicate=True),
    Input(RUN_BUTTON_ID, "n_clicks"),
    Input(CANCEL_BUTTON_ID, "n_clicks"),
    prevent_initial_call=True,
)
def note_run(_run: int | None, _cancel: int | None) -> str:
    return run_note(ctx.triggered_id)

@app.callback(Output(RESULTS_ID, "children"), Input(RUN_STORE_ID, "data"), Input(THRESHOLD_ID, "value"))
def show_results(run_id: str | None, threshold: float | None) -> Component:
    t = round(float(DEFAULT_THRESHOLD if threshold is None else threshold), 2)
    return results_view(runs.load_run(runs_dir, run_id), run_id, t)
```
Call the job as `forecast_job.run_forecast_job` (module attribute, so a manual-check script can wrap it). `show_results` must not call the job.

**Step 12. Move the `forecast_cache` fixture** from `tests/unit/test_forecast.py:268` to `tests/conftest.py` with `scope="session"` (keep its body, the `SPEC` it uses and the comment on `error_rows=0`; move `SPEC` to conftest as `FORECAST_SPEC` and import it in `test_forecast.py` where `SPEC` is used). #52's plan expects this move. All #50 tests must pass unchanged otherwise.

**Step 13. Tests**: `tests/unit/test_forecast_lab.py` (L1 to L11) and `tests/unit/test_forecast_page.py` (P1 to P7), section 6.

#### Commit 4: benchmark (step 14)

**Step 14. `tests/benchmark/test_latency.py`**: add after `test_forecast`:
```python
@pytest.fixture(scope="module")
def forecast_run(store: CacheStore, tmp_path_factory: pytest.TempPathFactory) -> tuple[CacheStore, str]:
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
        measure("forecast.show_results", f"threshold {threshold}", CALLBACK_BUDGET_S,
                lambda t=threshold: fns["show_results"](run_id, t))  # type: ignore[misc]
    measure("forecast.show_results", "no run", CALLBACK_BUDGET_S, lambda: fns["show_results"](None, 0.5))
```
Imports: `dataclasses`, `from logdash.forecast import job as forecast_job, runs`, `from logdash.forecast.params import RunParams`. `test_layouts` already covers `forecast.layout`. Run it per protocol (link the real cache, `uv run pytest -m slow --no-cov -s`) and paste the forecast rows and the job time into the PR.

#### Commit 5: ADR, README, plan (steps 15 to 17)

**Step 15. `adrs/ADR004-forecast-background-runs.md`** (Status: accepted; Context; Decision; Consequences). Use the next free number if ADR004 is taken on `main` when you start (#58 may renumber; say so in the PR). Context: #51/#59, later stories #60, #52, #54 extend the run; the fork measurement. Decision bullets:
- Runs are Dash background callbacks with a `DiskcacheManager` whose cache is `<LOGDASH_RUNS>/jobs`; one job function `logdash.forecast.job.run_forecast_job(set_progress, store, runs_dir, params, run_id=None)`; stages in `job.STAGES`, progress through `job.progress_value(stage, stages, fraction)`; the `running` list and `progress`/`progress_default` lists in `register_callbacks` are extended by later stories (both progress lists keep the same length).
- **The job process never calls polars or DuckDB.** It reads `frame.parquet` with pyarrow and `meta.json` with `json`. Libraries that need threads after the fork (torch in #52) are imported inside the job. `tests/unit/test_forecast_lab.py` runs the job in a forked process to guard this.
- Run directory layout, `scores.npz` keys (`labels`, `host`, `minute`, `score_<model>`, extra arrays allowed), `run.json` keys, atomic write, run id format, ids validated on load, runs never modified.
- Samples: `data.period_samples` is the full grid of hosts × kept minutes of a period, host-major; every model is scored on exactly the test samples.
- Metrics: `score_metrics` (samples, positives, positive_rate, auroc, pr_auc) stored per model; accuracy and F1 at the slider threshold computed by the page from the stored scores; the n/a rules.
- The persistence baseline definition (`y(t - H)`, window clipped at the span start).
Consequences: runs accumulate under `runs/` (about 1 MB per run on the real cache) and may be deleted at any time; a run reflects the cache at its time (its `data` block says which); the results callback never recomputes a model.

**Step 16. `README.md`.**
- Configuration table, after `LOGDASH_CACHE`: `| `LOGDASH_RUNS` | `runs` | forecasting lab runs and the background-job cache (git-ignored, safe to delete) |`.
- `## Forecasting lab`: append "Below the charts, **Run** scores the persistence baseline (repeat the last value) on the test period for the chosen horizon in a background job, with a progress bar and Cancel. Each run is saved under `LOGDASH_RUNS` (`runs/<run id>/`); the results table reads it, and the threshold slider changes F1 and accuracy without a rerun. Design: [ADR004](adrs/ADR004-forecast-background-runs.md)."
- `## Tests`: in the benchmark sentence, change "forecast chart" to "forecast chart and results".

**Step 17.** Commit this plan as `plans/2026-10-26-forecast-background-runs-v1.md`.

### 6. Tests

`tests/unit/test_config.py`:
- **C1** `test_runs_dir_from_env_and_default` (AC7): with `monkeypatch.setattr(config, "DEFAULT_RUNS", "runs")`: `Settings().runs_dir == Path("runs") == Settings.from_env({}).runs_dir`; `Settings.from_env({"LOGDASH_RUNS": "/r"}).runs_dir == Path("/r")`; `Settings().with_overrides(runs_dir="x").runs_dir == Path("x")`; `"runs_dir" not in Settings().analysis_params(("burst_k",))`. (The session fixture of step 3 points `DEFAULT_RUNS` at a temporary directory for every other test; the literal default `"runs"` is checked by review of `config.py` and the README row.)

`tests/unit/test_forecast_lab.py` (no built cache). Helper `write_forecast_cache(root: Path, spike: np.ndarray, hosts: list[str]) -> CacheStore`: writes `forecast/frame.parquet` (`bin` from `2023-05-21 00:00` per minute tiled per host, `host` repeated, `spike`) with polars and `forecast/meta.json` with `start`, `end`, `minutes`, `hosts`, `burst_k: 6.0`, `split` (`train_end`, `validation_end` from `split_bounds`, `periods` with ISO bounds). Returns `CacheStore(root)`.
- **L1** `test_period_samples_are_the_full_grid_without_crossings` (AC4): spike `(2, 24)` (`split_bounds(24) == (14, 16)`), random with `np.random.default_rng(0)`. Test, horizon 2: minutes `16..21` for each host (12 samples), host-major (`host == [0]*6 + [1]*6`), `labels == targets(spike, 2)[host, minute]`. Train, horizon 1: last minute 12 (13 would cross into validation). Validation, horizon 1: only minute 14; horizon 2: no samples (`len == 0`, arrays empty, dtypes int32/bool).
- **L2** `test_persistence_repeats_the_last_target` (AC4): row `[0,0,1,0,0,0,0,1,1,0,0,0]`, horizon 2, all minutes as samples: expected `[0,0,1,1,0,0,0,1,1,1,0,0]`. Property: for a random `(3, 60)` spike and `H in (1, 3, 5)`, scores at `t >= H` equal `targets(spike, H)[h, t - H]`. dtype float32.
- **L3** `test_metrics_known_values` (AC4): labels `[1,0,0,1,0]`, scores `[0.9,0.1,0.4,0.35,0.8]`: `samples 5, positives 2, positive_rate 0.4, auroc 4/6, pr_auc 0.75` (`pytest.approx`); at 0.5 `accuracy 0.6, f1 0.5`; at 0.3 `accuracy 0.6, f1 2/3`.
- **L4** `test_metrics_without_positives` (AC4): all-False labels: `auroc is None`, `pr_auc is None`, `f1 is None`, `accuracy` a float; all-True labels: `auroc is None`, `pr_auc == 1.0`; empty arrays: `samples == 0` and every other value `None`; `json.dumps(..., allow_nan=False)` passes for each.
- **L5** `test_progress_value_per_stage` (AC2): `progress_value("labels") == (0, "Computing targets and the time split (stage 1 of 2)")`; `progress_value("scoring")[0] == 50`; `progress_value("scoring", fraction=0.5)[0] == 75`; unknown stage raises `ValueError`.
- **L6** `test_run_params_validate_the_horizon` (AC1): `RunParams(0)` and `RunParams(16)` raise `ValueError` with "between 1 and 15"; `asdict(RunParams()) == {"horizon": 5}`.
- **L7** `test_write_and_load_run` (tmp_path): round trip of info and arrays; no `.tmp-*` left; `load_run` twice returns the same object (`is`); `load_run(dir, None)`, a valid but missing id, `"../etc"` and `"x"` give `None`; writing the same id again raises `FileExistsError`; `write_run` with `info={"x": float("nan")}` raises `ValueError` and leaves neither the run nor a temp directory; a run directory whose `run.json` names a model without its score array loads as `None`; `runs.clear_caches()` then `load_run` returns an equal, new object.
- **L8** `test_cleanup_partial`: an old `.tmp-a` (`os.utime` 2 h back) is removed, a fresh `.tmp-b` and a finished run directory stay; a missing `runs_dir` raises nothing.
- **L9** `test_job_with_a_fake_set_progress` (AC2, AC4): hand-made cache (3 hosts × 48 minutes, spikes in the test period on two hosts), `calls = []`, `run_forecast_job(calls.append, store, tmp_path / "runs", RunParams(horizon=2), run_id="20261023T120000Z-abcdef")` returns that id; `calls == [progress_value("labels"), progress_value("scoring"), (100, "Saving the run")]`; values never decrease; the run loads; `info["models"] == ["persistence"]`; `info["settings"] == {"horizon": 2}`; `info["hosts"]` equals the hosts; labels and `score_persistence` equal `period_samples` and `persistence_scores` on the same array; `info["metrics"]["persistence"] == score_metrics(...)` and has `samples`, `positives`.
- **L10** `test_cancelled_job_saves_nothing` (AC2): `set_progress` raises a local `Cancelled` on its third call (the save) — `pytest.raises(Cancelled)`, `load_run` of that id is `None` and `runs_dir` holds no run directory. Second case: `monkeypatch.setattr(np, "savez_compressed", boom)` (raises `OSError`), the job raises `OSError` and nothing named after the run id or `.tmp-<id>` remains.
- **L11** `test_job_runs_in_a_forked_process` (ADR004 guard): warm polars in the test process (`pl.DataFrame({"g": np.arange(2_000_000) % 1000, "a": np.ones(2_000_000)}).group_by("g").agg(pl.col("a").sum())`), then `multiprocess.get_context("fork").Process(target=_job_in_child, args=(str(root), str(runs_dir), run_id))`, `start()`, `join(60)`. If it is still alive: `kill()` and `pytest.fail("the job hangs in a forked process: it must not call polars or DuckDB (ADR004)")`. Then `exitcode == 0` and `load_run` finds the run. `_job_in_child` is a module-level function that builds `CacheStore(Path(root))` and calls `run_forecast_job(lambda _v: None, ...)`.

`tests/unit/test_forecast_page.py` (session `forecast_cache`; every test builds `store = CacheStore(built.settings.cache_dir, dataclasses.replace(built.settings, runs_dir=tmp_path))`). A local 8-line `Recorder` like the benchmark's.
- **P1** `test_run_callback_is_a_background_callback` (AC5, AC2): `app = create_app(store, registry)`, no request before the asserts. `entry = next(v for k, v in app.callback_map.items() if "forecast-run-id.data" in k)`: `entry["background"]` is truthy; `[(c.component_id, c.component_property) for c in entry["background"]["cancel_inputs"]] == [("forecast-cancel", "n_clicks")]`; `[str(o) for o in entry["background"]["progress"]] == ["forecast-progress.value", "forecast-progress-label.children"]`; `isinstance(entry["manager"], DiskcacheManager)` and `Path(entry["manager"].handle.directory) == tmp_path / "jobs"`. The spec in `app._callback_list` whose `"output"` contains `forecast-run-id.data` has `running == {"running": {"forecast-run.disabled": True, "forecast-cancel.disabled": False}, "runningOff": {"forecast-run.disabled": False, "forecast-cancel.disabled": True}}`.
- **P2** `test_layout_has_the_run_controls` (AC1, AC3): walk `Forecast().layout(store)` (helper `find(component, id)` over `children`): horizon slider `min == 1, max == 15, step == 1, value == 5`; Cancel `disabled is True`; threshold slider `value == 0.5`; run store `storage_type == "session"`; `str(layout)` contains "accuracy is misleading" and "PR-AUC and F1 are the numbers to read".
- **P3** `test_run_then_results_on_the_forecast_data` (AC2, AC3): `fns = Recorder` funcs; `progress = []`; `run_id, message = fns["run_forecast"](progress.append, 1, 5)`; `message == f"Run {run_id} finished (horizon 5 min)."`; `len(progress) == 3`; `fns["show_results"](run_id, 0.5)` as a string contains "Persistence (repeat the last value)", "test samples" and "%"; the run's persistence metrics have `positives > 0` and a float `pr_auc` in `(0, 1]` (the synthetic test period has injected spikes).
- **P4** `test_results_states_and_failures`: `show_results(None, 0.5)` contains `NO_RUN_TEXT`; a valid missing id and `"../../etc"` give the `MISSING_RUN_TEXT` sentence; `show_results(run_id, None)` uses 0.5; with `monkeypatch.setattr(forecast_job, "run_forecast_job", raises ValueError("stale cache"))`, `fns["run_forecast"](...)` returns `(no_update, "The run failed: stale cache")`; `run_note("forecast-cancel") == CANCELLED_TEXT` and `run_note("forecast-run") == STARTED_TEXT`.
- **P5** `test_threshold_moves_f1_and_accuracy_only` (AC3): a hand-made `runs.Run` (labels `[1,0,0,1,0]`, model `persistence` with scores `[0.9,0.1,0.4,0.35,0.8]`, metrics from `score_metrics`): `results_table(run, 0.5)` and `(run, 0.3)` differ in `F1` (`0.500` vs `0.667`), equal in `AUROC` (`0.667`), `PR-AUC` (`0.750`) and `positive rate` (`40.00 %`); columns equal `RESULT_COLUMNS`. A run whose labels are all False shows `NA_NO_SPIKES` in `AUROC`, `PR-AUC` and `F1`. A second model key `"other"` gives a second row labelled `"other"` (rows follow `run.models`).
- **P6** `test_results_callback_over_http` (AC3): run once via `run_forecast_job`, then POST `/_dash-update-component` with output `forecast-results.children`, inputs `forecast-run-id.data = run_id` and `forecast-threshold.value = 0.5` (body like `tests/unit/test_forecast.py:392`); status 200 and the response text contains "Persistence".
- **P7** `test_page_still_answers_over_http`: `GET /forecast` is 200 with the new layout (the `draw_spikes` POST from #50's test still returns a figure).

Manual checks, recorded in the PR (AC2 in the browser):
1. Link the real cache (protocol), `uv run logdash serve --no-build`, open `/forecast`, press Run with horizon 5: the bar moves, Run is disabled and Cancel enabled during the run, then the table shows one row and "Run … finished". Move the threshold slider. Toggle dark mode once.
2. Cancel: the real job takes about a second, too short to click Cancel. Start the app from a scratch script (not committed) that wraps the job so every progress step sleeps 4 s:
   ```python
   import time
   from logdash.analyses import load_builtin
   from logdash.app.main import create_app
   from logdash.cache.store import CacheStore
   from logdash.config import Settings
   from logdash.forecast import job

   real = job.run_forecast_job
   def slow(set_progress, *args, **kwargs):
       def step(value):
           set_progress(value)
           time.sleep(4)
       return real(step, *args, **kwargs)
   job.run_forecast_job = slow
   load_builtin()
   settings = Settings.from_env()
   create_app(CacheStore(settings.cache_dir, settings)).run(port=8052)
   ```
   Press Run, then Cancel during the first stage: the page says "The run was cancelled. No results were saved.", Run is enabled again, the previous results stay, and `runs/` holds no new run directory. Note the result in the PR.

AC6: P-tests plus the benchmark (step 14). AC7: C1, the README and `.gitignore` diffs. AC8: the full gate.

### 7. Risks

- **The job hangs in the forked process** if it calls polars (measured: hangs) or DuckDB. Guarded by: `data.load_spikes` uses pyarrow and `json`; the package docstring and ADR004 state the rule; L11 runs the job in a real fork after warming polars. Do not "fix" L11 by using spawn.
- **Path traversal through the run id** from the browser store: `valid_run_id` pattern check in `load_run` and `write_run` (L7, P4).
- **A cancelled job leaves partial results**: single rename at the end (L10); stale `.tmp-*` removed by the next job (L8). A cancel that lands after the rename leaves a complete run, which is correct; the message then says cancelled while the store keeps the previous id. Acceptable; noted here for the reviewer.
- **Stale or missing cache**: `load_spikes` raises `ValueError` on a row-count mismatch, `FileNotFoundError` without the stage; the callback turns both into "The run failed: …" (P4).
- **No positives / no samples** (short span, horizon near the period length): metrics return `None`, the table shows the n/a texts, JSON stays strict (L4, P5).
- **NaN**: scores are 0/1 for persistence; `json.dumps(allow_nan=False)` in `write_run` refuses NaN in metrics instead of writing a bad file (L7).
- **Session store after a rebuild or a new `LOGDASH_RUNS`**: the id no longer exists → `MISSING_RUN_TEXT`, no error (P4).
- **Tests writing `./runs`**: session autouse fixture (step 3). Settings built in module or session fixtures read the patched default because the field uses a default factory.
- **Positional `Settings(...)` calls**: `runs_dir` is the last field, so existing calls keep their meaning.
- **Latency**: `show_results` reads a memoised run and computes two counts over at most 77 k samples (162 hosts × 475 test minutes at horizon 5); benchmarked (step 14). The layout adds no I/O.
- **UTC**: `created` and run ids use `datetime.now(UTC)`; times shown come from `meta.json` (naive UTC).
- **162 hosts**: the job holds a 162 × 1,439 bool array and 77 k-sample arrays: a few MB.
- **Two concurrent runs** (two tabs): separate processes, distinct run ids, separate temp directories.
- **Disk growth**: about 1 MB per run, never pruned in this story (README says the directory is safe to delete).

### 8. Docs

- `adrs/ADR004-forecast-background-runs.md` (step 15): binds #60, #52, #53 and #54.
- `README.md`: configuration row, Forecasting lab paragraph, benchmark sentence (step 16).
- `.gitignore`: `/runs/` (step 3).

### 9. Open questions (non-blocking, defaults used)

1. **Issue names ADR002, the contract is ADR003.** Default: follow `adrs/ADR003-spike-forecasting-lab.md`.
2. **Run-id store survives a page reload** (`storage_type="session"`). Default: yes, so returning to the page shows the last run; a missing run shows a sentence, not an error.
3. **Threshold range** 0.05 to 0.95 in steps of 0.05 (0 and 1 are degenerate). Default as stated.
4. **F1 with no positives** shows "n/a (no spikes in the test period)" like AUROC and PR-AUC, because it is undefined there.
5. **Runs are never pruned.** Default: no pruning in this story.
6. **Fixture move to `conftest.py`** (step 12) is done here, because #52's plan expects it and P-tests need the forecast cache in a second module.
