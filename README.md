# logdash

Interactive dashboard for analysing the OpenStack log dataset in `data/`
(38.8 M log lines, 24 h, 162 hosts, 40 services). Expensive work runs once in a
precompute step that writes Parquet and JSON to `cache/`; the Dash app only reads
that cache and answers every interaction in well under 300 ms on a laptop.
See [PLAN.md](PLAN.md) for the design and status.

## Data

`data/flog-data_*_preprocessed.csv.zst` (759 MB compressed, about 13 GB as CSV):
Unix timestamp, log level, program, host, logger, Python module, request, user and
tenant ids, pid and the free-text payload. Levels are mixed case, 1,055 rows are
malformed (flagged, kept) and the payload is highly templated. Time is handled in UTC.
`data/` and `cache/` are git-ignored.

## Quick start

```bash
uv sync                  # install
uv run logdash build     # precompute into cache/ (only stale stages)
uv run logdash serve     # build if stale, then serve on http://127.0.0.1:8050
```

Other commands:

```bash
uv run logdash build --only templates --force   # rebuild one stage
uv run logdash build --strict                   # fingerprint file content, not mtime
uv run logdash status                           # cache state per stage
uv run logdash serve --no-build --port 8051     # serve the existing cache as is
```

### Build time and cache size

Reference laptop (16 cores, 31 GB RAM, DuckDB limited to 8 GB / 8 threads), real data:

| Stage | Time |
|---|---|
| events (normalised, hour-partitioned Parquet) | 97 s |
| overview, distributions, temporal, templates, explorer | under 8 s each (measured before the templates stage also wrote the per-host counts) |
| features | about 1 s for a synthetic 600k-row template count table (not yet measured on the real data) |
| pca | 12 fits (group x window x scaling); about 4 s for a synthetic 230k-window x 281-template matrix at 3 % density (not yet measured on the real data) |
| hosts | 3 s on the real data (162 hosts, 281 templates, 559 service x host pairs; one aggregate over `events` for the matrix) |
| anomalies | 1.3 s (three detectors: PCA residual, 73,421 windows above the limit, 11,446 kept: at most 100 per service or host; volume, 87 episodes; contextual, 496 windows above the threshold, 456 kept) |
| total | 1 min 58 s (measured before the features, pca and anomalies stages existed) |

`cache/` is 622 MB (281 templates from 73,583 unique masked lines).

## Pages

| Page | Content |
|---|---|
| Overview | data quality: rows, span, missing values per column, level normalisation, malformed rows |
| Distributions | level, program, host, logger, module, tenant, payload length, lines per request, request duration, inter-arrival times; linear/log |
| Temporal | event rate (1 s to 5 min bins by zoom), host x time heatmap, autocorrelation and periodogram, bursts and gaps; drag on the rate chart to set the global time range |
| Templates | Drain3 templates: top, volume over time, parameter distributions, rare and novel lists |
| Features | window x template count matrix (1, 5, 15 min; per service or host; log1p or TF-IDF): template correlation heatmap, template-entity association, variance ranking, mutual information |
| PCA | PCA of the window x template matrix (precomputed per grouping, window and scaling): scree plot, 2D and 3D projections coloured by time, service or host, or SPE; loadings per component; residual SPE per window against its control limit, and the windows above it with their top residual templates |
| Anomalies | every registered detector's anomalies: score timeline coloured by type, per-type filter, table sortable (the whole selection) by score, time, type or entity, "N of M kept" per type, with a link per row to the raw lines of its window in the explorer |
| Hosts | host profiles (template mix per host): silhouette for every candidate k, 2D projection, cluster table, outlier hosts (and the hosts without peers) with the templates that deviate, Ward dendrogram (every host, or collapsed to the clusters) and the service x host matrix in dendrogram order |
| Explorer | paged grid over raw events with filters, template, request id and text search; other pages link here |

Global filters (date range, level, service, host) live in the sidebar. The time
range is respected by Temporal, Templates, Features, PCA, Anomalies and Explorer; Hosts uses the whole span (its host and service filters select rows and columns); the other pages state which
filters they honour.

Screenshots (`docs/screenshots/`):

![overview](docs/screenshots/overview.png)
![distributions](docs/screenshots/distributions.png)
![temporal](docs/screenshots/temporal.png)
![temporal, dark theme](docs/screenshots/temporal-dark.png)
![templates](docs/screenshots/templates.png)
![explorer](docs/screenshots/explorer.png)

## Anomaly scores from PCA

