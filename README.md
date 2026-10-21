# logdash

Interactive dashboard for analysing the OpenStack log dataset in `data/`.
See [PLAN.md](PLAN.md) for the design.

## Usage

```bash
uv sync                          # install
uv run logdash build             # precompute into cache/ (only stale modules)
uv run logdash build --only templates --force
uv run logdash status            # show cache state per module
uv run logdash serve             # start the dashboard on :8050
uv run pytest                    # tests + coverage gate (>= 90 %)
uv run ruff check . && uv run ruff format --check .
```

Configuration via environment variables (CLI flags override):

| Variable | Default | Meaning |
|---|---|---|
| `LOGDASH_DATA` | `data` | directory with `*.csv.zst` |
| `LOGDASH_CACHE` | `cache` | precompute output |
| `LOGDASH_MEMORY_LIMIT` | `8GB` | DuckDB memory limit |
| `LOGDASH_THREADS` | `8` | DuckDB threads |

## Adding an analysis

Create a module in `src/logdash/analyses/` with a class implementing the
`Analysis` protocol (`key`, `title`, `version`, `depends_on`, `compute`, `layout`,
`register_callbacks`), decorate it with `@register`, and add the module name to
`BUILTIN` in `analyses/__init__.py`. `logdash build` runs stale modules in
dependency order.
