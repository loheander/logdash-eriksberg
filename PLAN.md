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
host; host × time heatmap (one labelled row per host, dendrogram order, scrolling); autocorrelation and FFT periodogram per service;
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
| Novel/rare template | First-seen (late first line, scored in bits) and frequency (count at most the rare threshold) |
| Sequence | Markov transition tail mass per `request_id` (smoothed log-likelihood in the evidence) |
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
  (about 190 ms), heatmap at 1 min (99 ms cold, 82 ms warm, one labelled row per host; main was about 250 ms warm on this host, the earlier 180 ms was a quieter machine) and explorer text search (about 180 ms).
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
  window (1, 5, 15 min) in `cache/features/`; `load_matrix` returns a `Matrix` with a CSR count matrix for PCA and the
  detectors; `load_scaled` returns the log1p or TF-IDF variant. The page computes scaling,
  correlation, variance and the mutual information between "template occurs in a window" and the
  entity from that matrix in memory, memoised per (grid, range, filter, scaling), so a change of
  theme or top-N only redraws. Bumping the templates version (per-host counts) forces a rebuild of
  templates, features and everything downstream on the next `logdash build`. On a synthetic
  matrix (300k windows x 300 templates, 3 % density, 30 entities) a cold request takes about
  0.5 s, a theme or top-N change about 40 ms and a new scaling or range about 0.35 s; the slow
  benchmark has not been run on the real cache yet.
- Module 5 (PCA) done: `analyses/pca.py`, built from `features.load_scaled`. For each grouping,
  window and scaling (12 fits) scikit-learn PCA (covariance eigendecomposition on the sparse matrix) is
  fitted over the non-empty windows of the whole span at build time. The stage stores per fit
  `fit_*.json` (explained variance, `k` for 95 %, SPE control limit, loadings of the first 10
  components), `scores_*.parquet` (`row, bin, entity, pc1..pc3, spe, limit, ratio`) and `residuals_*.parquet`
  (top 3 templates per window by residual, signed, with share of the SPE) for the anomaly module and the
  structural detector (`load_spe`, `load_residuals`). The SPE control limit is Jackson-Mudholkar at
  99.9 % under Gaussian theory (a reference line; downstream detectors use `ratio = spe / limit`); it is
  infinite when the residual space has no variance. The page reads these files only (memoised per file version), applies the time range and
  service or host filter by selecting windows and samples at most 5,000 (2D) or 3,000 (3D) points, always
  keeping the highest-SPE quarter. On the synthetic data the volume spike and the novel template exceed
  the limit in the per-service 1-minute log1p fit; in the per-host grid the spike is absorbed by a
  retained component, so it is not above the limit there. A synthetic 230k x 281 fit takes about 4 s;
  the slow benchmark has not been run on the real cache yet.
- Module 6 framework (issue 3) done: `detectors/base.py` (the `Detector` protocol, a registry,
  declared entity kinds and scoring), `analyses/anomalies.py` (stage and page) and one real
  detector, `detectors/structural_pca.py`, the PCA residual part of the structural type (the Isolation Forest is beside it, see issue 6 below). Every module in `detectors/` is imported, so a detector is
  one new file and the stage depends on what its detectors depend on; a new entity kind is one
  `declare_kind` call (explorer parameter, global filter, whether the link is time-limited),
  an undeclared kind is rejected by name, and a detector that raises is logged, skipped and
  recorded in `meta.json` without losing the others. `cache/anomalies/anomalies.parquet` holds
  `detector, type, bin, window_s, kind, entity, template_id, raw, score, overall, evidence`. The score in
  [0, 1] is `0.5 * percentile + 0.5 * magnitude` (rank among the detector's rows of the same
  kind; log-scale excess over the threshold relative to that kind's 99th percentile, raw clipped
  at 1024x), replacing a saturating `1 - threshold / (2 raw)` whose median was 0.995 and under
  which every host row outranked every service row. Rows are capped at 100 per service or host,
  then 20,000 per detector. On the real data the PCA detector finds 1,676 service and 71,745
  host windows above the limit (73,421), keeps 11,446 (all 34 services and 162 hosts, at most
  100 each; 61,975 dropped, recorded in `meta.json` and shown as "kept N of M" on the page);
  median score 0.50, the default top 100 holds 97 host and 3 service rows. The stage takes 0.2 s
  and the page answers in at most 61 ms (cold or warm) on the real cache. The test of
  injected anomalies checks type and rank (the PCA spike and the novel template, bounded to ten
  minutes after its first line, are in the top 10 and 15); the sequence case
  passes since issue 8, severity since issue 9.
  A stub detector of a new type on a new kind is tested end to end.
