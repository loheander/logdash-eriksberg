# Plan: semantic tags on the Templates page (story #29), v1

## 1. Story

Story [#29](https://github.com/loheander/logdash-eriksberg/issues/29), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24). Depends on #28
(tagging stage, PR [#34](https://github.com/loheander/logdash-eriksberg/pull/34)).
Feature branch `feature/semantic-template-tags`, story branch
`feature/semantic-template-tags-29-templates-page-tags`.

**Goal.** The Templates page shows each template's semantic tags, with its plain-language
summary as a tooltip, and offers the filters "hide noise", "actionable only" and
"by component".

**Acceptance criteria** (copied from the story):

- [ ] AC1: With tags in the cache, the templates grid shows actionable, component and noise, and the summary as a tooltip on the template text.
- [ ] AC2: Filters "hide noise", "actionable only" and component restrict the grid and the selected-template views consistently.
- [ ] AC3: Without tags in the cache, the filters and tag columns are hidden and the page works as today.
- [ ] AC4: Callbacks stay under 300 ms (benchmark in `tests/benchmark/test_latency.py` extended). Coverage stays at or above 90 %.

Out of scope: Explorer and Anomalies filters (#30, #31). They reuse the shared module
from step 1 (ADR002).

**Precondition.** #28 must be merged into `feature/semantic-template-tags` first. Before
step 1, merge `origin/feature/semantic-template-tags` into the story branch. This plan
uses #28's interface as planned in `plans/2026-10-22-tagging-stage-v2.md` (step 8):

- `logdash.analyses.tags.read_tags(store) -> pl.DataFrame | None`, with the columns of
  `TAGS_SCHEMA`: `template_id, template, tagged, actionable, component, noise, summary`.
  It returns `None` when tags are absent.
- `logdash.analyses.tags.tags_frame(templates, tags: Mapping[str, Tag]) -> pl.DataFrame`.
- `logdash.tagging.model.COMPONENTS` (7 strings) and `Tag(actionable, component, noise,
  summary)`.

If the merged code differs only in names, adapt the imports. If `read_tags` behaves
differently (for example, it never returns `None`), stop and send the story back with
`needs-replan`.

## 2. Context

- `src/logdash/analyses/templates.py`
  - `:58` `GRID_ROWS = 200`. `:243-244` `short`.
  - `:280-317` `param_figure`. With `template_id=None` it currently says "this template has
    no extracted parameters".
  - `:320-332` `grid_rows`: top `limit` templates by count, as dicts.
  - `:349-350` `selected_id`. `:353-367` `volume_for`. With `template_id=None` it filters
    on `template_id == None` and draws "no events for this template in the range".
  - `:382-446` `layout`: the AG Grid `templates-grid` (`:388-413`) with static `rowData`
    and `selectedRows=rows[:1]`, the volume and parameter cards, the rare and novel
    tables, and the description (`:442-445`).
  - `:448-484` `register_callbacks`. `draw_volume` (`:456-462`) outputs the figure and
    the explorer link. `pick_params` and `draw_params` (`:469-484`) are all driven by
    `templates-grid.selectedRows`. These are the "selected-template views" in AC2.
- `src/logdash/app/main.py:61-77`: `create_app` with `suppress_callback_exceptions=True`.
  Each page layout is `partial(analysis.layout, store)` and is built **per request**, so
  whether tags exist is checked on every page load. Callbacks are registered once.
- `src/logdash/analyses/explorer.py:233`: existing `tooltipField` use in an AG Grid
  column.
- `src/logdash/analyses/anomalies.py:410-433`: existing page-local controls
  (`dmc.MultiSelect` labelled "Type (empty = all)") in a `dmc.Group`.
- `src/logdash/app/ui.py:22-28` `card`. `src/logdash/app/figures.py:82-87`
  `empty_figure(message, scheme)`.
- dash-ag-grid 35.3 (`uv.lock`). Its `getRowId` is a JS expression string, and
  `selectedRows` may be set from a callback. With `getRowId`, rows are matched by id.
- Import order: #28's `analyses/tags.py` does `from logdash.analyses import templates as
  tpl` and uses `tpl.KEY` in its class body. So `templates.py` must not import
  `analyses.tags` at module level.
- Tests: `tests/unit/test_templates.py:15-18` (module fixture `tpl`). `:132-139`
  (`test_tables_and_helpers`). `:142-183` (HTTP callback test with a local `call`
  helper). The synthetic templates include "Duplicate iptables rule…", "internal error:
  connection <NUM> closed unexpectedly by peer <IP>" and "Unexpected bgp session reset…"
  (`tests/synthetic.py:62,109,87`). `tests/unit/test_temporal.py:378-383` has a `walk`
  helper over a component tree.
- `tests/benchmark/test_latency.py:35-46` `Recorder`. `:173-192` `test_templates`.
- `README.md:61-63`: the benchmark description.

## 3. Design

ADR: `adrs/ADR002-semantic-tag-filters-on-pages.md` (new, `Status: proposed`). It binds
#30 and #31 to the same module, control ids, semantics and text matching.

**Shared module** `src/logdash/app/tag_filters.py` (step 1). It holds loading, matching,
filter semantics, controls and grid cells. Page-specific code stays in `templates.py`.

**Loading.** `load_tags(store)` calls #28's `read_tags` (imported inside the function, to
avoid the import cycle). It returns `None` when there are no tags **or no row is tagged**,
so an aborted run (for example, a bad key) also hides the UI.

**Matching by text.** `attach_tags(templates, tags)` left-joins on `template`, not on
`template_id`. `serve` never rebuilds `tags` (ADR001), and Drain3 may renumber ids when
`templates` is rebuilt. Duplicate texts in `tags` are reduced to the first row first.

**Page-local controls, always rendered.** `tag_controls("templates", visible)` renders two
`dmc.Switch` controls and one `dmc.MultiSelect`. Without tags the group is rendered with
`style={"display": "none"}`. Dash does not fire a callback whose `Input` components are
missing, so the controls stay in the layout, and callback registration does not depend
on the cache.

**Filtering flow (AC2).** A new callback `filter_grid` reads the three controls (plus
`selectedRows` as `State`) and outputs the grid's `rowData` and `selectedRows`:

1. Read `templates.parquet`. If tags are present, attach them and apply the filter. Then
   take the top `GRID_ROWS` by count, so "actionable only" shows the top 200 actionable
   templates, not the actionable ones among the overall top 200.
2. Keep the current selection if it is still in the new rows. Otherwise select the first
   row, or nothing if no row is left.
3. The existing callbacks (volume, explorer link, parameter select, parameter chart) are
   driven by `selectedRows`, so they follow automatically. With no selection they show
   "no template selected" and the link is plain `/explorer`.

`filter_grid` uses `prevent_initial_call=True`, because the layout already renders the
unfiltered rows and the controls start in the "no filter" state.

`getRowId="String(params.data.template_id)"` is set on the grid, so that replacing
`rowData` and `selectedRows` together selects the right row.

**Grid (AC1).** With tags, three columns are inserted after `template`: Actionable,
Component and Noise. Each cell shows `yes`, `no` or `""` (untagged), or the component name.
The template column gets `tooltipField: "summary"`, and `dashGridOptions` gets
`tooltipShowDelay: 300`. Untagged rows have `summary = None`, so they show no tooltip.
Without tags, the columns and row keys are exactly as today (AC3).

**Not filtered:** the rare and novel tables (open question 1). The description says so.

**Templates stage unchanged.** `Templates.version` and `depends_on` stay as they are.
The page reads `tags` from the cache at request time. A build dependency would also be a
cycle, since `tags` depends on `templates`.

**Alternatives rejected.**

- Tag filters in the global `Filters` store: most pages have no per-template rows to
  apply them to, and every page would need to change (ADR002).
- AG Grid client-side filtering (column filters or an external filter in JS): the
  selection would not follow, the volume and parameter views would show a hidden
  template, and JS is harder to test than a polars function.
- Joining tags on `template_id`: this gives wrong tags after a renumbering rebuild under
  `serve`.
- Leaving the controls out of the layout when there are no tags: the callback would then
  have missing inputs, and its registration would depend on what the cache held when the
  page was first rendered.
- Rendering booleans as AG Grid checkboxes: untagged (null) would look like "false".

## 4. Steps

### Commit 1: steps 1-2 (shared tag filter module and its tests)

**Step 1: new file `src/logdash/app/tag_filters.py`**

```python
"""Semantic tag filters shared by the pages (ADR002). Reads only the cache (ADR001)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import dash_mantine_components as dmc
import polars as pl
from dash.development.base_component import Component

from logdash.cache.store import CacheStore
from logdash.tagging.model import COMPONENTS

TAG_COLUMNS = ("tagged", "actionable", "component", "noise", "summary")
TOOLTIP_DELAY_MS = 300


@dataclass(frozen=True)
class TagFilter:
    """Active tag filters; the default restricts nothing."""

    hide_noise: bool = False
    actionable_only: bool = False
    components: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.hide_noise or self.actionable_only or bool(self.components)


def tag_filter(
    hide_noise: bool | None, actionable_only: bool | None, components: Sequence[str] | None
) -> TagFilter:
    """Filter from control values: ``None`` means off; unknown components are dropped."""
    return TagFilter(
        hide_noise=bool(hide_noise),
        actionable_only=bool(actionable_only),
        components=tuple(c for c in (components or ()) if c in COMPONENTS),
    )


def load_tags(store: CacheStore) -> pl.DataFrame | None:
    """Cached tags, or ``None`` if absent or if no template is tagged (pages hide the tag UI)."""
    # imported here: analyses.tags imports analyses.templates, which imports this module
    from logdash.analyses.tags import read_tags

    tags = read_tags(store)
    if tags is None or not tags["tagged"].any():
        return None
    return tags


def attach_tags(templates: pl.DataFrame, tags: pl.DataFrame) -> pl.DataFrame:
    """``templates`` plus ``TAG_COLUMNS``, matched on the template text (ADR002).

    Ids can be renumbered by a templates rebuild that did not re-tag, so the text is the
    key. Texts without tags get ``tagged = False`` and nulls. Row order and count are kept.
    """
    by_text = tags.select("template", *TAG_COLUMNS).unique(
        "template", keep="first", maintain_order=True
    )
    return templates.join(by_text, on="template", how="left", maintain_order="left").with_columns(
        pl.col("tagged").fill_null(False)
    )


def apply_tag_filter(df: pl.DataFrame, tag_filter: TagFilter) -> pl.DataFrame:
    """Rows of a frame with ``TAG_COLUMNS`` that pass the filter.

    Hide noise keeps untagged rows; actionable only and a component choice drop them.
    """
    if tag_filter.hide_noise:
        df = df.filter(~pl.col("noise").fill_null(False))
    if tag_filter.actionable_only:
        df = df.filter(pl.col("actionable").fill_null(False))
    if tag_filter.components:
        df = df.filter(pl.col("component").is_in(list(tag_filter.components)).fill_null(False))
    return df


def tag_controls(prefix: str, visible: bool) -> Component:
    """The three tag filter controls; rendered hidden (not omitted) when there are no tags."""
    hidden: dict[str, Any] = {} if visible else {"style": {"display": "none"}}
    return dmc.Group(
        [
            dmc.Switch(id=f"{prefix}-hide-noise", label="Hide noise", checked=False, size="sm"),
            dmc.Switch(
                id=f"{prefix}-actionable-only", label="Actionable only", checked=False, size="sm"
            ),
            dmc.MultiSelect(
                id=f"{prefix}-components",
                label="Component (empty = all)",
                data=list(COMPONENTS),
                value=[],
                clearable=True,
                size="xs",
                w=320,
            ),
        ],
        id=f"{prefix}-tag-filters",
        align="flex-end",
        gap="lg",
        mb="xs",
        **hidden,
    )


def tag_column_defs() -> list[dict[str, Any]]:
    """AG Grid columns for the tag cells (see ``tag_cells``)."""
    return [
        {"field": "actionable", "headerName": "Actionable", "width": 110},
        {"field": "component", "headerName": "Component", "width": 130},
        {"field": "noise", "headerName": "Noise", "width": 90},
    ]


def flag(value: bool | None) -> str:
    return "" if value is None else "yes" if value else "no"


def tag_cells(row: Mapping[str, Any]) -> dict[str, Any]:
    """Grid values of one row with ``TAG_COLUMNS``; untagged rows get empty cells, no summary."""
    return {
        "actionable": flag(row["actionable"]),
        "component": row["component"] or "",
        "noise": flag(row["noise"]),
        "summary": row["summary"],
    }
```

If ruff format rewraps any of this, accept its formatting.

**Step 2: new file `tests/unit/test_tag_filters.py`**, with the commit 1 tests from
section 5.

### Commit 2: steps 3-5 (Templates page)

**Step 3: `src/logdash/analyses/templates.py`, helpers**

- Add the imports
  `from logdash.app.tag_filters import TOOLTIP_DELAY_MS, TagFilter, apply_tag_filter, attach_tags, load_tags, tag_cells, tag_column_defs, tag_controls, tag_filter`
  (let ruff sort and wrap them).
- Add constants after `TEXT_CHARS`:
  ```python
  NO_SELECTION = "no template selected"
  DESCRIPTION = (
      "Drain3 over unique masked lines. Service and time filters apply to the volume "
      "chart; the tables are not filtered."
  )
  TAGS_DESCRIPTION = (
      " Semantic tags come from a language model at build time; hover a template for its "
      "plain-language summary. The tag filters apply to the top templates and the selected "
      "template, not to the rare and novel tables."
  )
  ```
- `param_figure`: as the first statement, add
  `if template_id is None: return figures.empty_figure(NO_SELECTION, scheme)`.
- `grid_rows`: keep the signature. Compute `with_tags = "tagged" in templates.columns`
  once. For each row, build the existing dict, and if `with_tags`, merge `tag_cells(r)`
  into it (`row |= tag_cells(r)`). Without tag columns the output is byte-for-byte what it
  is today.
- New functions, placed after `selected_id`:
  ```python
  def page_templates(store: CacheStore) -> tuple[pl.DataFrame, bool]:
      """``templates.parquet``, with the tag columns attached when tags are cached."""
      templates = store.read_parquet(KEY, "templates.parquet")
      tags = load_tags(store)
      return (templates, False) if tags is None else (attach_tags(templates, tags), True)


  def grid_columns(with_tags: bool) -> list[dict[str, Any]]:
      """Grid columns; with tags, the tag columns follow the text, which shows the summary on hover."""
  ```
  `grid_columns` returns the six current column dicts (`layout`, `:392-402`) in their
  current order. With `with_tags`:
  - the template column becomes
    `{"field": "template", "flex": 1, "minWidth": 300, "tooltipField": "summary"}`;
  - `tag_column_defs()` is inserted right after the template column.
  ```python
  def keep_selection(
      rows: list[dict[str, Any]], selected: list[dict[str, Any]] | None
  ) -> list[dict[str, Any]]:
      """The selected row if still shown, else the first row, else nothing."""
      sid = selected_id(selected)
      for row in rows:
          if row["template_id"] == sid:
              return [row]
      return rows[:1]


  def filtered_grid(
      store: CacheStore, tag_filter: TagFilter, selected: list[dict[str, Any]] | None
  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
      """Grid rows that pass the tag filter (before the top-N cut) and the selection to show."""
      templates, with_tags = page_templates(store)
      if with_tags:
          templates = apply_tag_filter(templates, tag_filter)
      rows = grid_rows(templates)
      return rows, keep_selection(rows, selected)
  ```

**Step 4: `templates.py`, `layout`**

- Replace `templates = store.read_parquet(KEY, "templates.parquet")` with
  `templates, with_tags = page_templates(store)`.
- Grid: `columnDefs=grid_columns(with_tags)`. Add
  `getRowId="String(params.data.template_id)"`. Add `"tooltipShowDelay": TOOLTIP_DELAY_MS`
  to `dashGridOptions`, next to `rowSelection`. Everything else stays the same.
- First card: `ui.card("Top templates (click a row)", tag_controls("templates", with_tags), grid)`.
- `description=DESCRIPTION + (TAGS_DESCRIPTION if with_tags else "")`.
- The rare and novel tables keep receiving `templates`. The extra columns are ignored by
  `list_table`, which selects its own columns.

**Step 5: `templates.py`, `register_callbacks`**

- `draw_volume`: after `tid = selected_id(rows)`, add
  ```python
  if tid is None:
      return figures.empty_figure(NO_SELECTION, theme), "/explorer"
  ```
- Add a new callback, first in `register_callbacks`:
  ```python
  @app.callback(
      Output("templates-grid", "rowData"),
      Output("templates-grid", "selectedRows"),
      Input("templates-hide-noise", "checked"),
      Input("templates-actionable-only", "checked"),
      Input("templates-components", "value"),
      State("templates-grid", "selectedRows"),
      prevent_initial_call=True,
  )
  def filter_grid(
      hide_noise: bool | None,
      actionable_only: bool | None,
      components: list[str] | None,
      selected: list[dict[str, Any]] | None,
  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
      return filtered_grid(store, tag_filter(hide_noise, actionable_only, components), selected)
  ```
  Import `State` from `dash`. Keep the function name `filter_grid`, because the
  benchmark looks it up by name.

Commit 2 includes the `test_templates.py` tests from section 5.

### Commit 3: steps 6-7 (benchmark, README)

**Step 6: `tests/benchmark/test_latency.py`**

Add a module constant directly above `def test_templates`:

```python
TAG_FILTER_CASES = (
    ("no tag filter", False, False, []),
    ("hide noise", True, False, []),
    ("actionable only", False, True, []),
    ("component network", False, False, ["network"]),
    ("all three", True, True, ["network", "storage"]),
)
```

At the end of `test_templates`:

```python
    for case, hide, actionable, components in TAG_FILTER_CASES:
        measure(
            "templates.filter_grid",
            case,
            CALLBACK_BUDGET_S,
            lambda h=hide, a=actionable, c=components: fns["filter_grid"](h, a, c, rows),  # type: ignore[misc]
        )
    measure(
        "templates.draw_volume",
        "no selection",
        CALLBACK_BUDGET_S,
        lambda: fns["draw_volume"]([], None, "light"),
    )
```

On a cache without tags this measures the no-tags path. With tags it measures the real
filter. `test_layouts` already times `templates.layout` in both cases.

**Step 7: `README.md:61-62`**

Change "template, text search, brushing, anomaly types and sorts)" to
"template, text search, brushing, anomaly types and sorts, template tag filters)".

Then run the quality gate from board.md. If `./cache` exists in the main checkout, also
run `uv run pytest -m slow --no-cov -s tests/benchmark/test_latency.py -k "templates or layouts"`
and paste the `templates.*` rows into the PR.

## 5. Tests

**Fixtures in `tests/unit/test_templates.py`** (commit 2). They use the module fixture
`tpl`, which is the session cache with `templates` built.

```python
NOISE = Tag(False, "network", True, "Harmless duplicate firewall rule.")
FAILURE = Tag(True, "network", False, "A peer closed a connection unexpectedly.")
ROUTINE = Tag(False, "other", False, "Routine message.")

def one_text(templates, needle):  # the single template text containing needle
    hit = templates.filter(pl.col("template").str.contains(needle, literal=True))
    assert hit.height == 1
    return hit["template"][0]
```

- `no_tags` (function scope): `shutil.rmtree(tpl.module_dir("tags"), ignore_errors=True)`
  before, and returns `tpl`.
- `tagged` (function scope):
  - Remove `module_dir("tags")`.
  - Read `templates.parquet` and pick `noise = one_text(.., "Duplicate iptables rule")`,
    `failure = one_text(.., "closed unexpectedly")` and
    `untagged = one_text(.., "bgp session reset")`.
  - Build `{text: NOISE if text == noise else FAILURE if text == failure else ROUTINE}`
    for every text except `untagged`.
  - Write `tags_frame(templates.select("template_id", "template"), mapping)` to
    `("tags", "tags.parquet")`, and write `meta.json` with the keys #28's
    `Tags.compute` writes (`available: True`, `model: "stub"`, `prompt_version: "1"`,
    `templates`, `tagged`, `untagged: 1`, `calls: 0`, `aborted: None`, `failures: []`).
  - Yield a `SimpleNamespace(store=tpl, noise=<id>, failure=<id>, untagged=<id>)` with the
    template ids of those texts.
  - Afterwards, remove `module_dir("tags")` again.

| Commit | File: test | Proves | AC |
|---|---|---|---|
| 1 | `test_tag_filters.py`: `test_tag_filter_from_controls` | `tag_filter(None, None, None) == TagFilter()` and is not `active`; `tag_filter(True, False, ["network", "bogus"])` gives `components == ("network",)`, `hide_noise`, `active` | AC2 |
| 1 | `test_tag_filters.py`: `test_apply_tag_filter_semantics` (a frame with ids 1-4: 1 tagged noise/network, 2 tagged actionable/network, 3 tagged storage, 4 untagged with nulls) | the inactive filter keeps all; hide noise gives `[2, 3, 4]`; actionable only gives `[2]`; components `("storage",)` gives `[3]`; hide noise and `("network",)` gives `[2]` | AC2 |
| 1 | `test_tag_filters.py`: `test_attach_tags_matches_on_text` | tags with ids 1-2 for texts "a" and "b", templates with ids 7-9 for texts "b", "a" and "new": row order and count kept; "b" gets b's tags; "new" has `tagged == False` and null `actionable`/`summary`; a tags frame with "a" twice still gives 3 rows | AC1, AC2 |
| 1 | `test_tag_filters.py`: `test_load_tags` (a `CacheStore(tmp_path)`) | with no `tags` dir it is `None`; with `meta.json` `available: False` and no parquet it is `None`; with a frame where every row has `tagged == False` it is `None`; with one tagged row it returns the frame | AC3 |
| 1 | `test_tag_filters.py`: `test_tag_controls_and_cells` | `tag_controls("p", True)` has id `p-tag-filters`, no `display: none`, children ids `p-hide-noise`, `p-actionable-only` and `p-components`, and the MultiSelect `data == list(COMPONENTS)`; `tag_controls("p", False).style == {"display": "none"}`; `tag_cells` of a tagged row gives `yes`/`no`/component/summary; of an untagged row gives `""`, `""`, `""` and `None`; `[c["field"] for c in tag_column_defs()] == ["actionable", "component", "noise"]` | AC1, AC3 |
| 2 | `test_templates.py`: `test_layout_without_tags_is_unchanged` (`no_tags`) | the `templates-grid` (found with a `walk` helper as in `test_temporal.py:378`) has the six current fields, no `tooltipField`, and row dicts with exactly the six current keys; `templates-tag-filters` has `style == {"display": "none"}`; the page description has no "Semantic tags"; `filtered_grid(store, TagFilter(True, True, ("storage",)), None)` returns all `grid_rows` (no-op) | AC3 |
| 2 | `test_templates.py`: `test_layout_with_tags_shows_columns_and_tooltip` (`tagged`) | the grid fields are `template_id, template, actionable, component, noise, count, variants, first_seen, last_seen`; the template column has `tooltipField == "summary"`; `getRowId` is set; the noise row has `noise == "yes"`, `component == "network"` and `summary == NOISE.summary`; the untagged row has `actionable == ""` and `summary is None`; the controls have no `display: none`; the description contains "Semantic tags" | AC1 |
| 2 | `test_templates.py`: `test_filtered_grid_moves_selection` (`tagged`) | hide noise with the noise row selected: that id is not in the rows and the selection is `[rows[0]]`; actionable only gives exactly `[failure]` and selects it; hide noise with the failure row selected keeps it; components `("storage",)` gives `([], [])` | AC2 |
| 2 | `test_templates.py`: `test_no_selection_views` | `param_figure(params, None, None, None)` has an annotation with text `NO_SELECTION` | AC2 |
| 2 | `test_templates.py`: extend `test_layout_and_callbacks_over_http`, running it with the `tagged` fixture | the `call` helper gets an optional `state` list (body key `"state": [{"id", "property", "value"}]`). Calling `filter_grid` with `hide-noise checked True`, the actionable switch `False`, components `[]` and state `selectedRows=[{"template_id": tagged.noise}]` returns `rowData` without `tagged.noise` and `selectedRows[0]["template_id"] == rowData[0]["template_id"]`. Calling `draw_volume` with `selectedRows=[]` returns `href == "/explorer"`. The existing assertions still pass | AC2 |
| 2 | existing `test_tables_and_helpers`, `test_figures` | unchanged and still pass (`grid_rows` without tag columns, `param_figure(…, 1, None, …)`) | AC3 |
| 3 | `tests/benchmark/test_latency.py::test_templates` (slow, real cache) | `filter_grid` for 5 cases and `draw_volume` with no selection stay under 300 ms warm | AC4 |

AC4 coverage: the quality gate enforces 90 %. Every new branch in `tag_filters.py` and
`templates.py` is covered by the rows above.

**Manual check (AC1 tooltip, not required for review).** With a cache built with a key,
run `uv run logdash serve --no-build`, open `/templates`, and hover a template text: the
summary appears after about 0.3 s. Turn on "Hide noise": the iptables row disappears, and
the volume chart and the explorer link follow the new selection.

## 6. Open questions

None blocks the plan. Each has a default.

1. **Rare and novel tables.** Should the tag filters also restrict them? *Default: no.*
   AC2 names the grid and the selected-template views only, and the description says so.
   Adding it later means passing `apply_tag_filter(templates, f)` to the two tables from a
   callback.
2. **Filter state across pages.** *Default: page-local, reset on every page load
   (ADR002).* A global "hide noise" would need a field in `Filters` and changes on every
   page.
3. **Untagged templates under "hide noise".** *Default: kept, because they are not known
   to be noise.* They are dropped by "actionable only" and by a component choice (ADR002).
4. **Tags present but none tagged** (for example, an aborted run). *Default: treated as
   "no tags"; the tag UI is hidden.*
