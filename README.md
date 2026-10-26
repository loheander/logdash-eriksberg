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
uv run logdash spot-check sample                # review sheet of 30 tagged templates (cache only)
uv run logdash spot-check score                 # agreement from the marked sheet
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
| `LOGDASH_JEV_MODEL` | `typesafe/jev-1.14` | decision model of the triage page |
| `LOGDASH_JEV_TIMEOUT_S` | `10` | seconds per attempt of a triage call (one retry at most) |
| `OPENROUTER_API_KEY` | unset | key for the triage page's live calls; without it the page shows a banner |

## Anomaly triage (`/triage`)

The detectors say how unusual a window is, not whether anyone must act. On `/triage`, a
click on an anomaly (a grid row or a point in the scatter) sends one request to the Jev
decision model on OpenRouter (`typesafe/jev-1.14`) and shows the answers in a drawer:

- **actionable** and **user impact**: P(yes), with a tick at 0.5;
- **nature**: `error_fault`, `config`, `load`, `periodic`, `lifecycle`, `noise` or `unclear`;
- **severity**: 0 (informational) to 4 (critical), as a gauge at the expected level;
- the confidence of each answer, with a "check" badge when it is below 0.5 or the top two
  probabilities are within 0.1.

The call is live and not cached: a timer runs while it waits, and the delay is split into
the state query, the Jev call and the rest (about 0.3 to 0.5 s in all). "ask again" repeats
the call, and "show raw prompt" opens the exact request (context, item and questions) and
the response as returned. The questions are a constant in `analyses/triage.py`; the drawer footer shows the
model and the hash of the spec. The subsystem of a row comes from its service name, not
from the model.

**What is sent.** Each click sends the row's detector, entity (host names and request ids as
they are), subsystem, window, evidence, the masked text of up to five templates
(`<IP>`, `<UUID>`, `<PATH>`) and the line counts per level to OpenRouter and TypeSafe. Raw
payloads are not sent. A call costs about $0.00008.

Set the key before starting the server:

```bash
export OPENROUTER_API_KEY=...
uv run logdash serve
```

Without the key, the page shows a banner, the grid and scatter still work, and the drawer
shows the error. Failed calls and unexpected answers are shown as errors, never as defaults.

**Demo script.** Three rows show what the model adds to the detector score:

1. `volume`, `neutron-l3-agent`, score 1.0, "96 episodes in all, repeating about every
   77 min": the highest score, but *periodic*, not actionable, informational.
2. `severity`, `barbican-api`, score 0.75, "ERROR burst ... over 112 min": a lower rank,
   but an *error_fault*, actionable, high severity.
3. `contextual`, host `eselda12u01s09`, "could not open network device (No such device)":
   a *config* problem on one host, medium severity.

Switching between them shows the bars and the needle move from one answer to the next.

## Tests

```bash
uv run pytest                      # unit and integration tests, 90 % coverage gate (no model calls)
uv run pytest -m slow --no-cov     # latency benchmark on the real ./cache (skipped if absent)
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

The benchmark times every page layout and callback (no filter, one hour, host,
template, text search, brushing, anomaly types and sorts, the triage page with a fake model) and asserts each is under 300 ms; run it with `-s`
to see the timing table.

## Building elsewhere

`cache/` is plain Parquet and JSON and portable. Run `uv run logdash build` on a
bigger machine (set `LOGDASH_THREADS` and `LOGDASH_MEMORY_LIMIT`), copy the whole
`cache/` directory to the laptop and run `uv run logdash serve --no-build`. The app
never reads `data/`.

## Agent SDLC

Issues on the [project board](https://github.com/users/loheander/projects/1) are
planned, implemented, reviewed and merged by Claude Code agents. See
[ADR001](adrs/ADR001-agent-orchestrated-sdlc.md) for the design and
`.claude/sdlc/protocol.md` for the rules.

```bash
claude --agent orchestrator "/orchestrate-kanban dry-run"      # what would be dispatched
claude --agent orchestrator "/orchestrate-kanban only 26"      # run one issue end to end
claude --agent orchestrator "/loop 30m /orchestrate-kanban"    # keep working the board
```

Backlog → planner (plan comment) → Ready → implementer (PR) → In review → reviewer
(review rounds, fixes by the implementer) → Jev gate → Done (auto-merged) or Human
review. Every 5 finished PRs a retrospective opens a PR that tunes the agents. Humans
answer `needs-human` questions on issues (then remove the label) and merge Human-review and retrospective PRs.
