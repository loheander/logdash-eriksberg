# logdash: implementation plan

Interactive dashboard for analysing the OpenStack log dataset under `data/`.

## 1. Goals and constraints

- Analyse `data/flog-data_*_preprocessed.csv.zst` (759 MB compressed, ~13 GB CSV,
  38,819,200 rows, 24 h from 2023-05-21 02:01 to 2023-05-22 01:59 CEST).
- Expensive work happens once in a **precompute** step that writes to `cache/`.
  The dashboard only reads `cache/` and must stay responsive on the laptop
  (16 cores, 31 GB RAM, ~10 GB typically free).
- Extensible: a new analysis is one new module, registered in one place.
- Test coverage of at least 90 %, enforced in CI and locally.
- Dependency management with `uv`.

## 2. Data profile (from a full scan)

| Column | Distinct | Empty / `-` | Notes |
|---|---|---|---|
| `Timestamp` | | | Unix seconds; `@timestamp` holds ms precision with offset |
| `log_level` | 9 | 1.93 M | Mixed casing: `INFO`/`info`, `WARNING`/`warning`/`warn`/`WARN`/`Warning`, `ERROR` (1,244), `CRITICAL` (1) |
| `programname` | 40 | 1,055 | `neutron-openvswitch-agent` 21.2 M, `neutron-l3-agent` 6.8 M, `haproxy` 5.4 M |
| `Hostname` | 162 | 1,055 | One host has a `.xerces.lan` suffix; normalise |
| `Logger` | 10 | 6.9 M | `openstack.<service>` |
| `python_module` | 51 | 8.1 M | |
| `request_id` | 2.44 M | 8.2 M | Enables per-request session analysis |
| `user_id`, `tenant_id` | 21 | 29.7 M | |
| `user_domain`, `project_domain` | 3 | 36.5 M | |
| `domain_id` | 1 | all | Drop |
| `Pid` | 5,763 | 7.4 M | Float-formatted (`1910.0`) |
| `id` | 575 | 1,055 | |
| `Payload` | 14.9 M | 1,055 | Highly templated; top messages are parameterised iptables warnings |

Volume is flat at ~1.6 M events/hour: no daily cycle, mostly background agent noise.
1,055 rows are malformed (almost all fields empty) and are kept but flagged.

## 3. Technology

- **Python 3.12**, managed with **uv** (`pyproject.toml`, `uv.lock`).
- **Dash** + **dash-mantine-components** (light/dark theme, AppShell layout) +
  **Plotly** for figures; `dash-ag-grid` for tables.
- **DuckDB** for ingestion and for interactive queries over Parquet.
- **Polars** / **PyArrow** for dataframe work in precompute.
- **Drain3** for log template mining.
- **scikit-learn** for PCA, Isolation Forest, clustering; **NumPy** / **SciPy**
  for FFT, autocorrelation, robust statistics.
- **pytest**, **pytest-cov**, **ruff**, **mypy** (lenient on Dash internals).

## 4. Architecture

```
data/*.csv.zst ──► logdash build ──► cache/ ──► logdash serve (Dash app)
                   (precompute)       Parquet     reads only, never
                                      + JSON      touches data/
```

### 4.1 Package layout

```
src/logdash/
  __init__.py
  cli.py               # `logdash build|serve|status`
  config.py            # Settings (pydantic-settings / dataclass), env + CLI overrides
  ingest/
    reader.py          # DuckDB read_csv over .zst, typed schema
    normalize.py       # level/host/pid normalisation, malformed-row flag
    masking.py         # regex masking of IPs, UUIDs, hex, numbers before Drain
  cache/
    store.py           # CacheStore: paths, read/write Parquet & JSON, DuckDB views
    manifest.py        # fingerprints, staleness check
  analyses/
    base.py            # Analysis protocol, ComputeContext, registry
    overview.py        # 0  data quality & overview
    distributions.py   # 1  histograms
    temporal.py        # 2  temporal analysis
    templates.py       # 3  Drain3 templates
    explorer.py        # 8  raw log explorer
    features.py        # 4  (phase 2)
    pca.py             # 5  (phase 2)
    anomalies.py       # 6  (phase 2)
    hosts.py           # 7  (phase 2)
  detectors/           # phase 2: one file per anomaly detector, common interface
  app/
    main.py            # create_app(store) -> Dash
    shell.py           # Mantine AppShell, nav built from the registry, theme toggle
    filters.py         # global filter state (time range, level, service, host) in dcc.Store
    figures.py         # shared figure styling, downsampling helpers (LTTB, top-N + "other")
tests/
  conftest.py          # synthetic dataset fixture, temp cache fixture
  synthetic.py         # generator for small .csv.zst with injected anomalies
  unit/ ...            # mirrors src layout
  integration/         # build -> serve end to end on synthetic data
```