The `pca` stage keeps, for every grouping (`service`, `host`), window (60, 300, 900 s) and
scaling (`log1p`, `tfidf`), the per-window SPE (squared norm of what the first `k`
components, keeping 95 % of the variance, do not explain) with its Jackson-Mudholkar control
limit (Gaussian theory at 99.9 %: a reference line, not a false-alarm rate), and the three templates that contribute most to each window's residual.
Downstream modules read them with `pca.load_spe(store, group, window_s, scaling)`
(`row, bin, entity, spe, limit, ratio`; rank by `ratio = spe / limit` rather than the flag) and `pca.load_residuals(...)`
(`row, rank, template_id, residual, share`); `row` is the row of the features index.

## Host clusters

The `hosts` stage (module 7) clusters hosts on their template mix over the whole span and finds
every host's peers for the contextual detector. A host's profile is `h = sqrt(share)` of its
template counts (Hellinger space). Two groupings are kept apart:

- *clusters* (k-means, `k` from 2 to 10 by silhouette among the `k` where every cluster has at
  least 3 hosts; the silhouette chart shows every candidate `k` and why an excluded one was left
  out) are the overview of the fleet, with a Ward dendrogram on the same profiles. They can be
  lopsided or have several modes, so hosts are not scored against them;
- *peers* are a host's up to 8 nearest other hosts within a Hellinger distance of 0.15. A host
  with fewer than 3 such hosts has no valid peer group (`peer_ok` false) and gets no peer
  centroid or baseline. On the real data 23 of 162 hosts have none (idle hosts, the three-host
  groups and the single very large hosts).

An outlier host has no valid peer group and a robust z (median and MAD of its distance to its
3rd nearest host within its cluster) above 3.5; clusters of fewer than 8 hosts get no z. A tight
sub-population far from its cluster mean is therefore not a set of outliers.

Files in `cache/hosts/`:

| File | Content |
|---|---|
| `hosts.parquet` | `row, host, events, cluster, hier_cluster, distance, peer_distance, peers, peer_ok, z, outlier, leaf, x, y`: `distance` to the cluster centroid, `peer_distance` to the 3rd nearest host, `peers` the number of peers within the radius |
| `clusters.parquet` | `cluster, n_hosts, scored, median_distance, median_peer_distance, scale, max_peer_distance, no_peers, outliers`; `z = (peer_distance - median_peer_distance) / scale` in a scored cluster |
| `centroids.parquet` | long, `cluster, template_id, share, root`: `root` is the cluster centroid in `h` space, `share` the mean template share (display) |
| `peers.parquet` | `host, peer, distance`, the peers of each host with a valid peer group |
| `peer_centroids.parquet` | long, `host, template_id, root`: mean `h` of the host's peers (the host itself excluded) |
| `baseline.parquet` | `host, windows, median_lines, median, mad`: median and scaled MAD of the host's 1-minute window distances to its peer centroid (windows with at least one line), and the median lines per window |
| `deviations.parquet` | `row, host, rank, template_id, delta, share`: the three templates that contribute most to a host's squared distance to its peer centroid (cluster centroid without peers) |
| `linkage.parquet`, `service_host.parquet`, `fit.json` | Ward linkage matrix, lines per host and service, chosen `k` and every candidate with its silhouette and exclusion reason, thresholds, template ids |

To score a host window, build the window's counts (dense or sparse, one row per window, columns
in the order of the template ids), then

```python
cen = hosts.load_peer_centroids(store)  # Centroids: .keys (hosts), .matrix, .rows(), .has()
base = hosts.load_baseline(store)  # median, mad per host; hosts without a row have no peers
d = hosts.centroid_distance(
    counts, window_hosts, cen
)  # KeyError for a host without a peer centroid
```

and compare `d` with the host's `median` and `mad` (a window of `n` lines is about `1 / sqrt(n)`
noisier, `median_lines` gives the usual `n`). `Centroids.rows` maps ids to matrix rows, so cluster
ids (1-based) and rows (0-based) are never mixed up; `hosts.load_centroids(store)` returns the
cluster centroids the same way, `hosts.load_assignments(store)` the per-host columns.

## Adding an anomaly detector

Anomaly detection (module 6) has a framework and a page; the detectors are plug-ins. Create
`src/logdash/detectors/<name>.py` with a class implementing `Detector` from
`detectors/base.py` and decorate it with `@register`. Nothing else changes: the package imports
every module in `detectors/`, the `anomalies` stage runs all registered detectors, and the page
follows the data (type filter, timeline, table, explorer links).

- Attributes: `key` (value of the `detector` column), `type` (anomaly type; several detectors
  may share one, for example `structural`), `title`, `version`, `depends_on` (analyses whose
  cache it reads; the stage depends on the union and is rebuilt when a version changes, so put
  the module constants that change the output into `version`), `threshold` (raw score at which a
  row counts as anomalous, above 0) and `setting_names` (settings that enter the cache
  fingerprint, `()` if none). A detector whose threshold depends on settings may define
  `threshold_for(settings)`. The registry checks the class against the `Detector` protocol.