- Volume detector (issue 4) done: `detectors/volume.py` uses `find_bursts`, `find_drops` and
  `find_gaps` of `analyses/temporal.py` on `rate_60` (spikes and drops per service and host) and
  `host_60` (gaps per host); one row per episode (`window_s` is its length). Raw score is the peak
  or trough robust z-score (threshold `burst_k`, via `threshold_for`); a gap scores
  `burst_k * minutes / gap_minutes`. Drops need a baseline of 50 events/min and cover busy
  services going silent; gaps and drops stay inside an entity's first and last line; periodic
  repeats of one entity are merged into the strongest episode. The synthetic volume spike and
  silent-host gap are in the top 20; partial drops are tested on hand-made rate tables. Real data:
  87 episodes of 42 entities (426 before merging; 91 spikes before the full-hour baseline rule
  removed 4 start-up artefacts), no drops or gaps, 0.4 s. Framework score normalisation is
  unchanged: 2 of the top 100 anomalies are volume.
- Contextual detector (issue 5) done: `detectors/contextual.py` scores host x 1-minute windows by their
  distance to the host's peer centroid (`hosts.centroid_distance`, `load_peer_centroids`). Each host has its own
  fit `expected(n) = a + b / sqrt(n)` of that distance against the window's line count (robust refit, `b >= 0`) and
  a scale (MAD of the residuals, at least 0.1 of the median, times `sqrt(median lines / n)` clipped to 0.5..2);
  `u` is the standardised residual and the raw score is `z = u - median(u of the host's peers in the same minute)`
  (`hosts.load_peers`; the fleet's median of the minute when fewer than 3 peers have a window, no shift when the
  fleet is that small too), so fleet-wide events are not host-versus-peer deviations. Threshold 8 (about the
  99th percentile of `z` on the real data; the quantiles are in the meta) and at least 3 lines of the template
  that leads the evidence. Windows with fewer than `max(30, 0.5 * median lines)` lines are not scored (inert on
  the real data: all windows have 46 or more lines). The 23 hosts without a valid peer group are not scored: the
  count and the names go to the detector's meta (`DetectContext.meta`, stored as `info` in `meta.json`
  after `json_safe` converts numpy values and NaN; its `note` is shown on the page). `hosts.window_matrix` was
  factored out of `window_baseline` (same output). The synthetic injection (`context_busy_rows`,
  `context_minutes`, off by default) makes one busy host log a single message for 6 minutes (z 15 to 22); it
  changes the structural fits of the shared data, so it has a dataset of its own in `test_contextual.py` and the
  structural novel-template bound stays 15. Hand-made fleets test the negative cases (a fleet-wide shift, a
  normal host and a single stray line are not flagged) and that the flag rate is level over line counts. Real
  data: 2,299 of 199,882 scored windows at `z >= 8`, 496 after the evidence rule, 456 kept over 41 hosts, 0.9 s;
  the top rows are eselda12u01s09 ('could not open network device' at 23 to 33 % of a window, z 70 to 94).
