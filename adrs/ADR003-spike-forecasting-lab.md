# ADR003: Spike forecasting lab

## Status

accepted

## Context

Issue #23 adds a forecasting lab: given what a host logged up to minute `t`, will it have a
volume spike in the next `H` minutes? It is split into stories (#50 to #54). Later stories
train models, so they must all read the same labels, features and split. The data is 24 hours
long and about 1 % of the host-minutes are positive, so a definition that drifts between
stories makes their results incomparable. Until this ADR (and its numbering) the contract
lived in the issue body of #50, which called it ADR002; ADR002 was taken by the tag filters,
so this is ADR003.

## Decision

- **Spike label.** A host-minute is a spike minute when it lies inside an episode returned by
  `analyses.temporal.find_bursts(x, settings.burst_k)` on the host's dense 1-minute series from
  `temporal/host_60.parquet`: the volume detector's host episodes before `merge_periodic` and
  the anomaly caps. The span is the one of the detector (`rate_60` and `host_60` bins). Rows of
  the host `(missing)` are dropped. On the real cache: 162 hosts, 1,439 minutes, 233,118
  host-minutes, 270 episodes on 16 hosts, 1,059 spike minutes. (The 271, 17 and 1,079 of the
  split comment on #23 counted the `(missing)` host.)
- **Target** for horizon `H` minutes: `y(t) = 1` when any minute in `(t, t+H]` is a spike
  minute (`forecast.targets`).
- **Feature groups** (the checkbox values of later stories: `templates`, `levels`, `services`,
  `peers`, `lags`) and their columns: `tpl_<template_id>` (`log1p` of the host's lines per
  minute of each of the 20 templates with the most lines), `lvl_<level>` (share of each level
  of `ingest.normalize.LEVELS`, 0 in an empty minute), `svc_<service>` (`log1p` of the lines of
  each of the 20 busiest services), `peer_rate` (`log1p` of the mean per-minute rate of the
  host's peers from `hosts/peers.parquet`; for a host without a peer present in the frame, the
  median over all frame hosts of that minute, empty minutes included), `lag_rate` (`log1p` of
  the host's rate) and `lag_z` (trailing robust z from `rolling_baseline(x, trailing=True)`,
  so no future minute enters it). All other features are values of the same minute.
- **Stored files** in `cache/forecast/`: `frame.parquet` with one row per host and minute of
  the whole span (`bin, host, spike` and one Float32 column per feature; empty minutes are
  zeros), ordered by host and then by minute so a host's series is one contiguous block;
  `spike_counts.parquet` (`bin, hosts`) for the page's chart; `meta.json` with `start`, `end`
  (exclusive), `minutes`, `hosts`, `host_minutes`, `burst_k`, `groups`, `templates` and
  `services` (column, id or name, text, `lines`), `episodes`, `spike_minutes`, `spike_hosts`,
  `no_peer_hosts`, `split`, `horizons` and `positive_rate`.
- **Time split.** Train is the first 7/12 of the span, validation the next 1/12 and test the
  last 1/3 (on the real data 14 h, 2 h and 8 h). A sample whose target window reaches into
  the next period is dropped. `split_bounds`, `period_index`, `targets`, `sample_mask` and
  `positive_rates` in `logdash.analyses.forecast` are the one definition; later stories import
  them.
- **Code layout.** The stage and page are `src/logdash/analyses/forecast.py` (key `forecast`,
  title `Forecasting`, after `hosts`, `depends_on = ("temporal", "templates", "hosts")`,
  `setting_names = ("burst_k",)`). The learning code of later stories goes in
  `src/logdash/forecast/`.
- **Synthetic data.** `SyntheticSpec.forecast_spikes` injects spikes on whole minutes, each
  after a few WARNING lines of one template on the same host, round-robin over the hosts
  except the silent host and the spike host. With `forecast_spikes=0` the data is byte for
  byte what it was.
- **Filters.** The global filters do not apply to the lab: it always uses every host and the
  whole span.

## Consequences

- A change of `LOGDASH_BURST_K` rebuilds the stage.
- The frame is about 50 Float32 columns times 233 thousand rows.
- A change to `find_bursts` must bump `Forecast.version` as well as `Volume.version`, or the
  labels drift from the detector. A test compares them on the synthetic cache.
- Later stories can train on `frame.parquet` without sorting; the page reads only
  `meta.json` and a 1,439-row file.
