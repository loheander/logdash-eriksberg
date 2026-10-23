# Plan: semantic tag filters on the Explorer page (story #30), v1

## 1. Story

Story [#30](https://github.com/loheander/logdash-eriksberg/issues/30), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24). Depends on #28 (tagging
stage, PR [#34](https://github.com/loheander/logdash-eriksberg/pull/34)) and #29 (shared
tag-filter module, PR [#35](https://github.com/loheander/logdash-eriksberg/pull/35)).
Feature branch `feature/semantic-template-tags`, story branch
`feature/semantic-template-tags-30-explorer-tag-filters`.

**Goal.** The Explorer page can hide noise, show only actionable lines and restrict to a
component, based on the semantic tags of each line's template.

**Acceptance criteria** (copied from the story):

- [ ] AC1: Filters "hide noise", "actionable only" and component restrict the event list to lines whose template carries matching tags.
- [ ] AC2: The filters combine correctly with the existing filters and with a `template` link from another page.
- [ ] AC3: Without tags in the cache, the filters are hidden and the page works as today.
- [ ] AC4: Callbacks stay under 300 ms (benchmark extended). Coverage stays at or above 90 %.

Out of scope: Templates and Anomalies pages (#29, #31).

**Precondition.** #28 and #29 must both be merged into `feature/semantic-template-tags`.
Before step 1, merge `origin/feature/semantic-template-tags` into the story branch. If #29
is not merged yet, stop and report; do not copy its module. This plan uses, unchanged:

- From #28 (`plans/2026-10-22-tagging-stage-v2.md`, step 8):
  `logdash.analyses.tags.tags_frame(templates, tags: Mapping[str, Tag]) -> pl.DataFrame`,
  `logdash.tagging.model.Tag(actionable, component, noise, summary)`.
- From #29 (`adrs/ADR002-semantic-tag-filters-on-pages.md`,
  `plans/2026-10-22-templates-page-tags-v1.md`, step 1), module
  `logdash.app.tag_filters`: `TagFilter`, `tag_filter(hide_noise, actionable_only,
  components)`, `load_tags(store)`, `attach_tags(templates, tags)`,
  `apply_tag_filter(df, tag_filter)`, `tag_controls(prefix, visible)`.

If the merged names differ only in spelling, adapt the imports. If their behaviour differs
(for example, `load_tags` never returns `None`, or `tag_controls` uses other control ids
than `<prefix>-hide-noise`, `<prefix>-actionable-only`, `<prefix>-components`), stop and
send the story back with `needs-replan`. Do not change `tag_filters.py` in this story.

## 2. Context

- `src/logdash/analyses/explorer.py`
  - `:25-33` constants `KEY`, `PAGE_SIZE`, `COLUMNS`, `SELECT`.
  - `:72-115` `build_query`: global filters (`where_clause`), hour pushdown, the template
    restriction as `masked_hash IN (SELECT masked_hash FROM read_parquet('<assignments>')
    WHERE template_id = ?)` (`:99-104`), request id, text, then `ORDER BY ts LIMIT … OFFSET …`
    or `LIMIT limit`.
  - `:118-141` `fetch_page`, `:144-187` `search_page` (text search, hour by hour on a time
    budget). Both pass `store.path("templates", "assignments.parquet")`.
  - `:220-281` `layout`: inputs group (template id, request id, text), link text, paging
    buttons, grid, stores, description (`:277-280`).
  - `:283-351` `register_callbacks` / `update`: one callback for everything; any trigger
    other than the paging buttons returns to page 0 (`next_page`, `:190-196`).
- Events carry no template id. They are matched to templates through `masked_hash` and
  `cache/templates/assignments.parquet` (`masked_hash UInt64, template_id Int64`, 73 583
  rows on the real cache). Events with a null payload have a null `masked_hash`
  (`src/logdash/ingest/normalize.py:104`) and belong to no template (1 055 such lines in the
  real cache).
- `src/logdash/analyses/templates.py:150-182`: `templates.parquet` and
  `assignments.parquet` are written by the same run, so their ids always agree.
- ADR002 (from #29), "Consequences": Explorer takes `templates.parquet`, calls
  `attach_tags` then `apply_tag_filter`, and restricts its SQL to the resulting ids.
- Tests: `tests/unit/test_explorer.py` (module fixtures `exp`, `con`; helper `page`;
  `test_layout_prefilled_and_callback` `:114-177` posts to `/_dash-update-component` with
  an `inputs()` helper listing every callback input). `tests/unit/test_temporal.py:378-383`
  has a `walk` helper. Synthetic texts: "Duplicate iptables rule…",
  "internal error: connection <NUM> closed unexpectedly by peer <IP>",
  "Unexpected bgp session reset…" (20 lines) (`tests/synthetic.py:62,109,87`).
- `tests/benchmark/test_latency.py:348-369` `test_explorer`, `:105-113` `_cases`,
  `:92-96` `_hour_filters`. `README.md:62-63` benchmark description.

**Measured on the real cache** (38.8 M lines, 281 templates, DuckDB 1.5.6, 2 threads,
machine under load average 11-20, so absolute numbers are high): the current one-step
query restricted to the 53 templates whose text looks like a failure (118 579 lines, a
stand-in for "actionable only") took 260-850 ms warm. The same restriction done in two
steps (section 3) took 170-320 ms. "Hide noise" as an exclusion of template 1 (6.8 M
lines) took 110-200 ms either way. 35 % of the lines share their millisecond timestamp
with another line, so the two-step query must handle ties exactly.

## 3. Design

No new ADR: this story applies ADR002 and makes no decision that binds other stories.

**Controls.** `tag_controls("explorer", with_tags)` from the shared module, as its own row
under the existing inputs. Without tags it is rendered hidden, so `update` keeps its inputs
(ADR002, decision 5). Changing a control returns to page 0 like every other filter.

**From tag filter to template ids.** New `TagRestriction(ids, keep)` and
`tag_restriction(store, tag_filter)` in `explorer.py`:

- `None` when the filter is off or there are no tags: the query is exactly as today (AC3).
- Otherwise read `templates.parquet` (`template_id`, `template`), `attach_tags`,
  `apply_tag_filter`.
- If the filter is only "hide noise", the result is an **exclusion**:
  `keep=False`, `ids` = the templates the filter drops (the noise templates). Lines
  without a template are kept, as untagged rows are kept by "hide noise" (ADR002). If
  nothing is dropped, the result is `None`.
- If "actionable only" or a component is set, the result is an **inclusion**: `keep=True`,
  `ids` = the templates that pass. Lines without a template are dropped, as untagged rows
  are (ADR002). An empty `ids` means no line can match.

Matching by text (inside `attach_tags`) keeps the ids right when `serve` has rebuilt
`templates` without re-tagging.

**SQL.** `build_query` gets `tags: TagRestriction | None`. It adds, after the template
restriction, with the ids as `?` parameters:

- `keep=True`: `masked_hash IN (SELECT masked_hash FROM read_parquet('<assignments>') WHERE template_id IN (?, …))`
- `keep=False`: `(masked_hash IS NULL OR masked_hash NOT IN (SELECT … WHERE template_id IN (?, …)))`
- `keep=True` with no ids: `FALSE`.

All restrictions are joined with `AND`, so the tag filters combine with the global
filters, the URL link filters, the template id, the request id and the text search (AC2).

**Two-step page fetch (AC4).** A selective inclusion over the whole range is slow in one
step because DuckDB decodes `payload` and the other columns for every matching line before
the top-N sort. So, when a tag restriction is active, `fetch_page` first fetches only the
`ts` values of the page (`SELECT ts … ORDER BY ts LIMIT size + 1 OFFSET page * size`),
then the full rows inside `[first ts, last ts]` (row groups outside are skipped), and cuts
the page out of that window:

- The window holds every line at the first timestamp, including tied lines that belong to
  the previous page. Their number is (lines in the window at the first timestamp) minus
  (stamps equal to the first one), and they are skipped. The slice is then
  `len(stamps)` rows long. This reproduces the one-step page exactly, up to the order of
  lines with equal timestamps, which is already arbitrary today.
- Fall back to the one-step query when the first and last stamp are equal (the window
  cannot locate the page) or the window reaches `WINDOW_CAP = 5000` rows.

The prototype of this on the real cache returned the same timestamps as the one-step
query on pages 0, 1, 7 and 50 for four restrictions. `search_page` (text search) keeps its
hour-by-hour scan and only gets the tag clause, as its own time budget already bounds it.
Queries without a tag restriction are unchanged.

**Template link (AC2).** A `template` from the URL or the template id box is combined
with the tags by `AND`. When that template is excluded by the tag filters, the result is
empty, and the label says why: `no matching rows (template 12 is excluded by the tag filters)`.

**Alternatives rejected.**

- Always an inclusion list (also for "hide noise"): it would drop lines without a
  template, which ADR002 keeps under "hide noise", and the list would hold almost all
  281 ids.
- Passing the matching `masked_hash` values as a list parameter: 1.5-2.5x slower than the
  subquery in the measurements.
- Tag filters in the global filter store or in the URL: ADR002 keeps them page-local.
- A `tags` dependency for the explorer stage: the page reads the tags at request time;
  the stage computes nothing.
- Two-step fetch for all queries: it slows the single-template case slightly
  (108-124 ms one step vs. 120-220 ms two steps measured) and is not needed by this story.
- Ordering by `ts` plus a tiebreak column: no column combination is unique (duplicate
  lines exist), and the view has no row id.

## 4. Steps

### Commit 1: steps 1-3 (query layer and its tests)

**Step 1: `src/logdash/analyses/explorer.py`, constants and types**

- Imports: `from collections.abc import Sequence`, `from dataclasses import dataclass`,
  `import polars as pl`, and
  `from logdash.app.tag_filters import TagFilter, apply_tag_filter, attach_tags, load_tags`
  (ruff sorts and wraps). Step 4 adds `tag_controls` and step 5 adds `tag_filter` to this
  import, so that each commit passes ruff.
- After `SELECT`:
  ```python
  TS_SELECT = "SELECT ts FROM events"
  WINDOW_SELECT = (
      "SELECT ts AS sort_ts, strftime(ts, '%Y-%m-%d %H:%M:%S.%g') AS time, level, host, "
      "program, logger, python_module, request_id, payload FROM events"
  )
  WINDOW_CAP = 5000  # rows in a two-step window before falling back to one step
  ```
  Step 4 adds `TAGS_DESCRIPTION` and step 5 adds `EXCLUDED` (both below) next to them:
  ```python
  TAGS_DESCRIPTION = (
      " Tag filters keep the lines whose template carries the chosen semantic tags; "
      "hide noise keeps lines without tags."
  )
  EXCLUDED = " (template {tid} is excluded by the tag filters)"
  ```
- New, before `hour_values`:
  ```python
  @dataclass(frozen=True)
  class TagRestriction:
      """Template ids a tag filter keeps (``keep``) or drops (not ``keep``)."""

      ids: tuple[int, ...]
      keep: bool

      def excludes(self, template_id: int) -> bool:
          """Whether lines of this template are filtered out."""
          return template_id not in self.ids if self.keep else template_id in self.ids


  def tag_restriction(store: CacheStore, tag_filter: TagFilter) -> TagRestriction | None:
      """Template ids for the tag filter (ADR002); ``None`` if it is off or there are no tags.

      Only "hide noise" drops the noise templates and keeps lines without a template; with
      "actionable only" or a component, only the passing templates are kept.
      """
      if not tag_filter.active:
          return None
      tags = load_tags(store)
      if tags is None:
          return None
      templates = attach_tags(
          store.read_parquet("templates", "templates.parquet").select("template_id", "template"),
          tags,
      )
      passing = apply_tag_filter(templates, tag_filter)["template_id"].to_list()
      if tag_filter.actionable_only or tag_filter.components:
          return TagRestriction(tuple(sorted(passing)), keep=True)
      dropped = sorted(set(templates["template_id"].to_list()) - set(passing))
      return TagRestriction(tuple(dropped), keep=False) if dropped else None


  def tag_clause(path: str, tags: TagRestriction) -> str:
      """``WHERE`` part for a tag restriction; ``path`` is the quoted assignments file."""
      if not tags.ids:
          return "FALSE" if tags.keep else "TRUE"
      marks = ", ".join("?" for _ in tags.ids)
      lines = f"SELECT masked_hash FROM read_parquet('{path}') WHERE template_id IN ({marks})"
      if tags.keep:
          return f"masked_hash IN ({lines})"
      return f"(masked_hash IS NULL OR masked_hash NOT IN ({lines}))"
  ```

**Step 2: `explorer.py`, `build_query`**

- New keyword parameters, after `limit`: `tags: TagRestriction | None = None`,
  `select: str = SELECT`, `window: tuple[datetime, datetime] | None = None`.
  Docstring addition: "``tags`` restricts to template ids (needs ``assignments``);
  ``select`` replaces the column list; ``window`` keeps ``ts`` between two inclusive bounds."
- Compute `path = assignments.replace("'", "''") if assignments is not None else None`
  once, and use it in the template part (same SQL text as now, condition
  `template is not None and path is not None`).
- Right after the template part:
  ```python
  if tags is not None:
      if path is None:
          raise ValueError("a tag restriction needs the assignments path")
      parts.append(tag_clause(path, tags))
      params.extend(tags.ids)
  ```
- After the text part: `if window is not None: parts.append("ts >= ? AND ts <= ?")`
  and `params.extend(window)`.
- Replace `SELECT` by `select` in both returned SQL strings.

**Step 3: `explorer.py`, fetching**

- New, after `build_query`:
  ```python
  def page_slice(found: Sequence[tuple[Any, ...]], stamps: Sequence[Any]) -> list[tuple[Any, ...]]:
      """The page inside window rows (``sort_ts`` first), without ``sort_ts``.

      Lines at the first timestamp that belong to the previous page are skipped.
      """
      lead = sum(1 for s in stamps if s == stamps[0])
      before = sum(1 for row in found if row[0] == stamps[0]) - lead
      return [row[1:] for row in found[before : before + len(stamps)]]


  def window_rows(
      con: duckdb.DuckDBPyConnection,
      filters: Filters,
      *,
      assignments: str,
      template: int | None,
      request_id: str | None,
      text: str | None,
      tags: TagRestriction,
      page: int,
      size: int,
  ) -> list[tuple[Any, ...]] | None:
      """Rows of one page (up to ``size + 1``) in two steps: sort only ``ts``, then read the
      full rows between the first and last stamp. ``None`` means "use the one-step query"
      (all stamps equal, or ``WINDOW_CAP`` rows in the window)."""
  ```
  Body: build the stamps query with
  `build_query(filters, assignments=assignments, template=template, request_id=request_id, text=text, tags=tags, select=TS_SELECT, page=page, size=size)`;
  `stamps = [row[0] for row in con.execute(sql, params).fetchall()]`. Return `[]` if
  empty, `None` if `stamps[0] == stamps[-1]`. Then the window query with the same
  arguments but `select=WINDOW_SELECT, window=(stamps[0], stamps[-1]), limit=WINDOW_CAP`
  (no `page`/`size`). `found = con.execute(...).fetchall()`. Return `None` if
  `len(found) >= WINDOW_CAP`, else `page_slice(found, stamps)`.
- `fetch_page`: new keyword `tags: TagRestriction | None = None` (after `text`). Compute
  `assignments` once. If `tags is not None`, call `window_rows(...)`. If that gave `None`
  (or `tags is None`), run today's one-step `build_query` (now also with `tags=tags`).
  The record and `more` computation stays as it is.
- `search_page`: new keyword `tags: TagRestriction | None = None` (after `request_id`),
  passed to `build_query`. Nothing else changes.

Commit 1 includes the commit 1 tests from section 5.

### Commit 2: steps 4-5 (page)

**Step 4: `explorer.py`, `layout`**

- First statement after `link = …`: `with_tags = load_tags(store) is not None`.
- In `ui.page(…)`, insert `tag_controls("explorer", with_tags)` right after the inputs
  `dmc.Group` and before `dmc.Text(describe_link(link), …)`.
- Description: append `TAGS_DESCRIPTION if with_tags else ""` to the current string.

**Step 5: `explorer.py`, `update` callback**

- Add three inputs after `Input("explorer-text", "value")`:
  `Input("explorer-hide-noise", "checked")`, `Input("explorer-actionable-only", "checked")`,
  `Input("explorer-components", "value")`, and the parameters `hide_noise: bool | None`,
  `actionable_only: bool | None`, `components: list[str] | None` after `text`.
- After `tid = …`:
  `restriction = tag_restriction(store, tag_filter(hide_noise, actionable_only, components))`.
- Pass `tags=restriction` to both `search_page` and `fetch_page` in `load`.
- Label: after `label = … if rows else "no matching rows"`, add
  ```python
  if not rows and tid is not None and restriction is not None and restriction.excludes(tid):
      label += EXCLUDED.format(tid=tid)
  ```
  before the `cut` suffix.

Commit 2 includes the commit 2 tests from section 5.

### Commit 3: steps 6-7 (benchmark, README)

**Step 6: `tests/benchmark/test_latency.py`**

- Imports: `from logdash.analyses.explorer import TagRestriction, fetch_page, search_page, tag_restriction`,
  `from logdash.app.tag_filters import TagFilter, load_tags`.
- Above a new `test_explorer_tags`:
  ```python
  ACTIONABLE_PROXY = r"(?i)error|fail|exception|refused|timeout"


  def _tag_restrictions(store: CacheStore) -> tuple[str, TagRestriction | None, TagRestriction | None]:
      """(source, hide noise, actionable only): real tags if cached, else stand-ins.

      Stand-ins: template 1 (the largest) as noise, and the templates whose text reads like a
      failure as actionable (53 templates, about 119 000 lines on the real cache).
      """
      if load_tags(store) is not None:
          return (
              "real",
              tag_restriction(store, TagFilter(hide_noise=True)),
              tag_restriction(store, TagFilter(actionable_only=True)),
          )
      t = store.read_parquet("templates", "templates.parquet")
      ids = t.filter(pl.col("template").str.contains(ACTIONABLE_PROXY))["template_id"].to_list()
      return "proxy", TagRestriction((1,), keep=False), TagRestriction(tuple(sorted(ids)), keep=True)
  ```
- `test_explorer_tags(store)`: `con = store.connect()`, `src, hide, act = _tag_restrictions(store)`,
  `hour = _hour_filters(store)`, `host = _cases(store)["host"]`. Cases, each measured as
  `"explorer.fetch_page"` with case name `f"tags ({src}): {name}"` and `CALLBACK_BUDGET_S`:

  | name | call |
  |---|---|
  | hide noise | `fetch_page(con.cursor(), store, from_dict(None), tags=hide)` |
  | hide noise, page 50 | same, `page=50` |
  | hide noise, template 5 | same, `template=5` |
  | actionable | `fetch_page(…, from_dict(None), tags=act)` |
  | actionable, page 5 | same, `page=5` |
  | actionable, 1 hour | `from_dict(hour)` |
  | actionable, host | `from_dict(host)` |
  | actionable, text 'error' | `search_page(con.cursor(), store, from_dict(None), text="error", tags=act)` |

  Also measure `"explorer.tag_restriction"`, case `"all three"`:
  `tag_restriction(store, TagFilter(True, True, ("network", "storage")))`.
  Bind loop variables as default arguments in the lambdas, as the existing tests do.

**Step 7: `README.md:62`**

After the merge, the text reads "…anomaly types and sorts, template tag filters)" (#29).
Change it to "…anomaly types and sorts, template and explorer tag filters)". If #29's
wording is not there, append ", explorer tag filters" before the closing parenthesis.

Then run the quality gate from board.md. If `./cache` exists in the main checkout, also run
`uv run pytest -m slow --no-cov -s tests/benchmark/test_latency.py -k explorer` and paste
the `explorer.*` rows into the PR.

## 5. Tests

**Fixtures in `tests/unit/test_explorer.py`** (commit 1):

```python
NOISE = Tag(False, "network", True, "Harmless duplicate firewall rule.")
FAILURE = Tag(True, "network", False, "A peer closed a connection unexpectedly.")
ROUTINE = Tag(False, "other", False, "Routine message.")
```

- `tagged` (function scope, uses `exp`): remove `store.module_dir("tags")`
  (`shutil.rmtree(…, ignore_errors=True)`); read `templates.parquet`; find the single
  template id whose text contains "Duplicate iptables rule" (`noise`), "closed
  unexpectedly" (`failure`) and "bgp session reset" (`untagged`), asserting one hit each;
  write `tags_frame(templates.select("template_id", "template"), mapping)` to
  `("tags", "tags.parquet")`, where `mapping` gives NOISE / FAILURE / ROUTINE by text and
  leaves out the untagged text; write `meta.json` with the keys #28 writes
  (`available: True`, `model: "stub"`, `prompt_version: "1"`, `templates`, `tagged`,
  `untagged: 1`, `calls: 0`, `aborted: None`, `failures: []`). Yield
  `SimpleNamespace(store=store, noise=…, failure=…, untagged=…)`. Afterwards remove
  `module_dir("tags")` again.
- `no_tags` (function scope): remove `module_dir("tags")` and return the store.
- `count(con, store, template_ids)`: `SELECT count(*) FROM events WHERE masked_hash IN
  (SELECT masked_hash FROM read_parquet(?) WHERE template_id IN (…))` with the
  assignments path, for expected totals.
- `walk(node)`: copy of the helper in `test_temporal.py:378-383`.

| Commit | Test | Proves | AC |
|---|---|---|---|
| 1 | `test_build_query_tag_clauses` | `keep=True, ids=(4, 9)`: SQL has `template_id IN (?, ?)` inside `masked_hash IN (`, and 4, 9 follow the template id in `params`; `keep=False`: SQL has `masked_hash IS NULL OR masked_hash NOT IN (`; `keep=True, ids=()`: SQL has `FALSE`, params unchanged; `keep=False, ids=()`: `TRUE`; `tags=…` without `assignments` raises `ValueError`; `select=TS_SELECT, window=(a, b), limit=7` gives `SELECT ts FROM events`, `ts >= ? AND ts <= ?` with `a, b` as the last params, and `LIMIT 7`; `build_query(Filters())` is unchanged (existing assertions) | AC1, AC2 |
| 1 | `test_page_slice_and_restriction_helpers` | `page_slice([(1,"a"),(1,"b"),(1,"c"),(2,"d"),(3,"e"),(3,"f")], [1, 2, 3]) == [("c",), ("d",), ("e",)]`; with stamps `[1, 1, 2]` it gives `[("b",), ("c",), ("d",)]`; `TagRestriction((2,), True).excludes(3)` and not `.excludes(2)`; `TagRestriction((2,), False).excludes(2)` and not `.excludes(3)` | AC1, AC2 |
| 1 | `test_window_rows_falls_back` (a fake connection whose `execute(sql, params).fetchall()` returns queued lists) | no stamps gives `[]`; stamps `[t, t]` gives `None` and runs one query; stamps `[t1, t2]` with a window of `WINDOW_CAP` rows (monkeypatched to 3) gives `None` | AC4 |
| 1 | `test_tag_restriction` (`tagged`, then `no_tags`) | `TagFilter()` gives `None`; hide noise gives `TagRestriction((noise,), False)`; actionable only gives `((failure,), True)`; components `("network",)` gives `(tuple(sorted({noise, failure})), True)`; hide noise with `("network",)` gives `((failure,), True)`; `("auth",)` gives `((), True)`; with `no_tags`, `TagFilter(True, True, ("network",))` gives `None` | AC1, AC3 |
| 1 | `test_fetch_page_with_tags` (`tagged`) | hide noise with `size=100_000` (falls back to one step): no payload contains "Duplicate iptables rule", and the row count is `SELECT count(*) FROM events` minus `count(noise)`; actionable only (`size=100_000`): every payload contains "closed unexpectedly" and the count equals `count(failure)`; components `("auth",)`: `([], False)`; actionable with `Filters(hosts=(<host of the first failure row>,))`: non-empty and every row has that host; template `failure` with hide noise: same rows as template `failure` alone; template `noise` with hide noise: `[]` | AC1, AC2 |
| 1 | `test_two_step_pages_match_one_step` (`tagged`) | for hide noise and for actionable only, pages 0-3 with `size=50`: the `time` lists are equal with the default `WINDOW_CAP` and with `WINDOW_CAP` monkeypatched to 0 (forces one step), and `more` is equal | AC4 |
| 1 | `test_search_page_with_tags` (`tagged`) | `search_page(text="iptables", tags=<hide noise>)` returns `[]`; `search_page(text="closed", tags=<actionable>)` returns the same `time` list as `fetch_page(text="closed", tags=<actionable>)` | AC2 |
| 2 | `test_layout_tag_controls` (`no_tags`, then `tagged`) | without tags: the component with id `explorer-tag-filters` has `style == {"display": "none"}`, and the page text has no "Tag filters"; with tags: no `display: none`, ids `explorer-hide-noise`, `explorer-actionable-only`, `explorer-components` are present, and the description contains "Tag filters" | AC3 |
| 2 | update `test_layout_prefilled_and_callback` (runs with `no_tags`) | `inputs()` gets `"explorer-hide-noise": ("checked", False)`, `"explorer-actionable-only": ("checked", False)`, `"explorer-components": ("value", [])`, inserted after `"explorer-text"` and before `"explorer-prev"` (the request lists inputs in callback order). All existing assertions still pass. New: with tags absent, `explorer-hide-noise` `True` and components `["auth"]` (changed `explorer-hide-noise.checked`) return the same `rowData` as the first call | AC3 |
| 2 | `test_callback_with_tags_over_http` (`tagged`; same `call`/`inputs` helpers, moved to module level as `post(client, inputs, state, changed)` and `explorer_inputs(**over)`) | hide noise on: no "Duplicate iptables rule" payload and `explorer-page.data == 0`; actionable on: all rows "closed unexpectedly"; template box `str(noise)` plus hide noise: `rowData == []` and label `f"no matching rows (template {noise} is excluded by the tag filters)"`; template box `str(failure)` (as a `?template=` link fills it) plus actionable: non-empty, all "closed unexpectedly"; hide noise plus global `filters` `{"levels": ["ERROR"]}`: every row has level ERROR and no iptables payload | AC1, AC2 |
| 3 | `tests/benchmark/test_latency.py::test_explorer_tags` (slow, real cache) | the 8 `fetch_page`/`search_page` tag cases and `tag_restriction` stay under 300 ms warm | AC4 |

AC4 coverage: the quality gate enforces 90 %. Every new branch in `explorer.py` is hit by
the rows above (`tag_clause` four ways, `window_rows` four exits, `excludes` both ways,
`tag_restriction` all returns, the label suffix).

**Manual check (not required for review).** With a cache built with a key, run
`uv run logdash serve --no-build`, open `/explorer`, turn on "Hide noise": the iptables
lines disappear. Open `/explorer?template=1` and turn on "Hide noise": the label explains
that template 1 is excluded.

## 6. Open questions

None blocks the plan. Each has a default.

1. **Tag columns or a summary tooltip in the explorer grid.** *Default: no.* The story
   asks for filters only; the grid shows raw lines. Adding them later needs a per-row
   template id, which the query does not return today.
2. **Tag filters in links from other pages** (for example, `/explorer?hide_noise=1`).
   *Default: no; the filters are page-local and reset on load (ADR002).*
3. **Lines without a template** (null payload, 1 055 lines on the real cache). *Default:
   treated as untagged: kept by "hide noise", dropped by "actionable only" and by a
   component choice (ADR002 semantics).*
4. **Two-step fetch only with a tag filter.** *Default: yes.* Other queries keep the
   one-step plan; extending it is a separate change with its own benchmark.
5. **Order of lines with equal timestamps at a page boundary.** *Default: arbitrary, as
   today;* the two-step fetch keeps page sizes and boundaries exact.