- Structural detectors (issue 6) done: `structural_pca` is reworked and `structural_iforest` added, with shared
  helpers in `detectors/_episodes.py` (`load_builtin` skips `_` modules). The fixed limit `ratio >= 1` flagged 31 %
  of the host windows (two hosts sit at ratio 100 all day), so both judge a window against its own entity: a
  window is flagged above the entity's 99.5 % quantile (PCA also above the limit, the forest above the average
  isolation score 0.5), minutes with more than 25 % of the active entities (at least 4) flagged are dropped as
  fleet-wide (counted in `meta.json`, with the worst minutes; each becomes one row of kind `fleet`, so the
  largest events stay on the page), and flagged windows at most 5 minutes apart are one episode. The forest
  is fitted per entity (100 trees, 256 samples, seed 0; entities under 100 windows or with
  identical windows are skipped and counted). `raw` is `peak ratio / level` (PCA) and `c / E[h]` (forest), both
  with threshold 1. Ranking: `overall = 1 / (60 + rank by score within detector and kind)` (reciprocal rank
  fusion, `FRAMEWORK_VERSION` 4); the page sorts by it by default (`score` stays as a sort and the timeline's y
  axis). Real data: PCA 1,232 windows flagged, 774 fleet-wide, 377 episodes; forest 1,405 flagged, 885
  fleet-wide, 437 episodes (12 short services skipped); 10 fleet rows per detector; stage 26 s (forests 25 s).
  The combined top 100 by score held 97 PCA rows on 11 entities; by `overall` it holds 33 PCA, 33 forest, 22
  volume and 12 contextual rows, 20 of them fleet rows, the rest at most 10 per entity and 2 per u39 or u40
  host (checked on the real cache in the slow benchmark). Tests: each structural detector must find the
  injected service spike and the novel template on its own (the host spike is a volume anomaly, found by the
  forest only), negative cases on hand-made data (iid noise within the 0.5 % budget, constant entity,
  short entity, fleet-wide minute, episode merge) and the ranking. Slow benchmark: every callback under 300 ms.
