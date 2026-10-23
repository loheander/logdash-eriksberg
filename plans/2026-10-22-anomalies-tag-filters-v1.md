# Plan: semantic tag filters on the Anomalies page (story #31), v1

## 1. Story

Story [#31](https://github.com/loheander/logdash-eriksberg/issues/31), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24). Depends on #28 (tagging
stage, PR [#34](https://github.com/loheander/logdash-eriksberg/pull/34)) and #29 (shared
tag filter module and ADR002, PR [#35](https://github.com/loheander/logdash-eriksberg/pull/35)).
Feature branch `feature/semantic-template-tags`, story branch
`feature/semantic-template-tags-31-anomalies-tag-filters`.

**Goal.** The Anomalies page can hide noise, show only actionable anomalies and restrict to
a component, based on the semantic tags of each anomaly's template.

**Acceptance criteria** (copied from the story):

- [ ] AC1: Filters "hide noise", "actionable only" and component apply to rows with a `template_id`.
- [ ] AC2: Rows without a `template_id` are kept, and the hint says the tag filters do not apply to them (like the existing host and service hint).
- [ ] AC3: Without tags in the cache, the filters are hidden and the page works as today.
- [ ] AC4: Callbacks stay under 300 ms (benchmark extended). Coverage stays at or above 90 %.

Out of scope: the Templates and Explorer pages (#29, #30); detector scores (#32).

**Precondition.** #28 and #29 must be merged into `feature/semantic-template-tags`
before step 1. Start by merging `origin/feature/semantic-template-tags` into the story
branch (a merge commit, no other changes). Then check that both of these exist:

- `src/logdash/analyses/tags.py` with `tags_frame` and `read_tags` (#28);
- `src/logdash/app/tag_filters.py` with `TagFilter`, `tag_filter`, `load_tags`,
  `attach_tags`, `apply_tag_filter` and `tag_controls` (#29, step 1 of
  `plans/2026-10-22-templates-page-tags-v1.md`).

If either is missing, the dependencies have not landed: stop, change nothing, leave the
story in Ready and report that to the orchestrator (this is sequencing, not a plan
defect). If they exist with other names or signatures than listed in section 2, follow
the implementer rule for a wrong plan (`needs-replan`).

**Do not change `tag_filters.py`** (ADR002, decision 1). Everything page-specific goes in
`analyses/anomalies.py`.

## 2. Context

Code on the feature branch today:

- `src/logdash/analyses/anomalies.py`
  - `:35` imports `short` from `analyses.templates`.
  - `:151-155` `load_anomalies`: the combined table, sorted by descending score, kept in
    the `_FRAMES` LRU until the file changes.
  - `:174-198` `filter_values` and `select`: time range, types, and the service and host
    filters, each restricting only its own kind of row (`KINDS[...].filter_field`).
  - `:303-322` `draw_anomalies(store, filters, types, sort, scheme)`: timeline, table
    (top `TABLE_ROWS`) and hint. It returns early when the stage is not built (`:307-309`).
  - `:344-359` `hint(filters, count, sort, meta)`. The sentence AC2 refers to is built at
    `:350-356`: "The services filter applies to service rows only; … other kinds stay in
    the view."
  - `:410-453` `layout`: a `dmc.Group` with the type `MultiSelect` and the sort control,
    then the timeline card, the table card, the hint text and the description (`:444-452`).
  - `:455-473` `register_callbacks`: one callback `draw` with four `Input`s
    (`anomalies-types.value`, `anomalies-sort.value`, the filter store, the theme store).
- `src/logdash/detectors/base.py:29-56`: `template_id` is an optional, nullable `Int64`
  column of `anomalies.parquet` ("null when the row is not about one template").
  Detectors that set it: `novel_template`, `rare_template` (`detectors/novel.py`) and
  `sequence` (`detectors/sequence.py`, the destination template).
- The `anomalies` stage depends on `templates` through its detectors
  (`Anomalies.depends_on`, `:378-381`), so every templates rebuild also rebuilds the
  anomalies in the same run, and the anomaly `template_id`s are always the ids in the
  current `templates.parquet`. The `tags` stage, on the other hand, can be older (serve
  never rebuilds it, ADR001), which is why ADR002 matches tags to templates by text.
- `src/logdash/app/ui.py:14-19` `page(title, *children, description)`: a `dmc.Stack` of
  the children.
- From #28 (PR #34, implemented): `logdash.analyses.tags.tags_frame(templates, tags:
  Mapping[str, Tag]) -> pl.DataFrame` (columns `template_id, template, tagged, actionable,
  component, noise, summary`), `read_tags(store) -> pl.DataFrame | None`, and
  `logdash.tagging.model.Tag(actionable, component, noise, summary)` and `COMPONENTS`.
- From #29 (ADR002 and plan v1, step 1): `TagFilter(hide_noise, actionable_only,
  components)` with `.active`; `tag_filter(hide_noise, actionable_only, components)` from
  control values; `load_tags(store) -> pl.DataFrame | None` (also `None` when no row is
  tagged); `attach_tags(templates, tags)` (left join on the text, keeps rows and order);
  `apply_tag_filter(df, f)` (hide noise keeps untagged rows; actionable only and a
  component choice drop them); `tag_controls(prefix, visible)` with the ids
  `<prefix>-hide-noise` (`checked`), `<prefix>-actionable-only` (`checked`),
  `<prefix>-components` (`value`) and the group id `<prefix>-tag-filters`, rendered with
  `style={"display": "none"}` when not visible.
- Tests:
  - `tests/unit/test_anomalies.py:41-44`: the module fixture `stage` (the session cache
    with templates, features, pca, temporal, hosts and anomalies built).
  - `:942-951` `test_hints_name_the_kept_counts_and_the_filters_that_do_not_apply`.
  - `:990-998` `test_draw_anomalies`. `:1001-1004` `test_degrades_without_the_stage`.
  - `:1019-1053` `test_layout_and_callback_over_http`: posts the four inputs of `draw` by
    position.
  - On the synthetic cache, `anomalies.parquet` has 33 rows. Three have a `template_id`,
    one row each for ids 10 (`sequence`, "Received request for instance <UUID>"), 14
    (`novel_template`, "Unexpected bgp session reset …") and 15 (`rare_template`,
    "Watchdog rebooted chassis blade …"); 30 rows have none. (Checked on 2026-10-22 with a
    throwaway test; the plan's fixture does not hard-code these ids.)
- `tests/benchmark/test_latency.py:300-317` `ANOMALY_CASES` and `test_anomalies`, which
  calls `fns["draw"](types, sort, filters, "light")` positionally. `:335-345`
  `test_anomalies_cold` does the same with four arguments.

**Real cache** (`/Users/emny/src/logdash-eriksberg/cache`, no `tags` stage yet):
`anomalies.parquet` has 1603 rows. Only 221 have a `template_id` (126 distinct ids):
`rare_template` 166, `sequence` 40, `novel_template` 15. The other detectors (contextual,
structural, volume, severity: 1382 rows) set none. So the tag filters change at most about
14 % of the rows today, and the hint matters (AC2).

**Measurement** (2026-10-22, 17:55, load average 3.6 / 5.9 / 6.1 on the 4 shared CPUs,
other agents running, `POLARS_MAX_THREADS=2`, stand-in tags for all 281 templates): the
whole tag step of section 3 (read `templates.parquet`, `attach_tags`, distinct anomaly
ids, `apply_tag_filter`, row filter) took **2.8-3.4 ms warm** for hide noise, actionable
only, one component and all three combined. The full `draw` callback is timed by the
benchmark (step 5).

## 3. Design

No new ADR: this story applies ADR002 and decides nothing that binds other stories.

**Controls.** `tag_controls("anomalies", with_tags)` from the shared module, as its own
row directly below the existing type and sort controls. `with_tags = load_tags(store) is
not None`, checked when the page is rendered (layouts are built per request). Without tags
the row is rendered hidden, so the callback keeps all its inputs (ADR002, decision 5), and
the controls stay in their "off" state, so the callback output is as today (AC3).

**From tags to anomaly rows.** Two new functions in `anomalies.py`:

- `template_tags(store) -> pl.DataFrame | None`: `None` when `load_tags` is `None`.
  Otherwise `attach_tags(templates.parquet[template_id, template], tags)`: the tags of the
  **current** template ids, matched on the text as ADR002 requires. Anomaly ids are
  current ids (section 2), so joining anomalies to this frame on `template_id` is exact.
- `restrict_by_tags(rows, by_id, wanted) -> pl.DataFrame`, a pure function:
  1. Take the distinct non-null `template_id`s of `rows`.
  2. Left-join them to `by_id` on `template_id`. An id missing from `by_id` gets null tags,
     so it counts as untagged.
  3. `apply_tag_filter` on that small frame gives the ids that pass (ADR002 semantics: hide
     noise keeps untagged ids; actionable only and a component choice drop them).
  4. Keep the rows whose `template_id` is null (AC2) or among the passing ids (AC1).
     `filter` keeps the row order, so the rows stay sorted by descending score, as
     `draw_anomalies` expects.

  Deciding per distinct id (at most a few hundred) instead of joining tag columns onto
  every row keeps the anomaly frame's columns unchanged and is cheaper.

**Where it runs.** In `draw_anomalies`, right after `select(...)`, so that the timeline,
the table, the count and the hint all describe the same rows. The tags are read only when
the filter is active, so the no-filter path does not change and does not read any more
files (AC3, AC4).

**Hint (AC2).** When a tag filter is active and tags exist, `hint` adds, after the
service and host sentence and before the level sentence:
`" The tag filters apply to rows about one template only; {n:,} rows without a template stay in the view."`
`n` is the number of rows without a `template_id` among the rows shown (after all filters).
It is computed with `null_count()`.

**Description.** Without tags it is exactly today's text. With tags, one sentence is
appended (step 3).

**Callback signature.** The three new `Input`s are appended **after** the theme store, and
the new parameters have defaults of `None`. Existing positional callers (the benchmark)
keep working unchanged.

**Not shown:** tag columns or the summary tooltip in the anomalies table (open question 1).

**Alternatives rejected.**

- Joining the tag columns onto every anomaly row and calling `apply_tag_filter` on it: it
  drops rows without a template under "actionable only" (that would break AC2), and it
  needs a row-index trick to keep them.
- Matching tags to anomalies through `tags.parquet`'s own `template_id`: wrong after a
  templates rebuild under `serve` that renumbered ids (ADR002, decision 4).
- Dropping rows without a template under "actionable only": the story says they are kept.
- A build-time join of tags into `anomalies.parquet`: it would make `anomalies` depend on
  `tags`, a model stage that serve never rebuilds (ADR001), and the page would need a
  rebuild after every tagging run.
- Caching `template_tags` in the LRU: the step costs about 3 ms; #29 reads the same files
  per callback without caching too.
- Putting the tag controls inside the existing `dmc.Group`: `tag_controls` returns its own
  group with the id `anomalies-tag-filters`, which is what ADR002 hides; nesting it would
  give an awkward layout when visible.

## 4. Steps

### Commit 1: steps 1-4 (page, callback and unit tests)

Before step 1: the precondition merge (section 1) is its own merge commit.

**Step 1: `src/logdash/analyses/anomalies.py`, imports and constants**

- Change the import at `:35` to
  `from logdash.analyses.templates import KEY as TEMPLATES_KEY, short` (let ruff split
  or sort it).
- Add
  `from logdash.app.tag_filters import TagFilter, apply_tag_filter, attach_tags, load_tags, tag_controls, tag_filter`
  (let ruff sort and wrap it).
- Add these constants after `SORT_COLUMNS`:
  ```python
  DESCRIPTION = (
      "Every registered detector writes its candidates at build time. The score in "
      "[0, 1] blends a row's rank among the detector's rows of the same kind with its "
      "log-scaled excess over the detector's threshold; the default order, overall, "
      "is the reciprocal rank (1 / (60 + rank by score within detector and kind)), "
      "which compares detectors by position, not by how many rows each has. The time "
      "range, service and host filters apply (each to its own kind of row); each row "
      "links to the raw lines of its window in the explorer."
  )
  TAGS_DESCRIPTION = (
      " Hide noise, actionable only and component use the semantic tags of a row's "
      "template (from a language model at build time); rows without a template are not "
      "filtered by them."
  )
  ```
  `DESCRIPTION` is the current text from `:445-451`, unchanged, character for character.

**Step 2: `anomalies.py`, helpers and `draw_anomalies`**

Add, in the "pure helpers" section after `select`:

```python
def template_tags(store: CacheStore) -> pl.DataFrame | None:
    """Tags of the current templates by ``template_id`` (matched on the text, ADR002).

    ``None`` when no tags are cached; the tag filters are then a no-op.
    """
    tags = load_tags(store)
    if tags is None:
        return None
    templates = store.read_parquet(TEMPLATES_KEY, "templates.parquet")
    return attach_tags(templates.select("template_id", "template"), tags)


def restrict_by_tags(rows: pl.DataFrame, by_id: pl.DataFrame, wanted: TagFilter) -> pl.DataFrame:
    """Rows that pass the tag filter by their template's tags, in their order.

    Rows without a ``template_id`` are kept: the tags describe templates. An id missing from
    ``by_id`` counts as untagged (ADR002: hide noise keeps it, the other filters drop it).
    """
    if not wanted.active:
        return rows
    ids = rows.select(pl.col("template_id").drop_nulls().unique())
    passing = apply_tag_filter(ids.join(by_id, on="template_id", how="left"), wanted)
    return rows.filter(
        pl.col("template_id").is_null()
        | pl.col("template_id").is_in(passing["template_id"].to_list())
    )
```

(`is_in` with an empty list returns false for every row, so only the rows without a
template remain; checked with polars 2.0.0.)

Change `draw_anomalies`:

```python
def draw_anomalies(
    store: CacheStore,
    filters: Filters,
    types: list[str],
    sort: str,
    scheme: str | None,
    by_tags: TagFilter | None = None,
) -> tuple[go.Figure, Component, str]:
    """Timeline, ranked table with explorer links, and a hint on what was selected.

    ``by_tags`` restricts the rows about one template by its semantic tags (no-op without
    cached tags); rows without a template are kept.
    """
    problem = unavailable(store)
    if problem is not None:
        return figures.empty_figure(problem, scheme), dmc.Text(problem), ""
    rows = select(load_anomalies(store), filters, types)  # sorted by descending score
    untemplated: int | None = None
    if by_tags is not None and by_tags.active:
        by_id = template_tags(store)
        if by_id is not None:
            rows = restrict_by_tags(rows, by_id, by_tags)
            untemplated = rows["template_id"].null_count()
    ...
```

The rest of the body stays as it is, except that both `hint(...)` calls get the keyword
`untemplated=untemplated`.

**Step 3: `anomalies.py`, `hint` and `layout`**

`hint` gets a keyword parameter `untemplated: int | None = None` (last). Insert this
directly after the `if applied:` block and before `if filters.levels:`:

```python
    if untemplated is not None:
        text += (
            " The tag filters apply to rows about one template only; "
            f"{untemplated:,} rows without a template stay in the view."
        )
```

Extend the docstring (add one if there is none): "`untemplated`: rows without a template
in the view while a tag filter is active (None: no tag filter)."

`layout`:

- Add `with_tags = load_tags(store) is not None` as the second line, after
  `types = load_types(store)`.
- In `ui.page(...)`, insert `tag_controls("anomalies", with_tags)` directly after
  `controls`, before the timeline card.
- `description=DESCRIPTION + (TAGS_DESCRIPTION if with_tags else "")`.

**Step 4: `anomalies.py`, `register_callbacks`**

Append three inputs after `Input(THEME_STORE_ID, "data")`:

```python
            Input("anomalies-hide-noise", "checked"),
            Input("anomalies-actionable-only", "checked"),
            Input("anomalies-components", "value"),
```

and change `draw` to

```python
        def draw(
            types: list[str] | None,
            sort: str | None,
            filters: dict[str, Any] | None,
            theme: str | None,
            hide_noise: bool | None = None,
            actionable_only: bool | None = None,
            components: list[str] | None = None,
        ) -> tuple[go.Figure, Component, str]:
            return draw_anomalies(
                store,
                from_dict(filters),
                type_values(types),
                sort_value(sort),
                theme,
                tag_filter(hide_noise, actionable_only, components),
            )
```

Keep the function name `draw`, because the benchmark looks it up by name. No
`prevent_initial_call`: the callback already draws the page on load.

Update the module docstring's last paragraph by adding one sentence: "Semantic tag filters
(ADR002) restrict the rows that have a template; the others stay."

Commit 1 also contains the `test_anomalies.py` changes from section 5.

### Commit 2: step 5-6 (benchmark, README)

**Step 5: `tests/benchmark/test_latency.py`**

Add the imports `import re`, `from logdash.analyses.tags import tags_frame`,
`from logdash.app.tag_filters import load_tags` and
`from logdash.tagging.model import COMPONENTS, Tag` (let ruff sort them). If #30 has
already added some of them, do not duplicate them.

Add directly after `test_anomalies_cold`:

```python
ANOMALY_TAG_CASES = (
    ("hide noise", True, False, []),
    ("actionable only", False, True, []),
    ("component network", False, False, ["network"]),
    ("all three", True, True, ["network", "storage"]),
)


def _anomaly_tags(store: CacheStore) -> tuple[str, pl.DataFrame]:
    """("real", cached tags) or ("stand-in", tags for every template).

    Stand-ins: template 1 (the largest) is noise, texts that read like a failure are
    actionable, and the components cycle by id, so every case keeps and drops some rows.
    """
    real = load_tags(store)
    if real is not None:
        return "real", real
    templates = store.read_parquet("templates", "templates.parquet")
    failure = re.compile(r"(?i)error|fail|exception|refused|timeout")
    stand_in = {
        text: Tag(bool(failure.search(text)), COMPONENTS[tid % len(COMPONENTS)], tid == 1, "-")
        for tid, text in templates.select("template_id", "template").iter_rows()
    }
    return "stand-in", tags_frame(templates, stand_in)


def test_anomalies_tag_filters(store: CacheStore, monkeypatch: pytest.MonkeyPatch) -> None:
    source, tags = _anomaly_tags(store)
    monkeypatch.setattr(anomalies, "load_tags", lambda _store: tags)
    fns = _recorded(store, "anomalies")
    for name, filters in _cases(store).items():
        for case, hide, actionable, components in ANOMALY_TAG_CASES:
            measure(
                "anomalies.draw",
                f"tags ({source}) {case}, {name}",
                CALLBACK_BUDGET_S,
                partial(fns["draw"], [], "overall", filters, "light", hide, actionable, components),
            )
```

The `monkeypatch` replaces the name `load_tags` inside `anomalies`, which is what
`template_tags` calls; with real tags it returns the same frame on every call, which
leaves out a file read of about 1 ms. Leave `test_anomalies` and `test_anomalies_cold`
unchanged: they measure the no-tag-filter path, which must stay as fast as today.

**Step 6: `README.md:62-63`**

If the benchmark sentence does not mention tag filters yet, change "anomaly types and
sorts)" to "anomaly types and sorts, template tag filters)". If it already does (from #29
or #30), leave it.

Then run the quality gate from `board.md`. Then, because `./cache` exists in the main
checkout, run from the main checkout's directory
`LOGDASH_THREADS=2 uv run pytest -m slow --no-cov -s tests/benchmark/test_latency.py -k "anomalies or layouts"`
once, and paste the `anomalies.*` rows into the PR comment together with the load
average (`uptime`) at the time. The machine is shared: do not repeat the run to get better
numbers.

## 5. Tests

**Fixtures in `tests/unit/test_anomalies.py`** (commit 1). Add the imports
`import shutil`, `from types import SimpleNamespace`,
`from logdash.analyses.tags import tags_frame`,
`from logdash.app.tag_filters import TagFilter` and
`from logdash.tagging.model import Tag`.

```python
NOISE = Tag(False, "network", True, "Harmless chatter.")
FAILURE = Tag(True, "hypervisor", False, "A blade was rebooted after a thermal trip.")
ROUTINE = Tag(False, "other", False, "Routine message.")
```

- `no_tags(stage)` (function scope): `shutil.rmtree(stage.module_dir("tags"),
  ignore_errors=True)`, then returns `stage`.
- `tagged(stage)` (function scope):
  - Remove `stage.module_dir("tags")`.
  - `ids = sorted(an.load_anomalies(stage)["template_id"].drop_nulls().unique().to_list())`
    and `assert len(ids) >= 3`. Let `noise, failure, untagged = ids[0], ids[1], ids[2]`.
  - `templates = stage.read_parquet("templates", "templates.parquet")`; `text = dict(
    templates.select("template_id", "template").iter_rows())`.
  - `mapping = {t: NOISE if i == noise else FAILURE if i == failure else ROUTINE for i, t
    in text.items() if i != untagged}`.
  - `stage.write_parquet("tags", "tags.parquet", tags_frame(templates, mapping))`.
  - Yield `SimpleNamespace(store=stage, noise=noise, failure=failure, untagged=untagged)`.
  - Afterwards remove `stage.module_dir("tags")` again, so other modules sharing the
    session cache do not see tags.

`count(rows, tid)` in the tests below means `rows.filter(pl.col("template_id") ==
tid).height`.

| Commit | File: test | Proves | AC |
|---|---|---|---|
| 1 | `test_restrict_by_tags_semantics` (no cache; `rows` with `template_id` `[1, None, 2, 3, 4, 99, 1]` and a `score` column `[7, 6, 5, 4, 3, 2, 1]`; `by_id` built with `tags_frame` from templates 1-4 with texts "a"-"d": 1 noise/network, 2 actionable/network, 3 storage, 4 untagged) | inactive filter returns `rows` unchanged; hide noise gives ids `[None, 2, 3, 4, 99]`; actionable only gives `[None, 2]`; components `("storage",)` gives `[None, 3]`; hide noise and `("network",)` gives `[None, 2]`; actionable only and `("storage",)` gives `[None]`; in each case the `score` column is still descending (order kept) | AC1, AC2 |
| 1 | `test_template_tags_matches_current_ids_by_text` (`tagged`) | `template_tags(store)` has one row per template in `templates.parquet`; the row of `tagged.noise` has `noise is True`; the row of `tagged.untagged` has `tagged is False` and null `actionable` | AC1 |
| 1 | `test_tag_filters_restrict_rows_with_a_template` (`tagged`) | with `all_rows = an.select(an.load_anomalies(store), Filters(), [])` and, for each filter `f`, `rows = an.restrict_by_tags(all_rows, an.template_tags(store), f)`: hide noise drops every `tagged.noise` row and keeps the others (height = `all_rows.height - count(all_rows, noise)`); actionable only keeps exactly the `tagged.failure` rows among the rows with a template; `TagFilter(components=("hypervisor",))` gives the same rows as actionable only; `TagFilter(components=("storage",))` keeps only rows without a template; in every case `rows["template_id"].null_count() == all_rows["template_id"].null_count()` | AC1, AC2 |
| 1 | `test_draw_with_tag_filter_says_untemplated_rows_stay` (`tagged`) | `draw_anomalies(store, Filters(), [], "score", None, TagFilter(actionable_only=True))[2]` contains `"tag filters apply to rows about one template only"` and `f"{n:,} rows without a template stay in the view"` where `n` is the null count of `all_rows`; its count sentence starts with `f"{n + count(all_rows, failure):,} anomalies in the selection"` (the noise, untagged and routine-tagged rows are dropped); `draw_anomalies(..., TagFilter())` (inactive) gives the same hint as `draw_anomalies(...)` without the argument and no "tag filters" | AC1, AC2 |
| 1 | `test_tag_filters_are_a_no_op_without_tags` (`no_tags`) | `draw_anomalies(store, Filters(), [], "score", None, TagFilter(True, True, ("storage",)))` returns the same table (`str(...)`) and the same hint as without the argument; the hint has no "tag filters"; `template_tags(store) is None` | AC3 |
| 1 | `test_layout_hides_tag_controls_without_tags` (`no_tags`) | in `Anomalies().layout(store)`, the component with id `anomalies-tag-filters` (found with a small recursive `walk` over `children`, as in `test_temporal.py:378`) has `style == {"display": "none"}`; the ids `anomalies-hide-noise`, `anomalies-actionable-only` and `anomalies-components` are present; the description (the `dmc.Text` that is the second child of the page `Stack`, see `ui.page`) has `children == an.DESCRIPTION` | AC3 |
| 1 | `test_layout_shows_tag_controls_with_tags` (`tagged`) | the `anomalies-tag-filters` group has no `display: none` style; the description's `children == an.DESCRIPTION + an.TAGS_DESCRIPTION` | AC1 |
| 1 | extend `test_hints_name_the_kept_counts_and_the_filters_that_do_not_apply` | `an.hint(Filters(), 5, untemplated=1234)` contains `"1,234 rows without a template stay in the view"`; `an.hint(Filters(), 5)` does not contain `"tag filters"` | AC2 |
| 1 | change `test_layout_and_callback_over_http` (run it with the `no_tags` fixture added to its arguments) | append the three inputs to `body["inputs"]`: `{"id": "anomalies-hide-noise", "property": "checked", "value": False}`, `{"id": "anomalies-actionable-only", "property": "checked", "value": False}`, `{"id": "anomalies-components", "property": "value", "value": []}`; the existing assertions still pass; then post once more with the actionable switch `True` and the types `[]`: status 200, and since there are no tags the hint has no "tag filters" | AC3 |
| 1 | new `test_tag_filter_callback_over_http` (`tagged`, `built`) | the same request as above with types `[]`, sort `"score"`, the actionable switch `True`: status 200 and `response["anomalies-hint"]["children"]` contains `"rows without a template stay in the view"` | AC1, AC2 |
| 1 | existing `test_draw_anomalies`, `test_degrades_without_the_stage`, `test_value_helpers` | unchanged and still pass: the default `by_tags=None` is today's behaviour | AC3 |
| 2 | `tests/benchmark/test_latency.py::test_anomalies_tag_filters` (slow, real cache) | `draw` with 4 tag cases x 4 filter cases stays under 300 ms warm | AC4 |
| 2 | existing `test_anomalies`, `test_anomalies_cold`, `test_layouts` (slow) | the no-filter path and the layout (which now calls `load_tags`) stay within budget | AC3, AC4 |

For the HTTP tests, factor the request body of `test_layout_and_callback_over_http` into a
local helper `draw_body(types, sort, actionable)` in the test module, so the two tests
share it.

AC4 coverage: the quality gate enforces 90 %. Every new branch in `anomalies.py` is
covered by the rows above (inactive filter, no tags, tags with each filter).

**Manual check (not required for review).** With a cache built with a key, run
`uv run logdash serve --no-build` and open `/anomalies`. The tag row shows below the type
and sort controls. Turn on "Actionable only": the novel, rare and sequence rows whose
template is not actionable disappear from the table and the timeline; the volume,
structural, contextual and severity rows stay; the hint names how many rows without a
template stay.

## 6. Open questions

None blocks the plan. Each has a default.

1. **Tag columns or the summary tooltip in the anomalies table.** *Default: no.* The story
   asks for filters only; the table is a `ui.link_table`, not an AG Grid, and most rows
   have no template. A later story can add a "component" column.
2. **Rows without a template under "actionable only".** *Default: kept* (the story says
   so), even though a severity or volume row may well be actionable. On the real cache
   that is 86 % of the rows, so "actionable only" narrows the table less than its name
   suggests; the hint states the count so this is visible.
3. **Filter state across pages.** *Default: page-local, reset on every page load*
   (ADR002, decision 2). It is not carried in links from other pages.
4. **The hint count.** *Default: rows without a template among the rows shown*, after the
   time, type, service, host and tag filters, not of the whole table.