- `detect(ctx)` gets a `DetectContext` (`store`, `settings`, `log`, `meta`: a dict to fill with
  JSON values, such as how many entities were not scored) and returns a polars frame
  with `bin` (window start, UTC), `window_s`, `kind` (what `entity` is, see below), `entity`,
  `raw` (larger is more anomalous), `evidence` (text) and optionally `template_id`. Return every
  row you consider anomalous and do not sort and cut first: the framework caps per entity.
  Read other stages through `ctx.store` (for example `pca.load_spe`). Do not import
  `analyses.anomalies` from a detector (import cycle).
- Entity kinds are declared once, in `detectors/base.py`: `declare_kind(name, link_param,
  filter_field, link_window)` says which explorer parameter the entity goes into, which global
  filter (`services`, `hosts`) restricts rows of that kind, and whether the explorer link is
  limited to the anomaly's window (not for a request, whose lines lie outside it). Declared:
  `service`, `host`, `request`. A detector returning an undeclared kind makes the stage record
  an error naming the detector and the kind; the declaration is validated against the
  explorer's parameters and `Filters`. The page, links and filters follow the declaration.
- `cache/anomalies/anomalies.parquet` has one row per anomaly: `detector, type, bin, window_s,
  kind, entity, template_id, raw, score, evidence`. The framework drops rows with `raw` below
  the detector's threshold (null, NaN, zero and negative values too; `+inf` counts as far above
  it), scores the rest, keeps the 100 highest per (kind, entity) so that a sustained incident on
  a few hosts does not hide an isolated anomaly on a quiet one, and then the 20,000 highest per
  detector.
- The score is in `[0, 1]`: `0.5 * percentile + 0.5 * magnitude`. `percentile` is the row's rank
  among the detector's rows of the same kind (ties share the average rank); `magnitude` is
  `log2(raw / threshold)` (raw clipped at 1024 times the threshold) divided by the excess at the
  99th percentile of the same kind and clipped to 1. It keeps its spread, does not saturate,
  and puts service and host rows of the same standing side by side; it orders rows within a
  detector and kind and compares them across detectors by position, it is not a probability.
- `meta.json` lists each detector with `above_threshold`, `rows` (kept), `dropped`, `seconds`, an
  optional `info` (what the detector put in `ctx.meta`; its `note` text is shown on the page)
  and, for a detector that raised, `error`: a failing detector is logged and skipped, the others
  still run, and the page names it. The page shows "kept N of M" per type.
- The page links each row to `/explorer?[start=&end=&]service=|host=|request_id=[&template=]`
  (start and end only for kinds whose lines stay inside the window).

`structural_pca.py` is the PCA residual half of the structural type:
windows of the per-service and per-host 1-minute log1p fits with `spe / limit` of at least 1,
with the top residual templates as evidence (on the real data 73,421 windows, 11,446 kept). Issue 6 adds the
Isolation Forest next to it.

`contextual.py` is the contextual type: a host's 1-minute template mix against its peers
(`analyses/hosts`: the host's nearest hosts within the Hellinger radius, not its k-means cluster). A window's
distance to the host's peer centroid (`centroid_distance`) is compared with what that host's windows of the same
line count usually have: a per-host fit `expected(n) = a + b / sqrt(n)` (the multinomial noise of a small window
falls with `1 / sqrt(n)`), a scale from the MAD of the residuals that widens by `sqrt(median lines / n)` for
sparse windows, and `u = (distance - expected) / scale`. The centroid is a whole-day mean, so the host is also
compared with its peers in the same minute: `z = u - median(u of its peers in that minute)` (the fleet's median
when fewer than 3 peers have a window), which leaves out events that hit many hosts at once (they are for the
structural and volume detectors). The raw score is `z`; the threshold 8 is about the 99th percentile of `z` on the
real data (8.4; the 99.9th is 17, both in the detector's meta), and a window is kept only if the template that
contributes most to its distance has at least 3 lines (one or two stray lines of a rare template are noise).
Windows of fewer than `max(30, half the host's median lines)` lines are not scored; on the real data every window
has at least 46 lines, so that rule and the widening only matter for sparse hosts. The 23 hosts without a valid
peer group are not scored, and these are the hosts most likely to be odd: their number is in the page's hint line
and their names are in the detector's `meta.json` entry (`info.unscored_names`, with `unscored_hosts`, the number
of windows scored and the `z` quantiles). Evidence: the three templates that contribute most to the squared
distance (ties by line count), with their share of the distance, their lines, their share of the window and the
peers' profile (the peer centroid squared, at most the peers' mean share, so shown as a profile). Real data: 2,299
of 199,882 scored windows have `z >= 8`, 496 pass the evidence rule, 456 are kept (41 hosts), 0.9 s; 11 of the 496
fall in minutes where 5 or more hosts are flagged (2,900 of 4,774 against the whole-day centroid).

