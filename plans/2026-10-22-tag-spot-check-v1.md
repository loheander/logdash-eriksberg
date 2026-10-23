# Plan: semantic tags, spot check of 30 templates (story #33), v1

## 1. Story

Story [#33](https://github.com/loheander/logdash-eriksberg/issues/33), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24). Feature branch
`feature/semantic-template-tags`, story branch
`feature/semantic-template-tags-33-tag-spot-check`. Depends on #28 (merged, PR #34) and
#29–#32 (PRs #35, #37, #38, #39). This story is implemented last.

**Goal.** Measure how well the tags match a team member's judgement: 30 sampled
templates with their tags in a review sheet, marked agree or disagree by a person, with
the agreement rate recorded.

**Acceptance criteria** (copied from the story):

- [ ] AC1: A real tagging run (with `OPENROUTER_API_KEY` from `.env`; worktrees do not have it, run in the main checkout) has produced the tags.
- [ ] AC2: A review sheet with 30 templates, sampled reproducibly (fixed seed, stratified so noise and actionable templates both appear), each with its tags and summary and empty agree/disagree columns, is committed.
- [ ] AC3: A team member fills in the sheet. The agreement rate, overall and per tag, is computed from it and recorded in the feature PR to `main`.

Out of scope: re-tagging or prompt tuning based on the result (a follow-up issue if
agreement is low).

**Who does what.** The implementer writes the tooling only (commits 1–2). The real
tagging run costs money and needs the key, and the marks need a person. Those are the
**user steps U1–U5** in section 4. The implementer must not do them, must not run
`logdash build` in the main checkout, and must not call OpenRouter in any way.

## 2. Context

Line numbers are as of `fd63eb7` (feature branch after #28).

- `src/logdash/analyses/tags.py:31-45`: `KEY = "tags"`, `FILE`, `META`, `TAGS_SCHEMA`
  (`template_id, template, tagged, actionable, component, noise, summary`).
  `:74-86`: `read_meta(store)` and `read_tags(store)`, which read the cache only
  (`None` when there is no `tags.parquet`). `:139-153`: `meta.json` keys `available,
  model, prompt_version, templates, tagged, untagged, calls, aborted, failures`.
- `src/logdash/analyses/templates.py:48`: `KEY = "templates"`. `:174-181`:
  `templates.parquet` (`template_id, template, count, first_seen, last_seen,
  n_variants`). `:182-189`: `template_counts.parquet` (`template_id, bin, program, n`),
  with `program` coalesced to `"(missing)"` (`:57`). #29 edits this file, but not these
  two outputs.
- `src/logdash/tagging/model.py:10-11`: `COMPONENTS`, `PROMPT_VERSION`.
- `src/logdash/cli.py:17-21` `_common`, `:24-45` `build_parser`, `:48-54` `_settings`,
  `:98` `COMMANDS`, `:101-107` `main`.
- `src/logdash/build.py:57-75`: `stage_statuses(settings, registry)` gives
  `fresh`, `stale`, `missing` or `unverified` per stage.
- `src/logdash/cache/store.py:53-69`: `CacheStore` helpers.
- `tests/conftest.py:62-74`: autouse shield. No test can reach OpenRouter or read the
  real `.env`.
- `README.md:25-33`: "Other commands". `:36-59`: Configuration.
- Real data (main checkout cache, read-only check): 281 templates, all texts unique,
  40 services in `template_counts.parquet`. No `cache/tags` exists yet.
- None of the plans for #29–#32 changes `tags.py`, `tagging/` or `cli.py`, so this
  story only reads their shared interface (`read_tags`, `read_meta`).

## 3. Design

**Tooling, not a one-off script.** A new module `src/logdash/tagging/spotcheck.py`
(pure functions over frames and files) and a CLI command with two actions:

- `logdash spot-check sample` reads the cache (tags, templates, per-service counts),
  draws a stratified sample with a fixed seed, and writes the sheet
  `spot-checks/tags-spot-check.csv` plus a sidecar `spot-checks/tags-spot-check.json`
  with the sampling metadata. It never calls a model.
- `logdash spot-check score` reads the filled sheet and the sidecar, computes agreement
  overall and per tag, prints a Markdown report and writes it to
  `spot-checks/tags-spot-check-results.md`. That text goes into the feature PR to `main`.

**Strata.** Every tagged template falls into exactly one stratum, in this order:
`actionable` (actionable is true), `noise` (noise is true, actionable false), `other`.
Untagged templates are left out, since there is nothing to judge.

**Allocation.** Round-robin, one place at a time in `STRATA` order, skipping full
strata, until 30 places are given or every stratum is exhausted. With enough templates
this gives 10/10/10, and a small stratum's shortfall goes to the others. So noise and
actionable templates both appear whenever the data has them.

**Reproducibility.** Within a stratum, candidates are sorted by template **text** (it
survives Drain renumbering) and drawn with `random.Random(f"{seed}:{stratum}").sample`.
String seeds are deterministic across runs. Seed 24 (the parent issue). The sheet lists
the picked rows by `template_id`. Re-running `sample` on the same cache gives
byte-identical files.

**What the marker sees.** Per row: `template_id`, `service` (the program with most lines
for the template, to help judge the component), `count` (lines), `template`,
`actionable`, `component`, `noise` (as `yes`/`no`), `summary`, then four empty columns
`agree_actionable`, `agree_component`, `agree_noise`, `agree_summary`, and `note`. All
four tags from #24, the summary included, are judged.

**CSV, tolerant reading.** CSV opens in any spreadsheet and GitHub renders it. It is
written as UTF-8 with BOM (so Excel shows `…` correctly). Spreadsheets in a Swedish
locale save with `;`, so the reader takes the delimiter from the header line (`,`, `;`
or tab) and accepts `agree`/`a`/`yes`/`y` and `disagree`/`d`/`no`/`n` in any case. An
empty cell is "unmarked" and is not counted. Anything else is an error naming the line
and column.

**Agreement.** Per tag: `agree / (agree + disagree)`. Overall: the same over all four
tags' marks pooled. Also reported: templates with every tag agreed, and a list of
disagreements with the marker's notes (input for a possible follow-up issue).

**Safety.** `sample` refuses to overwrite an existing sheet without `--force`, because
the sheet may already hold marks. It warns when the `tags` stage is not `fresh`, or
when templates are untagged.

**Alternatives rejected.**

- A Markdown sheet: template texts can contain `|`, and filling a table cell by cell in
  an editor is error-prone.
- Separate scripts under `scripts/`: no tests or coverage, and the sampling must be
  reproducible and reviewed.
- A dated sheet name: there is one spot check per tag version. The sidecar records the
  model and prompt version.
- Weighting the sample by line count: a few noise templates hold most lines, and the
  sample would be mostly noise.
- Hiding the tags from the marker (blind labelling): the story asks for an agree/disagree
  sheet with the tags shown.
- An automatic "agreement too low" threshold: whether agreement is good enough is a human
  call (out of scope).

No ADR: nothing here binds other stories.

## 4. Steps

### Step 0 (implementer, before commit 1)

Bring the story branch up to date: `git fetch origin`, then
`git merge origin/feature/semantic-template-tags` (with #29–#32 merged by then). If
`read_tags`, `read_meta`, `TAGS_SCHEMA` or the two templates outputs differ from
section 2, stop and send the story back with a PR comment.

### Commit 1: step 1 (spot-check module and its unit tests)

**Step 1: `src/logdash/tagging/spotcheck.py`**

Docstring: "Spot check of semantic tags (issue #24): a reproducible stratified sample
for a person to mark, and agreement from the marked sheet. Reads the cache only; never
calls a model."

```python
SAMPLE_SIZE = 30
SEED = 24  # the parent issue
STRATA = ("actionable", "noise", "other")
TAGS = ("actionable", "component", "noise", "summary")
AGREE_COLUMNS = tuple(f"agree_{t}" for t in TAGS)
SHEET_COLUMNS = ("template_id", "service", "count", "template", *TAGS,
                 *AGREE_COLUMNS, "note")
AGREE_WORDS = frozenset({"agree", "a", "yes", "y"})
DISAGREE_WORDS = frozenset({"disagree", "d", "no", "n"})
DEFAULT_SHEET = Path("spot-checks/tags-spot-check.csv")
DEFAULT_REPORT = Path("spot-checks/tags-spot-check-results.md")
META_KEYS = ("model", "prompt_version", "seed", "size", "templates", "untagged",
             "population", "sampled")
```

Functions:

- `def stratum(actionable: bool, noise: bool) -> str`: `"actionable"` if actionable,
  else `"noise"` if noise, else `"other"`.
- `def population(tags: pl.DataFrame, templates: pl.DataFrame, counts: pl.DataFrame) -> pl.DataFrame`:
  - `top = counts.group_by("template_id", "program").agg(pl.col("n").sum())`, sorted by
    `["template_id", "n", "program"]` with `descending=[False, True, False]`, then
    `.group_by("template_id", maintain_order=True).first()`, selecting
    `template_id` and `program` renamed to `service`.
  - Take `tags.filter(pl.col("tagged"))`, left-join
    `templates.select("template_id", "template", "count")` on
    `["template_id", "template"]` (a stale tags stage gives null counts, not wrong ones),
    then left-join `top` on `template_id`.
  - Add `stratum` with `pl.when(actionable).then("actionable").when(noise).then("noise").otherwise("other")`.
  - Return columns `template_id, service, count, template, actionable, component,
    noise, summary, stratum`, sorted by `template_id`.
- `def allocate(sizes: Mapping[str, int], n: int) -> dict[str, int]`: the round-robin
  of section 3. Returns a value for every name in `STRATA` (a missing size counts as 0).
- `def sample(pop: pl.DataFrame, n: int = SAMPLE_SIZE, seed: int = SEED) -> pl.DataFrame`:
  `sizes` = rows per stratum. For each stratum, `cands = sorted(texts of that stratum)`,
  `picked |= set(random.Random(f"{seed}:{s}").sample(cands, quota[s]))`. Return
  `pop.filter(pl.col("template").is_in(picked)).sort("template_id")`.
- `def sheet_meta(tags: pl.DataFrame, pop: pl.DataFrame, picked: pl.DataFrame, tags_meta: Mapping[str, Any], *, seed: int, n: int) -> dict[str, Any]`:
  returns `{"model": tags_meta.get("model"), "prompt_version": tags_meta.get("prompt_version"),
  "seed": seed, "size": n, "templates": pop.height,
  "untagged": tags.height - pop.height, "population": {s: count in pop},
  "sampled": {s: count in picked}}` with every stratum present (0 when absent), keys in
  `META_KEYS` order.
- `def sidecar_path(sheet: Path) -> Path`: `sheet.with_suffix(".json")`.
- `def write_sheet(path: Path, picked: pl.DataFrame, meta: Mapping[str, Any]) -> Path`:
  `mkdir(parents=True, exist_ok=True)`. Write the CSV with `csv.writer` (default
  dialect), `open(path, "w", encoding="utf-8-sig", newline="")`: the header
  `SHEET_COLUMNS`, then one row per picked row: booleans as `yes`/`no`, a null `count`
  or `service` as `""`, the agree columns and `note` as `""`. Write the sidecar with
  `json.dumps(meta, indent=2) + "\n"`. Return the sidecar path.
- `def parse_mark(value: str | None, where: str) -> bool | None`:
  `v = (value or "").strip().lower()`. Empty gives `None`, `AGREE_WORDS` gives `True`,
  `DISAGREE_WORDS` gives `False`. Otherwise
  `raise ValueError(f"{where}: {value!r} is not agree or disagree")`.
- `@dataclass(frozen=True) class MarkedRow`: `template_id: int`,
  `values: dict[str, str]` (the four `TAGS` columns as written), `marks: dict[str, bool | None]`
  (by tag), `note: str`.
- `def read_sheet(path: Path) -> list[MarkedRow]`:
  - `text = path.read_text(encoding="utf-8-sig")`; the header is the first line;
    `delimiter` is the first of `","`, `";"`, `"\t"` found in it (default `","`).
  - `csv.DictReader(io.StringIO(text), delimiter=delimiter)`. Missing columns of
    `SHEET_COLUMNS`: `ValueError(f"{path}: missing column(s) {', '.join(missing)}")`.
  - Rows are numbered from line 2. A row whose `template_id` is blank is skipped
    (spreadsheets add empty rows). A non-integer id raises
    `ValueError(f"{path}:{line}: template_id {raw!r} is not a number")`; a repeated id
    raises `ValueError(f"{path}:{line}: template_id {tid} appears twice")`.
  - Marks via `parse_mark(rec[f"agree_{tag}"], f"{path}:{line}: agree_{tag}")`.
    `note = " ".join((rec["note"] or "").split())`.
- `def read_sidecar(path: Path) -> dict[str, Any]`: `json.loads`. Missing keys of
  `META_KEYS` raise `ValueError(f"{path}: missing key(s) {', '.join(missing)}")`.
- `@dataclass(frozen=True) class TagScore`: `tag: str, agree: int, disagree: int,
  unmarked: int`, property `rate -> float | None` (`agree / (agree + disagree)`, `None`
  when both are 0).
- `@dataclass(frozen=True) class Agreement`: `per_tag: tuple[TagScore, ...]`,
  `overall: TagScore` (tag `"overall"`, sums), `rows: int`, `all_agreed: int` (rows
  whose four marks are all `True`), `disagreements: tuple[tuple[int, tuple[str, ...], str], ...]`
  (template id, the disagreed tag labels, note), in sheet order.
- `def score(rows: Sequence[MarkedRow]) -> Agreement`. A disagreed tag's label is
  `f"{tag}={row.values[tag]}"` for actionable, component and noise, and `"summary"` for
  the summary.
- `def render_report(result: Agreement, meta: Mapping[str, Any]) -> str`. Exactly
  (`pct(r)` is `"n/a"` for `None`, otherwise `f"{r:.0%}"`; `cell(s)` is
  `" ".join(s.split()).replace("|", "\\|")`):

  ```text
  ## Semantic tags spot check

  Model `{model}`, prompt version {prompt_version}. {rows} templates marked, sampled with seed {seed} from {templates} tagged templates ({strata}).[ {untagged} untagged template(s) could not be sampled.]

  | Tag | Agree | Disagree | Unmarked | Agreement |
  |---|---:|---:|---:|---:|
  | actionable | … | … | … | 90% |
  | component | … |
  | noise | … |
  | summary | … |
  | **overall** | … | … | … | **92%** |

  Templates with every tag agreed: {all_agreed} of {rows}.

  ### Disagreements

  | Template | Tags | Note |
  |---|---|---|
  | 17 | actionable=no, noise=yes | {cell(note)} |
  ```

  `{strata}` is `", ".join(f"{s} {sampled[s]} of {population[s]}" for s in STRATA)`.
  The bracketed sentence appears only when `untagged > 0`. With no disagreements, the
  table is replaced by the line `No disagreements.` The text ends with one `"\n"`.

### Commit 2: steps 2–4 (CLI, docs)

**Step 2: `src/logdash/cli.py`**

- Module docstring: ``"""Command line interface: ``logdash build | status | serve | spot-check``."""``
- In `build_parser`, after `serve`:
  ```python
  spot = sub.add_parser("spot-check", help="sample tagged templates for review, score the marks")
  actions = spot.add_subparsers(dest="action", required=True)
  sample = actions.add_parser("sample", help="write a review sheet from the cached tags")
  _common(sample)
  sample.add_argument("--out", type=Path, default=spotcheck.DEFAULT_SHEET, help="sheet (CSV) to write")
  sample.add_argument("--size", type=int, default=spotcheck.SAMPLE_SIZE, help="templates to sample")
  sample.add_argument("--seed", type=int, default=spotcheck.SEED, help="random seed")
  sample.add_argument("--force", action="store_true", help="overwrite an existing sheet")
  score = actions.add_parser("score", help="agreement from a marked sheet")
  score.add_argument("--sheet", type=Path, default=spotcheck.DEFAULT_SHEET, help="marked sheet (CSV)")
  score.add_argument("--out", type=Path, default=spotcheck.DEFAULT_REPORT, help="report (Markdown) to write")
  ```
- `cmd_spot_check(args) -> int` dispatches to `_spot_sample` or `_spot_score` on
  `args.action`. Add `"spot-check": cmd_spot_check` to `COMMANDS`.
- `_spot_sample(args) -> int`:
  1. `load_builtin()`; `settings = _settings(args)`;
     `store = CacheStore(settings.cache_dir, settings)`.
  2. If `args.out.exists()` and not `args.force`: print
     `error: {out} exists (it may hold marks); pass --force to overwrite` to stderr,
     return 2.
  3. `tags = read_tags(store)`. If `None`: print
     ``error: no semantic tags in the cache; run `logdash build --only tags` with OPENROUTER_API_KEY set``
     to stderr, return 2.
  4. State of `tags` from `stage_statuses(settings, REGISTRY)`. If not `"fresh"`, print
     `warning: the tags stage is {state}; the sheet may not match the current templates`
     to stderr.
  5. Inside `try:` read `templates.parquet` and `template_counts.parquet` from
     `store` (key `templates`), build `pop`, `picked = sample(pop, args.size, args.seed)`,
     `meta = sheet_meta(...)` with `read_meta(store)`, and `write_sheet`.
     `except (FileNotFoundError, ValueError) as exc:` print `error: {exc}`, return 2.
  6. If `meta["untagged"]`: print to stderr
     ``warning: {n} untagged template(s) left out; `logdash build --only tags --force` retries them``.
  7. Print `wrote {out} and {sidecar}: {picked.height} templates ({strata})`, with
     `{strata}` as in the report. If `picked.height < args.size`, also print
     `only {picked.height} tagged templates could be sampled (asked for {size})`.
     Return 0.
- `_spot_score(args) -> int`: inside `try:` `rows = read_sheet(args.sheet)`,
  `meta = read_sidecar(sidecar_path(args.sheet))`; `except (FileNotFoundError, ValueError)`
  print `error: {exc}`, return 2. Then `result = score(rows)`, `text = render_report(result, meta)`,
  `args.out.parent.mkdir(parents=True, exist_ok=True)`, `args.out.write_text(text, encoding="utf-8")`,
  print `text`, then `wrote {out}`. If `result.overall.unmarked`: print to stderr
  `warning: {u} of {result.rows * len(TAGS)} marks are empty and not counted`. Return 0.
- Imports: `from pathlib import Path`, `from logdash.analyses.tags import read_meta, read_tags`
  inside `_spot_sample` (keep CLI import light, like `create_app`), and
  `from logdash.tagging import spotcheck` at module top (stdlib + polars only). Call its
  functions module-qualified (`spotcheck.sample(...)`), since `build_parser` uses the
  local names `sample` and `score` for the sub-parsers.

**Step 3: `spot-checks/README.md`** (new; guidance for the marker):

```markdown
# Spot checks

`tags-spot-check.csv` is a sample of 30 log templates with the semantic tags a language
model gave them (issue #24). `tags-spot-check.json` records how it was sampled.

## Marking the sheet

Open the CSV in a spreadsheet (LibreOffice, Numbers, Google Sheets, or Excel via
Data > From Text/CSV) and save it back as CSV. Judge each tag on its own from the
template text, the service and the line count, not from the log level.

| Column | Write `agree` when |
|---|---|
| `agree_actionable` | you would also say the message does (`yes`) or does not (`no`) report a failure or degraded state an operator should look into |
| `agree_component` | the component is the one area the message is about, or a defensible choice when two fit |
| `agree_noise` | you would also hide (`yes`) or keep (`no`) this message while triaging |
| `agree_summary` | the summary says correctly, in plain language, what happened |

Write `agree` or `disagree` (`a`/`d`, `yes`/`no` and `y`/`n` also work). Leave a cell
empty only if you cannot judge it; empty cells are not counted. Say why in `note` when
you disagree. Do not change `template_id`.

Then run `uv run logdash spot-check score` and commit the sheet and
`tags-spot-check-results.md`.
```

**Step 4: `README.md`**, "Other commands" block, two lines after the `--only tags` line:

```
uv run logdash spot-check sample                # review sheet of 30 tagged templates (cache only)
uv run logdash spot-check score                 # agreement from the marked sheet
```

Then run the quality gate from board.md, push, comment on the PR that commits 1–2 are
done and that U1–U5 below are for the user, and move the story to In review. **Stop
there.**

### User steps (not for the implementer or the reviewer)

These need the key, cost money (about $0.02 for 281 templates) or need a person. They
happen while the PR is in Human review, before it is merged. An agent may help with U2
or U4 only when the user asks in their own message.

- **U1, real tagging run (AC1).** In the main checkout
  `/Users/emny/src/logdash-eriksberg` (it has `.env`):
  `git fetch origin`, then
  `git checkout --detach origin/feature/semantic-template-tags-33-tag-spot-check`
  (detached, because an agent worktree may still hold the branch), `uv sync`, then
  `uv run logdash build --only tags`. Expect `tagged 281 of 281 templates`. If some are
  untagged, run `uv run logdash build --only tags --force` once.
- **U2, sheet (AC2).** Same checkout: `uv run logdash spot-check sample`. Commit
  `spot-checks/tags-spot-check.csv` and `spot-checks/tags-spot-check.json` with the
  message `Add tag spot-check sheet (story #33)`, then
  `git push origin HEAD:feature/semantic-template-tags-33-tag-spot-check`.
- **U3, marks (AC3).** A team member fills the four `agree_*` columns and `note` as
  `spot-checks/README.md` says. Commit `Record tag spot-check marks (story #33)`.
- **U4, score (AC3).** `uv run logdash spot-check score`. Commit
  `spot-checks/tags-spot-check-results.md` with `Score tag spot check (story #33)` and
  push. Then merge the PR.
- **U5, record (AC3).** When all stories are Done and the orchestrator opens the
  feature PR to `main`, its body includes the contents of
  `spot-checks/tags-spot-check-results.md` under a heading "Spot check". If overall
  agreement is low, open a follow-up issue (out of scope here).

## 5. Tests

New file `tests/unit/test_spotcheck.py` (commit 1) and additions to
`tests/unit/test_build.py` (commit 2). Unit tests build frames by hand: a helper
`make_tags(n_actionable, n_noise, n_other, n_untagged=0)` returns a `TAGS_SCHEMA`
frame with distinct texts `f"tpl {i}"`, plus matching `templates` (counts `i + 1`) and
`counts` frames (two programs per template, `nova` with more lines for even ids).

| Commit | Test | Proves | AC |
|---|---|---|---|
| 1 | `test_allocate_round_robin` (parametrised) | `{a:50, n:50, o:200}` gives 10/10/10; `{a:3, n:50, o:200}` gives 3/14/13; `{a:0, n:2, o:5}` with 30 gives 0/2/5; a missing stratum key counts as 0 | AC2 |
| 1 | `test_population_joins_and_strata` | untagged rows are dropped; `service` is the top program (tie broken alphabetically); a row with actionable and noise both true is `actionable`; a mismatched template text gives a null count | AC2 |
| 1 | `test_sample_is_reproducible_and_stratified` | same seed: same rows; seed 25: different rows; 30 rows with 10 per stratum; rows sorted by `template_id`; shuffling the input frame's row order changes nothing; renumbering `template_id` picks the same texts | AC2 |
| 1 | `test_sample_takes_all_when_few` | 12 tagged templates give 12 rows | AC2 |
| 1 | `test_write_sheet_and_read_back` (`tmp_path`) | header equals `SHEET_COLUMNS`; booleans are `yes`/`no`; the file starts with the UTF-8 BOM; agree columns and note are empty; `read_sheet` gives every mark `None`; the sidecar has `META_KEYS`; writing twice gives identical bytes | AC2 |
| 1 | `test_read_sheet_variants` | `;` and tab delimiters; `Agree`, ` y `, `D`, `no`; blank trailing rows skipped; note whitespace collapsed | AC3 |
| 1 | `test_read_sheet_errors` (parametrised) | an unknown mark, a missing column, a non-numeric id and a repeated id each raise `ValueError` naming the line or column | AC3 |
| 1 | `test_score_and_report` | for a hand-made sheet: per-tag counts and rates, pooled overall, `all_agreed`, unmarked cells not counted, the disagreement labels (`actionable=no`, `summary`), a `|` in a note escaped; the report contains the table rows, `**overall**`, the strata line and the untagged sentence; with no disagreements it says `No disagreements.`; `pct(None)` is `n/a` | AC3 |
| 1 | `test_read_sidecar_missing_key` | a sidecar without `seed` raises `ValueError` | AC3 |
| 2 | `test_cli_spot_check_sample_and_score` (`tmp_path`, `monkeypatch.chdir`, cache written with `CacheStore.write_parquet`/`write_json`: `tags/tags.parquet`, `tags/meta.json`, `templates/templates.parquet`, `templates/template_counts.parquet`) | `main(["spot-check", "sample", "--cache", c])` returns 0 and writes the default sheet and sidecar; stderr warns that the tags stage is `missing` (no manifest); a second run returns 2 without `--force` and leaves the file unchanged; `--force` returns 0; after filling marks with `csv`, `main(["spot-check", "score"])` returns 0, writes the report and prints it | AC2, AC3 |
| 2 | `test_cli_spot_check_errors` | without tags: `sample` returns 2 and names `logdash build --only tags`; `score` with no sheet returns 2; `score` with a bad mark returns 2 | AC2, AC3 |
| 2 | `test_cli_spot_check_requires_action` | `main(["spot-check"])` exits with `SystemExit` code 2 (argparse) | – |

No test builds the real tags or calls a model. The autouse shield in `conftest.py`
also fails any test that tries. Coverage of the new code must keep the 90 % gate.

**AC coverage.** AC2's sampling (reproducible, stratified, empty columns) is proven by
the tests above. AC1, the committed sheet (AC2) and AC3's marks and recorded rate are
manual checks done in U1–U5. The reviewer checks the tooling, not these files.

## 6. Open questions

Each has a default; none blocks the plan.

1. **Is the summary judged as a tag?** #24 lists it among the four tags. *Default:
   yes.* `agree_summary` counts in the overall rate. Dropping it means removing it from
   `TAGS`.
2. **Strata and quotas.** *Default:* actionable, noise and other, 10 each, round-robin
   fill. Another split is a constant change.
3. **Sheet format.** *Default: CSV* with a tolerant reader (comma, semicolon or tab;
   BOM). A Google Sheet would need an export back to CSV anyway.
4. **Where the result is recorded.** The story says the feature PR to `main`, which the
   orchestrator opens later. *Default:* the report is also committed as
   `spot-checks/tags-spot-check-results.md`, and the orchestrator copies it into the
   feature PR body (U5).