- Novel and rare template detectors (issue 7) done: `detectors/novel.py`, type `novel`, kind `service`
  (the busiest service of the template's first minute, or of each window), reading `templates.parquet`
  and `template_counts.parquet`. `novel_template`: one row per template first seen in the last
  `novel_fraction` of the span (the templates page's rule), raw `-b * log2(1 - u)` bits with `b` the
  template's runs of active minutes (gap over 10 minutes starts a new run) and `u` its first line's position in the span (the chance a steady template
  starts that late is `(1 - u) ** b`); threshold `log2(1 / novel_fraction)` is the gate for `b = 1`;
  evidence has the text, `p` and how many other templates appeared within 10 minutes (an operator episode
  reads as one story). `rare_template`: one row per (template, minute, service) of a template with at most
  `rare_threshold` events, raw `(rare_threshold + 0.5) / count`, threshold 1, the first window of a novel
  template left out for the service the novel row names; `novel_fraction` is validated (0 < f < 1) in `Settings`. The synthetic data gained a two-line rare template (`RARE_TEMPLATE`, mid-run, from its
  own generator so the rest of the data is unchanged); the novel (rank under 10) and rare (under 15) cases
  replace the strict xfail. Hand-made tables test the boundaries, the burst-versus-spread score, settings
  overrides, empty input and a template without counts. Real data: 15 novel templates (13 in 22:03 to
  22:10, one operator episode, two at 23:54) and 166 rare windows, 0.03 s together; the combined top 100
  by `overall` holds 9 novel and 9 rare rows (28 PCA, 28 forest, 18 volume, 9 contextual).
- Sequence detector (issue 8) done: `detectors/sequence.py`, type `sequence`, kind `request`, from
  `events` joined to `templates/assignments.parquet` (events carry no template id) in DuckDB. Requests of 2 to
  100 lines are scored (871 longer ids hold 27.99 M of the 30.66 M lines with a request id; periodic agent loops
  would own the model; the count is in the note). Pairs `(src, dst)` in `(ts, template_id)` order are fitted over
  the population; a pair's tail mass `p = sum(c' <= c) / tot(src)` is the chance a draw from the state is as rare,
  the request's `p` is Sidak over its `L` transitions and `raw = -log10(p)`; mean smoothed NLL (`(c + 0.5) /
  (tot + 0.5 K)`) is only evidence, since 96 % of the scored requests have one transition. Deviations from the
  plan: `ALPHA` is 0.01 instead of 0.05 (6,711 requests at 0.05 per transition is not a strict level), and there is
  no `tot >= 20` rule; at most 5 requests per rarest transition are listed (the most extreme and an even spread
  over the group's time order, with the group size and span in the evidence; 51 of the flagged were one identical
  pair). Sidak is over the request's distinct pairs. The synthetic out-of-order request is one request with a
  pair seen once among 150 (own generator, `truth.out_of_order_request_ids`), so it is found at alpha 0.01; the
  5 swapped requests (3 %, `truth.swapped_request_ids`) are a common variant (tail mass 0.033) that a first-order
  chain cannot call anomalous, and a test documents that limit. Only 5 of 70 source states can flag; the 871
  requests over 100 lines are not scored (README gives them by program). The detector version embeds its
  constants. The strict xfail is removed. Real data: 203,308 requests scored; 98 flagged after Sidak, 40 kept, 3 s,
  peak 2.8 GB; the top row is `GET -> returned with HTTP` seen 51 of 190,285 times
  (P = 0.0003). The combined top 100 holds 8 sequence rows (25 PCA, 25 forest, 16 volume, 9 contextual, 9 rare,
  8 novel); the first is at position 12.
- Severity detector (issue 9) done: `detectors/severity.py`, kind `service` only (`rate_60` is the one table with a
  level). WARNING and ERROR+CRITICAL series per service through `find_bursts` (new `min_excess` parameter, default
  unchanged). One score scale: the tail `P(X >= peak)` as a Gaussian-equivalent z, Poisson at rate `max(median, mean
  outside incidents, 0.1)` or negative binomial when the rolling MAD exceeds `sqrt(rate)`; p printed as `P≈1e-412`
  below float range. Floors: none on the peak (the burst gate needs 7 lines at zero baseline), excess 300 for WARNING
  (volume default) and 30 for ERROR (chosen, documented as binding). Periodic episodes are merged first with the volume
  detector's `merge_periodic` (demoted to the threshold); the rest are merged into one incident per service and level
  when less than 60 minutes apart, scored against the baseline with the incident interpolated out. The shared fleet
  filter is not used (the meta records the most distinct services starting a burst in one minute). Synthetic injection:
  `error_rows=600` ERROR lines of neutron-l3-agent on the context host in two minutes at 80 % of the span (own generator,
  seed + 4000; a host and service no other injection uses, because a large injection raises that host's own 99.5 %
  structural level and hides another injection: README "Known limitations"). The strict xfail is gone.
  Real data: 5 incidents (barbican-api 7 bursts in one row), 0.1 s; the combined top 100 holds 5 severity rows (47
  structural, 16 volume, 16 novel and rare, 8 contextual, 8 sequence).
- Module 7 (hosts and services, issue 10) done: `analyses/hosts.py` from `templates/template_counts_host.parquet`
  and one aggregate over `events` (service x host lines). A host's profile is its template mix over the whole
  span, compared as `sqrt(share)` (Hellinger). Display clusters: k-means (fixed seed) chooses k in 2..10 by
  silhouette among the k where every cluster keeps at least 3 hosts (the silhouette chart lists every candidate
  with the reason an excluded k was left out); Ward linkage on the same vectors gives the dendrogram. Scoring and
  the contextual detector (issue 5) use a separate peer group: a host's up to 8 nearest hosts within a Hellinger
  distance of 0.15 (`peers.parquet`, `peer_centroids.parquet`, leave-one-out); a host with fewer than 3 has no
  valid peer group and none is stored. `baseline.parquet` holds, per host with peers, the median and MAD of its
  1-minute window distances to its peer centroid and its median lines per window. An outlier host has no peer
  group and a robust z of its distance to the 3rd nearest host above 3.5 within a cluster of at least 8 hosts,
  so a tight second mode is not 26 outliers; the three most deviating templates against the peer centroid are the
  evidence. Accessors `load_assignments`, `load_clusters`, `load_peers`, `load_baseline`, `load_centroids`,
  `load_peer_centroids` (a `Centroids` object keyed by id, no index arithmetic) and `centroid_distance` (sparse
  window counts without densifying). The page reads these files only: silhouette per k, a 2D projection, cluster
  and outlier tables ("top 25 of N"), a tall scrolling dendrogram or one collapsed to the cut clusters, and the
  service x host heatmap in dendrogram order. Whole span only (time and level filters do not apply). Real data:
  162 hosts, k = 7 (silhouette 0.87; k = 8 to 10 excluded, a cluster of one host), clusters of 122, 18, 9, 4, 3,
  3 and 3 hosts, k-means and Ward agree (ARI 1.0), 139 hosts have a valid peer group and 23 do not (the idle and
  very large hosts and the three-host groups), one outlier host (eselda11u11s07, z 58); stage 4 s.

## 9. Open decisions

- Default window for matrices: 1 minute (switchable). Default grouping: per
  service (switchable to per host).
- Whether to keep full `Payload` in `events/` or only masked text + parameters
  (full text kept by default; revisit if the cache grows too large).