`volume.py` is the volume spike/drop type. It uses `find_bursts`, `find_drops` and `find_gaps` of
`analyses/temporal.py` on the 1-minute rate tables. A row is an episode (`bin` its first minute,
`window_s` its length). Spikes (per service and host) score the robust z-score at the peak
(rolling median and MAD over one hour), threshold `burst_k`. Drops (per service and host) are the
low side: minutes below the trailing hour's median minus `burst_k` robust sigmas and at most half
of it, for entities whose baseline is at least 50 events/min, scored by the z-score at the trough
(a busy service falling silent is a drop). Gaps (per host) are silent runs of at least
`gap_minutes` and score `burst_k * minutes / gap_minutes`; gaps and drops never count the time
before an entity's first or after its last line. Episodes of one entity and kind that recur at a
fixed period are merged into the strongest one (evidence says how many and how often). On the real
data this leaves 87 episodes of 42 entities (87 spikes, no drops or gaps: every host logs
throughout the 24 hours); 426 before periodic repeats were merged.

## Adding an analysis

Create `src/logdash/analyses/<name>.py` with a class implementing the `Analysis`
protocol from `analyses/base.py`:

- `key` (cache subdirectory and URL path), `title`, `version` (bump to invalidate
  the cache), `depends_on` (upstream keys), optional `setting_names` (settings that
  enter the fingerprint);
- `compute(ctx)` writes Parquet/JSON into `ctx.out_dir` using `ctx.con` (DuckDB with
  the `events` view); `layout(store)` returns the page; `register_callbacks(app, store)`.

Decorate the class with `@register` and add the module name to `BUILTIN` in
`analyses/__init__.py`. The shell builds navigation and pages from the registry and
`logdash build` runs stale stages in dependency order.

Conventions:

- Component ids are prefixed with the key (`temporal-rate`). Take the global filters
  from `FILTER_STORE_ID` via `from_dict`, and the theme from `THEME_STORE_ID`; pass the
  theme to figure builders (`app/figures.py`).
- Callbacks read pre-binned aggregates; query `events` only with `LIMIT` and pushed-down
  hour filters (see the rules in PLAN.md 4.4). Never interpolate user values into SQL
  (`where_clause` parameterises).
- Keep work in pure functions (`compute_*`, `*_figure`) and callbacks thin. Use
  `app/ui.py` helpers for pages, cards and tables.
- Tests: build the stage on the synthetic data (`built` and `build_with` in
  `tests/conftest.py`), assert on figure data and call callbacks directly. Coverage must
  stay at or above 90 %.

## Configuration

Environment variables; CLI flags (`--data`, `--cache`, `--memory-limit`, `--threads`) override.

| Variable | Default | Meaning |
|---|---|---|
| `LOGDASH_DATA` | `data` | directory with `*.csv.zst` |
| `LOGDASH_CACHE` | `cache` | precompute output |
| `LOGDASH_MEMORY_LIMIT` | `8GB` | DuckDB memory limit |
| `LOGDASH_THREADS` | `8` | DuckDB threads |
| `LOGDASH_BURST_K` | `6` | burst threshold in robust sigmas above the rolling median |
| `LOGDASH_GAP_MINUTES` | `10` | silent minutes that make a gap |
| `LOGDASH_DRAIN_SIM_TH` | `0.4` | Drain3 similarity threshold |
| `LOGDASH_DRAIN_DEPTH` | `4` | Drain3 tree depth |
| `LOGDASH_TEMPLATE_TOP_K` | `50` | templates with parameter extraction |
| `LOGDASH_PARAM_SAMPLE` | `2000` | payloads sampled per top template |
| `LOGDASH_RARE_THRESHOLD` | `5` | a template with at most this many events is rare |
| `LOGDASH_NOVEL_FRACTION` | `0.1` | novel: first seen in the last fraction of the span |

## Tests

```bash
uv run pytest                      # unit and integration tests, 90 % coverage gate
uv run pytest -m slow --no-cov     # latency benchmark on the real ./cache (skipped if absent)
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

The benchmark times every page layout and callback (no filter, one hour, host,
template, text search, brushing, anomaly types and sorts) and asserts each is under 300 ms; run it with `-s`
to see the timing table.

## Building elsewhere

`cache/` is plain Parquet and JSON and portable. Run `uv run logdash build` on a
bigger machine (set `LOGDASH_THREADS` and `LOGDASH_MEMORY_LIMIT`), copy the whole
`cache/` directory to the laptop and run `uv run logdash serve --no-build`. The app
never reads `data/`.