### 4.2 Analysis module contract

Every analysis implements one interface and registers itself:

```python
class Analysis(Protocol):
    key: str  # "temporal"; also the cache subdirectory and URL path
    title: str  # nav label
    version: str  # bump to invalidate this module's cache
    depends_on: tuple[str, ...]  # upstream analyses (e.g. pca depends on features)

    def compute(self, ctx: ComputeContext) -> None: ...
    def layout(self, store: CacheStore) -> Component: ...
    def register_callbacks(self, app: Dash, store: CacheStore) -> None: ...
```

- `ComputeContext` provides the DuckDB connection with the normalised `events`
  view, settings, the module's output directory and a logger/progress reporter.
- The registry orders modules topologically by `depends_on`.
- The app shell builds navigation and pages from the registry (Dash Pages), so
  adding an analysis needs no changes to the shell.
- Pure functions do the work (`compute_*`, `make_*_figure`); `compute`,
  `layout` and callbacks are thin wrappers. This keeps most code testable
  without a browser.

### 4.3 Cache

```
cache/
  manifest.json            # per module: fingerprint, created_at, duration, row counts
  events/hour=00..23/*.parquet   # normalised rows + template_id, zstd, sorted by ts
  overview/*.json|parquet
  distributions/*.parquet
  temporal/*.parquet       # counts per bin (1s, 10s, 1m, 5m) x level/service/host
  templates/templates.parquet, template_counts.parquet, params/*.parquet
  ...
```

- **Fingerprint** = hash(data file name + size + mtime, or xxhash of the content
  with `--strict`) + relevant settings + module `version` + upstream fingerprints.
  `logdash build` recomputes only stale modules; `--only <key>` and `--force`
  are supported.
- `cache/` is portable (plain Parquet/JSON), so `build` can run on another
  machine (for example the DGX Spark) and the directory can be copied back.
- `cache/` is git-ignored.

### 4.4 Responsiveness rules (laptop)

- Callbacks never scan `events/` for full-range plots; they read pre-binned
  aggregates. Bin size is chosen from the visible range (≤ ~5,000 points per trace).
- `events/` is queried only by the raw explorer and drill-downs, always with a
  `LIMIT` and pushed-down time/host filters on hour partitions.
- Small aggregates are loaded once into memory at app start; DuckDB connection is
  read-only and shared.
- Categorical charts show top-N plus "other".
- Target: every callback < 300 ms on the laptop; a benchmark test asserts this on
  the real cache when present (marked `slow`, skipped in CI).
- DuckDB `memory_limit` and `threads` are set from config (default 8 GB) so
  `build` fits on the laptop.

## 5. Analyses

### Phase 1

**0. Overview and data quality**
Headline cards (rows, time span, hosts, services, templates), missing values per
column, malformed rows, level normalisation mapping, distinct values per column.

**1. Distributions**
Histograms and bar charts for level, program, host, logger, module, tenant;
payload length; lines per request; request duration (first to last timestamp per
`request_id`); inter-arrival times per service. Linear/log toggle.

**2. Temporal analysis**
Event rate at selectable bins (1 s, 10 s, 1 m, 5 m), split by level, service or
host; host × time heatmap; autocorrelation and FFT periodogram per service;
simple burst and gap detection (rolling median ± k·MAD, silent hosts).
Brushing a time range updates the global filter used by all pages.

**3. Log templates**
Mask variables (IPs/CIDRs, UUIDs, hex, numbers, paths), deduplicate masked lines,
run Drain3 on unique masked lines only, map back to `template_id` per row.
Page: top templates, template volume over time, rare templates, first-seen
time, extracted parameter distributions per template.

**8. Raw log explorer**
Paged AG Grid over `events/` with filters (time, level, service, host,
template, request ID, text search). Other pages deep-link here with filters
prefilled via URL query.

### Phase 2

**4. Feature analysis**
Window × template count matrix (default 1-minute windows, grouped per service,
switchable to per host), log1p or TF-IDF scaling. Template correlation
heatmap, template–service association, variance ranking and mutual information.

**5. PCA**
Scree plot, 2D/3D projections coloured by time/service/host/anomaly score,
loadings, and the residual SPE/Q-statistic (Xu et al., 2009) as an anomaly score.

**6. Anomaly detection and types**
Each detector implements a common `Detector` interface returning
`(window, entity, score, type, evidence)` rows:

| Type | Detector |
|---|---|
| Volume spike/drop | Rolling robust z-score per service/host |
| Contextual | Host deviating from its peer cluster |
| Structural | PCA residual (SPE), Isolation Forest |
| Novel/rare template | First-seen and frequency thresholds |
| Sequence | Markov transition log-likelihood per `request_id` |
| Severity burst | WARNING/ERROR rate change |

