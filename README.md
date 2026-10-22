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
uv run logdash build --only tags --force        # retry untagged templates (answers are memoised)
uv run logdash build --strict                   # fingerprint file content, not mtime
uv run logdash status                           # cache state per stage
uv run logdash serve --no-build --port 8051     # serve the existing cache as is
```


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
| `LOGDASH_TAG_MODEL` | `anthropic/claude-haiku-5.5` | OpenRouter model for semantic template tags (it must support structured outputs) |
| `OPENROUTER_API_KEY` | (unset) | enables the `tags` stage; without it the stage is skipped |

Variables can also be set in `.env` in the working directory; the real environment wins.
Only `logdash build` calls the model; `serve` builds every other stale stage but leaves
`tags` as it is.

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
