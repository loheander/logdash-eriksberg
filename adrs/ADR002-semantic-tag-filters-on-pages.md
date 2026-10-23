# ADR002: Semantic tag filters on dashboard pages

## Status

proposed

## Context

Story #28 caches one row of semantic tags per template (`actionable`, `component`,
`noise`, `summary`) in `cache/tags/tags.parquet`, readable with
`logdash.analyses.tags.read_tags(store) -> pl.DataFrame | None` (ADR001: pages read only
the cache and never call the model). Issue #24 asks for the filters "hide noise",
"actionable only" and "by component" on three pages: Templates (#29), Explorer (#30) and
Anomalies (#31). If each story chose its own control ids, semantics for untagged
templates and way of matching tags to templates, the three pages would disagree.

Three facts constrain the design:

- `logdash serve` rebuilds stale stages but never the `tags` stage (ADR001). After a data
  change, `templates` can be rebuilt while `tags` keeps its old cache, and Drain3 may
  renumber the template ids. The template text is stable; the id is not.
- Page layouts are built per request (`partial(analysis.layout, store)` in
  `src/logdash/app/main.py`), but callbacks are registered once at start-up. A Dash
  callback whose `Input` components are missing from the page does not fire.
- `analyses/tags.py` imports `analyses/templates.py`, so `templates.py` cannot import
  `tags.py` at module level.

## Decision

1. **One shared module**, `src/logdash/app/tag_filters.py`, holds the tag UI and filter
   semantics for every page: `load_tags`, `attach_tags`, `TagFilter`, `tag_filter`,
   `apply_tag_filter`, `tag_controls`, `tag_column_defs` and `tag_cells`. Pages do not
   re-implement them.
2. **Page-local controls, not global filters.** Each page renders
   `tag_controls(prefix, visible)` with the ids `<prefix>-hide-noise` (Switch,
   `checked`), `<prefix>-actionable-only` (Switch, `checked`) and `<prefix>-components`
   (MultiSelect, `value`, empty = all). The global filter store (`FILTER_STORE_ID`)
   is not extended, because most pages have no per-template rows to filter. The state
   resets when a page loads.
3. **Semantics** (the filters combine with AND):
   - *hide noise* drops rows whose `noise` is true. Untagged rows stay, because nothing
     says they are noise.
   - *actionable only* keeps rows whose `actionable` is true. Untagged rows are dropped.
   - *component* keeps rows whose `component` is one of the chosen values. Untagged rows
     are dropped.
4. **Tags are matched to templates by template text**, never by `template_id`
   (`attach_tags`). A text that is new since the last tagging run is untagged. If the
   tags hold the same text twice, only the first row is used, so templates are never
   duplicated.
5. **"No tags" means the tag UI is hidden.** `load_tags` returns `None` when
   `read_tags` returns `None` or when no row is tagged (for example, a run aborted by a
   bad key). Pages then show no tag columns. The controls still render, but hidden
   (`style={"display": "none"}`), so callbacks keep their inputs. Filtering is a no-op.
6. `tag_filters.py` imports `logdash.analyses.tags` only inside `load_tags`, to avoid the
   import cycle `templates -> tag_filters -> tags -> templates`.

## Consequences

- #30 and #31 reuse the module. To get the current template ids that pass a filter, they
  take `templates.parquet`, call `attach_tags` and then `apply_tag_filter`, and use the
  `template_id` column (Explorer can then restrict its SQL to those ids).
- A later "global tag filter" would need a new ADR, a field in `Filters`, and a change on
  every page.
- If tags are stale after a templates rebuild under `serve`, unchanged texts keep their
  tags and new texts show as untagged until the next `logdash build`.
