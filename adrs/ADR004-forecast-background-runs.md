# ADR004: Forecasting runs as background jobs

## Status

accepted

## Context

Issue #51 puts model runs on the Forecasting page. #59 adds the first run (the persistence
baseline); #60 (logistic regression), #52 (recurrent network) and #54 (placement view) extend
it. A run takes seconds to minutes, so it cannot be an ordinary callback: the dashboard keeps
answering other requests while it runs, and the user can cancel it.

Measured while planning #59: a `multiprocessing` fork child that calls polars after the parent
process has used polars hangs, because rayon's thread pool does not survive `fork`. The same
child reading the file with pyarrow finishes in 0.04 s. The dashboard uses polars in every
callback, so the job process must avoid it.

## Decision

- Runs are Dash background callbacks with a `DiskcacheManager` whose cache is
  `<LOGDASH_RUNS>/jobs`. One job function,
  `logdash.forecast.job.run_forecast_job(set_progress, store, runs_dir, params, run_id=None)`,
  does the work; tests call it directly with a fake `set_progress`. The stages are in
  `job.STAGES`, and progress goes through `job.progress_value(stage, stages, fraction)`. The
  `running` list and the `progress` and `progress_default` lists in `register_callbacks` are
  extended by later stories (both progress lists keep the same length).
- **The job process never calls polars or DuckDB.** It reads `frame.parquet` with pyarrow and
  `meta.json` with `json`. Libraries that need threads after the fork (torch in #52) are
  imported inside the job. `tests/unit/test_forecast_lab.py` runs the job in a forked process
  to guard this.
- Run directory `<runs_dir>/<run_id>/`: `scores.npz` with the keys `labels`, `host`, `minute`
  and `score_<model>` (extra arrays are allowed, in the order of the test samples), and
  `run.json` with `run_id`, `created`, `settings`, `data`, `hosts`, `test_period`, `models` and
  `metrics` (extra keys are allowed). The directory is written once, into `.tmp-<run_id>` and
  then renamed, and never changed. Run ids are `YYYYMMDDTHHMMSSZ-xxxxxx`; the id comes back
  from the browser, so `load_run` accepts only ids of that form.
- Samples: `data.period_samples` is the full grid of hosts times the minutes of a period that
  `sample_mask` keeps, host-major. Every model is scored on exactly the test samples.
- Metrics: `metrics.score_metrics` (samples, positives, positive rate, AUROC, PR-AUC) is stored
  per model. Accuracy and F1 at the slider threshold are computed by the page from the stored
  scores. Undefined values are `None` and the page says why (no samples, no spikes, no
  spike-free samples).
- The persistence baseline scores `y(t - H)`: 1 when the host had a spike minute in
  `(t - H, t]`, with the window clipped at the span start.

## Consequences

- Runs accumulate under `runs/` (about 1 MB per run on the real cache) and may be deleted at
  any time; the directory is git-ignored.
- A run reflects the cache at its time; its `data` block says which.
- The results callback never recomputes a model; it loads the run (memoised) and counts.