Page: combined anomaly timeline, sortable table with type and score, per-type
filter, drill-down to raw lines in the explorer.

**7. Hosts and services**
Host clustering on template mix (k-means / hierarchical), outlier hosts,
service × host matrix.

### Phase 3 (optional, cluster)

GPU-backed methods to run on the DGX Spark and copy into `cache/`:
template embeddings with a sentence-embedding model, DeepLog/LogBERT-style
sequence anomaly scores. Same `Analysis` contract; the dashboard part runs on
the laptop from cached results.

## 6. Testing strategy

- **Synthetic dataset**: `tests/synthetic.py` generates a small `.csv.zst`
  (~50k rows, same schema, mixed-case levels, malformed rows, a few known
  templates) with injected anomalies (a volume spike, a silent host, a novel
  template, an out-of-order request sequence). Detectors are tested to find them.
- **Unit tests** for pure functions: normalisation, masking, fingerprints,
  binning, downsampling, each detector, each figure builder (assert on figure
  data, not pixels).
- **Callback tests**: call callback functions directly with inputs.
- **Layout tests**: each page's `layout(store)` renders against a cache built
  from the synthetic data.
- **Integration**: `build` on synthetic data → `create_app` → Flask test client
  requests every page and `_dash-update-component` for key callbacks.
- **Coverage**: `pytest --cov=logdash --cov-branch --cov-fail-under=90`
  configured in `pyproject.toml`.
- Markers: `slow` (real data benchmarks), skipped by default.

## 7. Commands

```bash
uv sync                          # install
uv run logdash build             # precompute into cache/ (only stale modules)
uv run logdash build --only templates --force
uv run logdash status            # show cache state per module
uv run logdash serve             # builds if stale, then starts on :8050
uv run pytest                    # tests + coverage gate
uv run ruff check . && uv run ruff format --check .
```

## 8. Phase 1 work breakdown

Status: **Phase 1 done** (steps 1 to 9).

1. Project scaffold: `pyproject.toml` (uv, src layout, entry point, tool config),
   `.gitignore` (`data/`, `cache/`), ruff/mypy/pytest config, README usage.
2. Synthetic dataset generator and fixtures.
3. Ingestion: reader, normalisation, malformed-row flag, hour-partitioned
   `events/` Parquet.
4. Cache store, manifest, fingerprints, CLI `build`/`status`.
5. Analysis base, registry, topological ordering.
6. App shell: Mantine AppShell, theme toggle, global filters, pages from registry.
7. Modules 0, 1, 2, 3, 8 (compute + page + tests each).
8. Run `build` on the real data, measure build time and callback latency,
   tune bins and limits.
9. Coverage check ≥ 90 %, README with screenshots.

### Phase 1 status

- Real data: build takes 1 min 58 s (events 97 s, other stages under 8 s each),
  cache 622 MB, 281 templates from 73,583 unique masked lines.
- Latency (`pytest -m slow --no-cov`, real cache): every page layout under 25 ms and
  every callback under 300 ms warm; worst are temporal rate by service on one hour
  (about 190 ms), heatmap at 1 min (about 180 ms) and explorer text search (about 180 ms).
  Start-up (`create_app`) about 220 ms.
- Tuning: explorer text search scanned all payloads (5 s); it now walks hour partitions
  in time order with a 150 ms budget and says where it stopped. Bursts within two
  minutes merge into episodes, need 300 events above baseline and are ranked by excess
  volume (867 rows became 522; the table now shows a real recurring neutron-l3-agent
  burst every 75 minutes). The malformed-row host bucket is not analysed. Fixed a
  rate chart that was blank on data not aligned to bin edges.
- Deviations: hour partitions are in UTC; `events` carries `masked_hash` instead of
  `template_id` (templates map via `templates/assignments.parquet`); a `level_raw`
  column keeps the original level; shared widgets live in `app/ui.py`; the global time
  range is only respected by Temporal, Templates and Explorer.

### Phase 2 status

- Module 4 (features) done: `analyses/features.py`, built from `templates/template_counts*.parquet`
  only. The templates stage now also writes `template_counts_host.parquet` (version 2). The
  stage stores a sparse long-format matrix and row index per grouping (service, host) and
  window (1, 5, 15 min) in `cache/features/`; `load_matrix` returns a CSR matrix for PCA and the
  detectors. The page computes scaling (log1p, TF-IDF), correlation, variance and the mutual
  information between "template occurs in a window" and the entity from that matrix in memory.
  Latency on the real cache is covered by the slow benchmark but has not been measured yet.

## 9. Open decisions

- Default window for matrices: 1 minute (switchable). Default grouping: per
  service (switchable to per host).
- Whether to keep full `Payload` in `events/` or only masked text + parameters
  (full text kept by default; revisit if the cache grows too large).
