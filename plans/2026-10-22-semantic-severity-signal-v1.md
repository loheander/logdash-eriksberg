# Plan: semantic severity signal in the severity detector (story #32), v1

## 1. Story

Story [#32](https://github.com/loheander/logdash-eriksberg/issues/32), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24). Depends on #28 (tagging
stage, PR [#34](https://github.com/loheander/logdash-eriksberg/pull/34)). Feature branch
`feature/semantic-template-tags`, story branch
`feature/semantic-template-tags-32-semantic-severity-signal`.

**Goal.** The severity detector uses a "semantic severity" signal alongside the log level: a
surge of lines from templates tagged actionable counts as a severity burst, whatever level
they were logged at.

**Acceptance criteria** (copied from the story):

- [ ] AC1: With tags, a burst of an INFO-level template tagged actionable produces a severity episode on the synthetic dataset; the evidence says it came from the semantic signal.
- [ ] AC2: Templates tagged noise do not produce semantic severity episodes.
- [ ] AC3: Without tags in the cache, the detector's output is unchanged (existing severity tests pass untouched).
- [ ] AC4: The anomalies build stays within its current time budget. Coverage stays at or above 90 %.

Out of scope: tag filters on the Anomalies page (#31).

**Precondition.** #28 must be merged into `feature/semantic-template-tags`. Before step 1,
merge `origin/feature/semantic-template-tags` into the story branch. If #28 is not merged
yet, stop and report. Do not merge the #28 branch itself, because its diff would then show
up in this PR. This plan uses these names from #28 unchanged:

- `logdash.analyses.tags`: `KEY = "tags"`, `read_tags(store) -> pl.DataFrame | None`,
  `tags_frame(templates, tags: Mapping[str, Tag]) -> pl.DataFrame`, `TAGS_SCHEMA`, and
  `Tags(model_factory=..., sleep=...)`.
- `logdash.tagging.model.Tag(actionable, component, noise, summary)`.

If a name differs only in spelling, adapt the import. If the behaviour differs (for
example, `read_tags` does not return `None` when there is no `tags.parquet`), stop and
send the story back with `needs-replan`.

## 2. Context

- `src/logdash/detectors/severity.py` (285 lines, unchanged since #21):
  - `:1-50` module docstring. It explains the levels, gates, tail score, incidents and
    periodic merge.
  - `:68-70` `WINDOW_S`, `MIN_RATE`, `MERGE_GAP`. `:73-85` `Level(label, levels,
    min_peak, min_excess)` and `LEVELS` (WARNING: 10/300; ERROR+CRITICAL: 1/30).
  - `:182-186` `episode_text(level, f, how, when)` produces
    `"<label> burst: peak …/min vs baseline …/min (<how>), <n> extra <when>"`.
  - `:189-234` `burst_rows(x, level, entity, t0, settings)`: finds bursts, merges periodic
    episodes into one row demoted to the threshold (inline `note` at `:210`), and groups
    incidents.
  - `:237-285` `Severity`: `version` `:244-247`, `depends_on = ("temporal",)` `:249`.
    `detect` reads only `temporal/rate_60.parquet` and loops over `LEVELS` and services
    (`:270-277`). It sets `meta["episodes"]` and
    `meta["max_services_starting_in_one_minute"]` (`:278-284`).
- `src/logdash/analyses/templates.py:183-189` writes `template_counts.parquet`
  (`template_id, bin (1 min, naive UTC), program, n`, all levels together; 95 551 rows,
  0.02 s to read on the real cache). `:243` `short(text, limit)`. `templates.parquet` has
  `template_id, template, …`.
- `src/logdash/analyses/anomalies.py:378-381`: `Anomalies.depends_on` is the union of the
  detectors' `depends_on`. `:384-387`: the version includes each detector's version.
- #28, `src/logdash/analyses/tags.py`: `KEY` `:31`, `TAGS_SCHEMA` `:37` (`template_id,
  template, tagged, actionable, component, noise, summary`), `tags_frame` `:55`,
  `read_tags` `:82`, `Tags` `:90` (`page = False`, `calls_model = True`). Without a key
  the stage writes only `meta.json`, so `read_tags` returns `None`.
- #28, `src/logdash/build.py:132-141`: `serve` (`allow_model=False`) skips a stale `tags`
  stage, and stages that depend on it read its old cache (ADR001, decision 1). A stage is
  rebuilt when any upstream stage was rebuilt in the same run (`:111`).
- ADR002 (proposed in #29, PR #35, not on this branch): tags are matched to templates
  **by text**, never by `template_id`, because Drain3 may renumber templates when
  `templates` is rebuilt under `serve` and `tags` is not. If the same text appears twice,
  the first row wins. If no row is tagged, it counts as no tags. This story follows the
  same rules but does not import #29's module.
- Tests: `tests/unit/test_severity.py` builds hand-made `rate_60` tables only. The helpers
  `info`, `poisson` and `bursts_every` are at `:41-51` and `:167-172`.
  `tests/unit/test_anomalies.py:41-44` has the `stage` fixture
  (`build_with(built, Templates(), Features(), Pca(), Temporal(), Hosts(), Anomalies())`),
  `:407` asserts `Anomalies().depends_on`, `:660` has `during`, `:808-821` checks the
  severity rows of the shared data, and `:1024` calls `build_with` again.
  `tests/unit/test_contextual.py:165` builds the same registry on a dataset of its own.
  `tests/synthetic.py` assigns a **random level to every row** (`:42-43`, `:186`), so its
  only single-level injections are the ERROR burst (`:269-277`) and the INFO request
  steps, which are steady rather than a burst.
- Real data (measured, see section 3): every failure-like template is logged at exactly
  one level. Of the 53 templates matching `(?i)error|fail|exception|refused|timeout`
  (118 579 lines), 111 625 lines are WARNING, 6 591 INFO, 347 UNKNOWN and 16
  ERROR/CRITICAL.

## 3. Design

**A third series per service, from `template_counts`.** Next to WARNING and ERROR, each
service gets the series "lines per minute of templates tagged actionable and not noise".
The lines are taken from `templates/template_counts.parquet`, summed over the actionable
template ids, at every level. That series goes through the existing `burst_rows`
unchanged (tail score, floors, periodic merge, incidents), with
`SEMANTIC = Level("ACTIONABLE", (), 1, 30)`. Those are the ERROR floors: actionable lines
are failures, so a small surge counts. The tail gate needs at least 7 lines in the peak
minute at a zero baseline, plus 30 extra lines over the incident.

**Which templates.** Read `read_tags(store)`. If it returns `None` or no row is
`tagged`, there is no semantic signal and nothing else changes. Otherwise keep the first
row per `template` text, keep the rows with `tagged & actionable & ~noise`, and semi-join
them on `template` with the current `templates.parquet` to get the current ids. A template
tagged both actionable and noise is excluded (AC2). Untagged templates are excluded.

**Evidence.** A semantic row reads `ACTIONABLE burst: peak …; semantic signal: 2 templates
tagged actionable (any log level), most lines from '<template>'`. "Most lines" means the
actionable template with the most lines in that service inside the row's window (ties go
to the lower id). The text is cut with `short(text, TOP_CHARS)`.

**No duplicate rows.** When an actionable template is logged at WARNING or ERROR, its
surge is also a level burst. On the real cache with stand-in tags, 2 of 9 semantic
incidents overlapped a level row of the same service: fluent WARNING 08:06 and
neutron-openvswitch-agent WARNING 05:10. In that case the semantic incident does not get a
row of its own. Every overlapping level row of that service gets
`; semantic signal agrees: <semantic burst sentence>, <templates text>` appended, and keeps
its `raw`. Two rows overlap when their `[bin, bin + window_s)` windows intersect. A level
row demoted as periodic noise (it contains `PERIODIC_NOTE`) never absorbs a semantic
incident, because that would hide a real failure at the threshold rank.

**Meta.** With tags, `meta["semantic"] = {"actionable_templates": k, "episodes": rows of
their own, "merged_into_level_rows": m}`. `meta["episodes"]` keeps counting all rows.
Without tags, the meta is exactly as today.

**Stage dependency.** `Severity.depends_on = ("temporal", "templates", "tags")`, so
`anomalies` depends on `tags`. When the tags change (new key, other model, forced
re-tag), `anomalies` is rebuilt in the same `logdash build`. Under `serve`, a key change
makes `anomalies` stale while `tags` is skipped. `anomalies` is then rebuilt against the
old tags and rebuilt again by the next `logdash build`. ADR001 accepts this. It costs one
extra rebuild of about 42 s on the real cache, only when the tag settings change.
`Severity.version` starts with `3:` and includes `SEMANTIC` and `TOP_CHARS`.

**Synthetic data.** The shared dataset has no INFO-level burst, and adding one would
shift the structural and volume fits that other tests rely on (see the comments at
`tests/synthetic.py:101-108`). So add an opt-in injection: `SyntheticSpec.semantic_rows`
(default 0). It adds INFO lines of one failure-like template on `libvirt` over 3 minutes
at 60 % of the span, from a generator of its own, so the default data is byte-identical.
The new integration test builds a dataset of its own, as `test_contextual.py` does. It
builds only events, templates, temporal, tags (stub model) and an anomalies stage limited
to the severity detector. Measured cost: write the dataset 1.6 s, events 1.4 s, templates
and temporal 2.6 s (load average 6-7 on 4 CPUs, DuckDB 2 threads).

**Time budget (AC4).** Measured on 2026-10-22 against the real cache (38.8 M lines). The
machine was shared with other agents (load average 2.6-7.0 on 4 CPUs). The stand-in tags
were the 53 regex templates above. Results:

- current `Severity.detect`: 0.18 s; meta of the last real build: 0.198 s;
- semantic series and bursts: 0.05-0.11 s;
- the whole `anomalies` stage: 42.0 s (manifest).

The budget for this story: the severity detector stays at or below **0.5 s** on the real
cache with tags. The rest of the stage is untouched.

**Alternatives rejected.**

- *Count only actionable lines at levels the level series miss* (INFO, DEBUG, UNKNOWN).
  `template_counts` has no level, so the detector would have to scan `events`. That scan
  took 2.0 s (2 threads), ten times the detector. Adding a level column to
  `template_counts` changes the templates stage and every consumer of the table. Merging
  overlaps gives the same "no duplicates" result for free.
- *Keep both rows when they overlap.* The same incident would take two of the top severity
  ranks, and reciprocal rank fusion already ranks within detector and kind.
- *Raise the level row's `raw` to the semantic z.* That mixes two scores in one number
  that the evidence explains as one tail. The agreement is kept in the text.
- *Change the shared synthetic dataset.* Rejected, see above.
- *Import #29's `tag_filters.attach_tags`.* #29 is not a dependency of this story. The
  five lines of text matching are local.
- *Set `template_id` on semantic rows.* It would change the detector's column set (an
  existing test checks `set(base.REQUIRED) == set(rows.columns)`), and it would narrow the
  Explorer link to one of possibly several templates. Listed as an open question.

**No ADR.** The decisions are local to this detector. Matching by text applies ADR002's
rule, and the stage dependency follows ADR001.

## 4. Steps

### Step 1: `src/logdash/detectors/severity.py`

1. Imports: add `from datetime import datetime, timedelta` (replacing the existing
   `timedelta`-only import), `from logdash.analyses import tags as semantic_tags`,
   `from logdash.analyses import templates as tpl`, `from logdash.analyses.templates import short`,
   `from logdash.cache.store import CacheStore`.
2. Constants after `LEVELS`:
   ```python
   # Lines of the templates tagged actionable and not noise, at any level: failures, so the
   # ERROR floors (no peak floor, 30 extra lines)
   SEMANTIC = Level("ACTIONABLE", (), 1, 30)
   SIGNAL = "semantic signal"
   TOP_CHARS = 80  # template text in the evidence
   PERIODIC_NOTE = "; ranked at the threshold as periodic noise, z is the strongest episode's"
   ```
   In `burst_rows`, replace the local `note = "…"` (`:210`) with `PERIODIC_NOTE`. The text
   is the same, so the output does not change.
3. New code after `burst_rows`:
   ```python
   class Semantic(NamedTuple):
       """The actionable templates of the current cache and their lines per minute."""

       counts: pl.DataFrame  # template_id, bin, program, n (MISSING program dropped)
       texts: dict[int, str]  # template_id -> template text
       templates: int  # number of actionable templates


   def load_semantic(store: CacheStore) -> Semantic | None:
       """Templates tagged actionable and not noise, matched to current ids by text.

       ``None`` when there are no tags (no key, not built) or none is tagged: then the
       detector is the level detector alone. The first row of a repeated text wins.
       """
       tags = semantic_tags.read_tags(store)
       if tags is None or not tags["tagged"].any():
           return None
       wanted = (
           tags.unique(subset="template", keep="first", maintain_order=True)
           .filter(
               pl.col("tagged")
               & pl.col("actionable").fill_null(False)
               & ~pl.col("noise").fill_null(False)
           )
           .select("template")
       )
       current = store.read_parquet(tpl.KEY, "templates.parquet").select("template_id", "template")
       actionable = current.join(wanted, on="template", how="semi")
       ids = actionable["template_id"].to_list()
       counts = (
           pl.scan_parquet(store.path(tpl.KEY, "template_counts.parquet"))
           .filter(pl.col("template_id").is_in(ids) & (pl.col("program") != MISSING))
           .collect()
       )
       texts = dict(zip(ids, actionable["template"].to_list(), strict=True))
       return Semantic(counts, texts, len(ids))


   def semantic_rows(
       sem: Semantic, t0: datetime, length: int, settings: Settings
   ) -> list[dict[str, Any]]:
       """Burst rows of each service's series of actionable lines (services in name order)."""
       by_service = sem.counts.group_by("bin", "program").agg(pl.col("n").sum())
       rows: list[dict[str, Any]] = []
       for program in sorted(set(by_service["program"].to_list())):
           part = by_service.filter(pl.col("program") == program)
           x = temporal.dense_series(part["bin"], part["n"], t0, WINDOW_S, length)
           rows += burst_rows(x, SEMANTIC, str(program), t0, settings)
       return rows


   def signal_text(sem: Semantic, row: dict[str, Any]) -> str:
       """How many actionable templates the row's window holds and which has the most lines."""
       lo = row["bin"]
       hi = lo + timedelta(seconds=row["window_s"])
       inside = (
           sem.counts.filter(
               (pl.col("program") == row["entity"]) & (pl.col("bin") >= lo) & (pl.col("bin") < hi)
           )
           .group_by("template_id")
           .agg(pl.col("n").sum())
           .sort(["n", "template_id"], descending=[True, False])
       )
       k = inside.height
       text = f"{k} template{'' if k == 1 else 's'} tagged actionable (any log level)"
       if k:
           top = sem.texts[int(inside["template_id"][0])]
           text += f", most lines from '{short(top, TOP_CHARS)}'"
       return text


   def overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
       a_end = a["bin"] + timedelta(seconds=a["window_s"])
       b_end = b["bin"] + timedelta(seconds=b["window_s"])
       return bool(a["bin"] < b_end and b["bin"] < a_end)


   def merge_semantic(
       level_rows: list[dict[str, Any]], found: list[dict[str, Any]], sem: Semantic
   ) -> tuple[list[dict[str, Any]], int]:
       """Level rows plus the semantic rows no level row covers, and how many were merged.

       A semantic row that overlaps a (non-periodic) level row of the same service is
       written into that row's evidence instead; the level row keeps its score.
       """
       out = list(level_rows)
       alone: list[dict[str, Any]] = []
       merged = 0
       for s in found:
           what = signal_text(sem, s)
           hits = [
               i
               for i, r in enumerate(out)
               if r["entity"] == s["entity"]
               and PERIODIC_NOTE not in r["evidence"]
               and overlaps(r, s)
           ]
           if not hits:
               alone.append({**s, "evidence": f"{s['evidence']}; {SIGNAL}: {what}"})
               continue
           merged += 1
           head = s["evidence"].split("; ")[0]  # the burst sentence, without periodic notes
           for i in hits:
               out[i] = {**out[i], "evidence": f"{out[i]['evidence']}; {SIGNAL} agrees: {head}, {what}"}
       return out + alone, merged
   ```
   Run `ruff format`, which may rewrap the long lines.
4. `Severity`:
   - `version = (f"3:{WINDOW_S}:{MIN_RATE}:{MERGE_GAP}:{LEVELS}:{SEMANTIC}:{TOP_CHARS}:" f"{temporal.ROLL_WINDOW}:…")`,
     keeping the rest of the string as it is. Extend the comment above it: "the semantic
     constants too".
   - `depends_on: tuple[str, ...] = (temporal.KEY, tpl.KEY, semantic_tags.KEY)`.
   - In `detect`, after the `for level in LEVELS` loop and before `starting = …`:
     ```python
     sem = load_semantic(ctx.store)
     if sem is not None:
         found = semantic_rows(sem, t0, length, ctx.settings)
         rows, merged = merge_semantic(rows, found, sem)
         ctx.meta["semantic"] = {
             "actionable_templates": sem.templates,
             "episodes": len(found) - merged,
             "merged_into_level_rows": merged,
         }
         ctx.log(
             f"severity: semantic signal from {sem.templates} actionable templates, "
             f"{len(found) - merged} episodes, {merged} merged into level rows"
         )
     ```
     The `rate.is_empty()` early return stays first, so an empty rate table never reads
     the tags.
5. Module docstring: add this paragraph before "Fleet-wide events":

   > Semantic signal. With semantic tags in the cache (``analyses/tags.py``, issue #24), a
   > third series per service counts the lines of the templates tagged actionable and not
   > noise, whatever level they were logged at (``SEMANTIC``, with the ERROR floors: these
   > lines are failures). Tags are matched to the current templates by text, not id,
   > because ``logdash serve`` may rebuild ``templates`` but never ``tags``. Its rows read
   > ``ACTIONABLE burst: …; semantic signal: …`` and name the template with the most lines.
   > A semantic incident that overlaps a non-periodic WARNING or ERROR row of the same
   > service gets no row of its own. That row's evidence gains ``; semantic signal agrees:
   > …`` and keeps its score. Without tags (no key, not built, nothing tagged) the rows and
   > meta are those of the level series alone.

   Also change the first line to: "Severity bursts: a surge of WARNING or ERROR lines, or
   of lines tagged actionable, in one service."

### Step 2: registries that build `Anomalies()` with every detector

`anomalies` now depends on `tags`, so every test registry that includes `Anomalies()` with
the default detectors also needs `Tags()`. Without a key, `Tags()` writes only its meta.

- `tests/unit/test_anomalies.py:43` and `:1024`: insert `Tags()` after `Templates()`. Add
  `from logdash.analyses.tags import Tags`.
- `tests/unit/test_anomalies.py:407`: the expected tuple becomes
  `("features", "hosts", "pca", "tags", "templates", "temporal")`.
- `tests/unit/test_contextual.py:165`: insert `Tags()` after `Templates()`, with the
  import.
- Run `grep -rn "Anomalies()" tests src` for any other caller and treat it the same way.

### Step 3: `tests/unit/test_semantic_severity.py` (new), hand-made tables

Write the helpers like this, so the tests read like `test_severity.py`:

```python
"""Semantic severity: bursts of lines tagged actionable, merged with level bursts."""

T0 = datetime(2023, 5, 21)
MINUTES = 600
LOST = "Lost connection to broker <IP>"
SLOW = "Retrying request to <*> after timeout"
CHATTER = "Heartbeat sent to <IP>"
ACT = Tag(True, "network", False, "a failure")


def burst(start: int, counts: list[int], minutes: int = MINUTES) -> np.ndarray: ...
def counts_frame(x: np.ndarray, **const: Any) -> pl.DataFrame:
    """One row per non-zero minute: bin, n (Int64) and the constant columns."""
def make_store(tmp_path, levels, lines, tags, minutes=MINUTES) -> CacheStore:
    """levels {(program, level): x} -> temporal/rate_60.parquet (plus test_severity.info);
    lines {(template_id, text, program): x} -> templates/templates.parquet (template_id,
    template; one row per id) and templates/template_counts.parquet (template_id, bin,
    program, n; empty with that schema if no lines); tags {text: Tag} -> tags/tags.parquet
    via tags_frame(templates, tags), or no tags file when tags is None."""
def run(store) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Severity().detect on a fresh DetectContext; returns (rows, ctx.meta)."""
```

Import `info`, `poisson` and `bursts_every` from `tests.unit.test_severity`. The tests are
listed in section 5 (U1-U9).

### Step 4: `tests/synthetic.py`, an opt-in INFO burst

- Constants after `ERROR_TEMPLATE`:
  ```python
  # INFO lines of a failure-like template (only with SyntheticSpec.semantic_rows > 0): a surge
  # that no level series sees, for the semantic severity signal (tests/unit/test_semantic_severity.py)
  SEMANTIC_TEMPLATE = "Lost connection to AMQP server on {ip}:{n}, retrying in {n} seconds"
  SEMANTIC_SERVICE = SERVICES[3]  # libvirt
  SEMANTIC_HOST = HOSTS[4]
  ```
- `SyntheticSpec`: `semantic_rows: int = 0` with the comment "INFO lines of
  SEMANTIC_TEMPLATE in a three-minute window (0: none), from a generator of their own".
- `GroundTruth`: `semantic_start: datetime = START`, `semantic_end: datetime = START`,
  `semantic_service: str = SEMANTIC_SERVICE[0]`.
- In `generate_rows`, directly after the ERROR burst block:
  ```python
  sem = random.Random(spec.seed + 5000)
  truth.semantic_start = START + (end - START) * 0.60
  truth.semantic_end = truth.semantic_start + timedelta(minutes=3)
  for _ in range(spec.semantic_rows):
      ts = _random_time(sem, truth.semantic_start, truth.semantic_end)
      row = _row(ts, sem, SEMANTIC_HOST, SEMANTIC_SERVICE, _fill(sem, SEMANTIC_TEMPLATE))
      row["log_level"] = "INFO"
      rows.append(row)
  ```
  The main `rng` is not touched, so the default dataset stays identical.

### Step 5: `tests/unit/test_semantic_severity.py`, synthetic integration

Add to the same file:

```python
SPEC = SyntheticSpec(semantic_rows=300)
AMQP = "Lost connection to AMQP server"
PEER = "closed unexpectedly by peer"  # the ERROR burst's template


class RuleModel:
    """Stub tag model: the first rule whose needle is in the template wins."""

    RULES = (
        (AMQP, Tag(True, "network", False, "lost the message broker")),
        (PEER, Tag(True, "network", False, "a peer closed the connection")),
        ("Duplicate iptables rule", Tag(False, "network", True, "duplicate firewall rule")),
    )

    def tag(self, template: str) -> Tag:
        for needle, tag in self.RULES:
            if needle in template:
                return tag
        return Tag(False, "other", False, "routine message")


@pytest.fixture(scope="module")
def semantic(tmp_path_factory):
    """A dataset of its own, tagged by RuleModel, with an anomalies stage of severity only."""
    data_dir = tmp_path_factory.mktemp("semantic_data")
    truth = write_dataset(data_dir / "synthetic_preprocessed.csv.zst", SPEC)
    settings = Settings(
        data_dir, tmp_path_factory.mktemp("semantic_cache"), "1GB", 2, openrouter_api_key="sk-test"
    )
    detectors = DetectorRegistry()
    detectors.add(DETECTORS.get("severity"))
    registry = Registry()
    for analysis in (
        Templates(),
        Temporal(),
        Tags(model_factory=lambda _s: RuleModel(), sleep=lambda _: None),
        Anomalies(detectors),
    ):
        registry.add(analysis)
    run_build(settings, registry=registry, log=lambda _: None)
    return CacheStore(settings.cache_dir, settings), truth


def view(store: CacheStore, tmp_path: Path, tags: dict[str, Tag] | None) -> CacheStore:
    """A cache with the fixture's temporal and templates (symlinked) and its own tags."""
```

`view` symlinks `temporal` and `templates`, as `run_stage` does in `test_anomalies.py`. When
`tags` is not `None`, it writes `tags_frame(templates, tags)`. Import `during` from
`tests.unit.test_anomalies` and `utc` from `tests.synthetic`. The tests are S1-S4 in
section 5.

### Step 6: time check (manual, not committed)

After the gate passes, run this scratch script (outside the repository) against the real
`cache/`. It needs no key and does not write to `cache/`:

```python
# symlink cache/temporal and cache/templates into a temp dir; write tags/tags.parquet with
# tags_frame(templates, {t: Tag(True, "other", False, "x") for t in texts matching
# r"(?i)error|fail|exception|refused|timeout"}); time Severity().detect three times with
# and three times without the tags file; print the medians and meta["semantic"].
```

Put the medians, the load average (`uptime`) and `meta["semantic"]` in the PR
description. The pass mark is a median of at most 0.5 s with tags. If a real
`cache/tags/tags.parquet` exists, also time that one.

### Commits

- **Commit 1, steps 1-3:** "Add the semantic severity signal to the severity detector
  (story #32)". The dependency on `tags` and the test registries that need `Tags()` must
  land together.
- **Commit 2, steps 4-5:** "Add an INFO-level actionable burst to the synthetic data and
  test it (story #32)".

Run the full quality gate (board.md) before each commit.

## 5. Tests

Hand-made tables (`tests/unit/test_semantic_severity.py`):

| # | Test | Proves | AC |
|---|---|---|---|
| U1 | `test_info_burst_of_an_actionable_template_is_a_semantic_row`: levels `{("api","INFO"): poisson(4, 20)}`, lines `{(1, LOST, "api"): burst(300, [10, 25, 10])}`, tags `{LOST: ACT}` | one row; `bin == T0+300 min`, `window_s == 180`, service `api`; evidence starts with `"ACTIONABLE burst: peak 25/min vs baseline 0/min (Poisson P="` and ends with `"; semantic signal: 1 template tagged actionable (any log level), most lines from 'Lost connection to broker <IP>'"`; `raw == approx(tail_z(25, severity.MIN_RATE)[0])`; `meta["semantic"] == {"actionable_templates": 1, "episodes": 1, "merged_into_level_rows": 0}` | AC1 |
| U2 | `test_noise_untagged_and_routine_templates_give_no_semantic_row`: U1's lines plus a steady `(2, CHATTER, "api"): np.full(MINUTES, 2.0)`, parametrized over the tags `{LOST: Tag(True, "network", True, "x")}`, `{LOST: Tag(False, "network", False, "x")}` and `{CHATTER: ACT}` (LOST untagged; CHATTER steady, so no burst) | no rows; `meta["semantic"]["episodes"] == 0`; `actionable_templates` is 0, 0 and 1 | AC2 |
| U3 | `test_tags_match_templates_by_text_not_id` | a tags file whose LOST row has `template_id` 7 while `templates.parquet` has 1 still gives U1's row. With two rows for LOST (first not actionable, then actionable), there is no row: the first wins | AC1 |
| U4 | `test_a_semantic_burst_inside_an_error_burst_annotates_the_error_row`: ERROR and lines both `burst(300, [10, 15, 10])` on `api` | the run without tags gives one ERROR row. With tags there is still one row: its evidence starts with the untagged evidence followed by `"; semantic signal agrees: ACTIONABLE burst: peak 15/min"` and ends with `"most lines from 'Lost connection to broker <IP>'"`. `raw` is equal; `merged_into_level_rows == 1`, `episodes == 0` | AC1 |
| U5 | `test_a_periodic_level_row_does_not_absorb_a_semantic_burst`: `x = bursts_every(40, 9)`, `x[100:103] = 300`, WARNING on `agent`; lines `burst(100, [10, 25, 10], 2000)`, `minutes=2000` | two rows: the WARNING row contains `severity.PERIODIC_NOTE` and not `"semantic signal"`; the other starts with `"ACTIONABLE burst"` and contains `"; semantic signal: 1 template"` | AC1 |
| U6 | `test_the_busiest_actionable_template_is_named`: LOST `burst(300, [5, 8, 5])`, SLOW `burst(300, [5, 20, 5])` on `api`, both ACT | one row, `"peak 28/min"`, `"2 templates tagged actionable (any log level), most lines from 'Retrying request to <*> after timeout'"` | AC1 |
| U7 | `test_without_tags_rows_and_meta_are_unchanged`: WARNING `poisson(2, 20)` with `[300:303] = 500` and ERROR `burst(300, [10, 15, 10])` on `api`, INFO on `db`; lines LOST on `api`, CHATTER `burst(200, [40, 40, 40])` on `db` | no tags file, and a tags file with nothing tagged (`tags_frame(templates, {})`), give `assert_frame_equal` rows and equal meta without `"semantic"` | AC3 |
| U8 | `test_tags_without_actionable_templates_change_only_the_meta`: U7's tables, tags `{LOST: Tag(False, …), CHATTER: Tag(False, "other", True, …)}` | rows equal the U7 rows; `meta["semantic"] == {"actionable_templates": 0, "episodes": 0, "merged_into_level_rows": 0}` | AC2, AC3 |
| U9 | `test_missing_program_is_not_a_semantic_service`: U1's lines on `temporal.MISSING` | no rows; `actionable_templates == 1` | AC1 |
| U10 | `test_version_and_dependencies` | `Severity().depends_on == ("temporal", "templates", "tags")`; `version.startswith("3:")` and `str(severity.SEMANTIC) in version` | AC1 |

Synthetic (`semantic` fixture, `SyntheticSpec(semantic_rows=300)`):

| # | Test | Proves | AC |
|---|---|---|---|
| S1 | `test_synthetic_info_burst_tagged_actionable_is_a_severity_episode` | in `anomalies.parquet` exactly one severity row for `truth.semantic_service` overlaps `during(truth.semantic_start, truth.semantic_end)`. Its evidence starts with `"ACTIONABLE burst"` and contains `"; semantic signal: 1 template tagged actionable (any log level), most lines from 'Lost connection to AMQP server"`. `raw >= Settings().burst_k`. No row for that service starts with `WARNING` or `ERROR` (the level series do not see it). `meta["semantic"]["actionable_templates"]` equals the number of templates in `templates.parquet` containing `AMQP` or `PEER`. `merged_into_level_rows == 1` | AC1 |
| S2 | `test_synthetic_error_burst_of_an_actionable_template_is_one_annotated_row` | the rows of `truth.error_service` during the ERROR window are exactly one; it starts with `"ERROR burst"` and contains `"; semantic signal agrees: ACTIONABLE burst"` | AC1 |
| S3 | `test_synthetic_noise_tag_gives_no_semantic_episode` | `view` with every template containing `AMQP` tagged `Tag(True, "network", True, "x")`: `Severity().detect` has no row whose evidence contains `"semantic signal"` | AC2 |
| S4 | `test_synthetic_without_tags_is_unchanged` | `view` with no tags and `view` with `{}` (nothing tagged) give equal frames. Neither contains `"semantic signal"` or an `ACTIONABLE` row, and `"semantic"` is not in the meta | AC3 |

Unchanged tests that must still pass:

- `tests/unit/test_severity.py`, not edited (AC3). Its stores have no `tags/` directory,
  so `load_semantic` returns `None`.
- `test_anomalies.py::test_severity_rows_are_the_error_burst_and_the_spike_warnings_only`:
  the shared stage now includes `Tags()` without a key, so the rows are unchanged (AC3).

AC4: the quality gate (coverage of at least 90 %) and the time check in step 6.

## 6. Open questions (non-blocking; defaults used)

1. **Overlap with a level burst.** Default: one row, the level row, with the semantic
   agreement in its evidence. The alternative is to keep both rows.
2. **Semantic floors.** Default: the ERROR floors (no peak floor, 30 extra lines). With
   the stand-in tags on the real cache this gives 9 incidents, 7 of them new rows (all in
   the 09:20-09:41 incident plus fluent at 07:50). Real model tags may give a different
   count. The floors are constants in the version, so changing them later is cheap.
3. **`template_id` on semantic rows.** Default: none. Setting it would narrow the
   Explorer link to the busiest template.
4. **Templates tagged both actionable and noise.** Default: excluded, which follows from
   AC2.
5. **An extra `anomalies` rebuild when the tags change**, including one under `serve`
   after a key change (about 42 s on the real cache). Default: accepted (ADR001).
