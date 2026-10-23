# Plan: semantic tags, spot check of 30 templates (story #33), v2

## 1. Story

Story [#33](https://github.com/loheander/logdash-eriksberg/issues/33), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24), PR
[#40](https://github.com/loheander/logdash-eriksberg/pull/40). Feature branch
`feature/semantic-template-tags`, story branch
`feature/semantic-template-tags-33-tag-spot-check`.

**Goal.** Measure how well the tags match a team member's judgement: 30 sampled
templates with their tags in a review sheet, marked agree or disagree by a person, with
the agreement rate recorded.

**Acceptance criteria** (copied from the story):

- [ ] AC1: A real tagging run (with `OPENROUTER_API_KEY` from `.env`; worktrees do not have it, run in the main checkout) has produced the tags.
- [ ] AC2: A review sheet with 30 templates, sampled reproducibly (fixed seed, stratified so noise and actionable templates both appear), each with its tags and summary and empty agree/disagree columns, is committed.
- [ ] AC3: A team member fills in the sheet. The agreement rate, overall and per tag, is computed from it and recorded in the feature PR to `main`.

Out of scope: re-tagging or prompt tuning based on the result (a follow-up issue if
agreement is low).

**Scope of v2.** Plan v1 (`plans/2026-10-22-tag-spot-check-v1.md`) is implemented on
this branch (commits `ecf7ab9`, `ea81378`) and passed agent review. v2 is a small
change on top of it, requested by the user before the sheet is marked: marks in the
`agree_*` columns must be explicit `agree` or `disagree`. Everything else in v1
stands, including the user steps U1–U5 (repeated in section 4 for convenience).

**Who does what.** The implementer makes the one commit in section 4 and nothing else.
The implementer must not run `logdash build` in the main checkout, must not call
OpenRouter, and must not do U1–U5.

## 2. Context

- `src/logdash/tagging/spotcheck.py:33-34`: `AGREE_WORDS = {"agree", "a", "yes", "y"}`
  and `DISAGREE_WORDS = {"disagree", "d", "no", "n"}`. Used only by `parse_mark`
  (grep shows no other use in `src/` or `tests/`).
- `src/logdash/tagging/spotcheck.py:158-159`: `_yes_no` writes the `actionable` and
  `noise` tag columns as `yes`/`no`. This is why `yes`/`no` as a mark is ambiguous: a
  marker writing `no` next to `actionable=no` means "agreed, not actionable" but is
  scored as a disagreement (review note 2 on PR #40).
- `src/logdash/tagging/spotcheck.py:188-196`: `parse_mark(value, where)`. Strips and
  lowercases, returns `None` for empty, `True`/`False` for the word sets, otherwise
  raises `ValueError(f"{where}: {value!r} is not agree or disagree")`.
- `src/logdash/tagging/spotcheck.py:228`: `read_sheet` calls `parse_mark` with
  `where = f"{path}:{line}: agree_{t}"`, so the error names file, line and column.
- `src/logdash/cli.py:173-179`: `_spot_score` turns a `ValueError` into
  `error: <message>` on stderr and exit code 2. No change needed.
- `spot-checks/README.md:12-21`: the marking table and the sentence at line 19 that
  lists the shorthands.
- `tests/unit/test_spotcheck.py:175-180`: helper `sheet_text(rows, delimiter)`; tag
  cells are fixed (`actionable=yes`, `noise=no`).
- `tests/unit/test_spotcheck.py:183-198`: `test_read_sheet_variants` uses the marks
  `["Agree", " y ", "D", "no"]` and expects `[True, True, False, False]`. It breaks
  with this change and must be updated.
- `tests/unit/test_spotcheck.py:201-219`: `test_read_sheet_errors`, parametrised over
  `(text, match)`.
- `tests/unit/test_spotcheck_cli.py:73-88`: `test_cli_spot_check_errors` writes the mark
  `maybe` into `agree_noise` and asserts `not agree or disagree` on stderr.
- `tests/unit/test_spotcheck_cli.py:22-54` and `tests/unit/test_spotcheck.py` scoring
  tests already use only `agree`/`disagree`/empty. No change needed there.
- `spot-checks/` holds only `README.md`. No sheet has been sampled or marked yet, so no
  existing marks need migrating.

## 3. Design

Accept exactly two marks, `agree` and `disagree`, compared after `strip()` and
`lower()` (so `Agree`, ` DISAGREE ` work). An empty or whitespace-only cell is still
"not judged" (`None`). Every other non-empty value, including `yes`, `no`, `y`, `n`,
`a`, `d` and near misses such as `agreed`, raises the existing `ValueError`, which the
CLI reports with file, line and column and exit code 2.

Replace the two word sets with one mapping:

```python
MARKS: dict[str, bool] = {"agree": True, "disagree": False}
```

`parse_mark` looks the normalised value up in `MARKS`. One mapping is simpler than two
single-element sets and keeps the vocabulary in one place.

The error text stays `"{where}: {value!r} is not agree or disagree"`. It already says
what is allowed, and keeping it means the existing CLI assertion still holds.

Alternatives rejected:
- **Keep `yes`/`no` but reject them only where they collide with the tag value.**
  Still ambiguous for the `component` and `summary` columns, and harder to explain.
- **Rename the tag values to `true`/`false`.** Changes the sheet format for no gain;
  the user asked for explicit marks instead.
- **Keep the `a`/`d` shorthands.** The user's decision drops them explicitly.

No ADR: the change is local to this story.

## 4. Steps

### Commit 1: steps 1–3 (explicit marks, docs, tests)

Message: `Accept only explicit agree/disagree marks (story #33)`.

**Step 1: `src/logdash/tagging/spotcheck.py`**

- Replace lines 33-34 (`AGREE_WORDS`, `DISAGREE_WORDS`) with:

  ```python
  MARKS: dict[str, bool] = {"agree": True, "disagree": False}
  ```

- Replace the body of `parse_mark` (lines 188-196) with:

  ```python
  def parse_mark(value: str | None, where: str) -> bool | None:
      """`agree` -> True, `disagree` -> False, empty -> None (case and outer
      whitespace ignored). Anything else is an error: yes/no would clash with the
      yes/no tag columns."""
      v = (value or "").strip().lower()
      if not v:
          return None
      if v in MARKS:
          return MARKS[v]
      raise ValueError(f"{where}: {value!r} is not agree or disagree")
  ```

  The signature and error text are unchanged.

**Step 2: `spot-checks/README.md`**

- In the table, make clear that `yes`/`no` are the tag values, not marks. Replace the
  `agree_actionable` and `agree_noise` rows (lines 14 and 16) with:

  ```markdown
  | `agree_actionable` | you would also say the message does (tag `yes`) or does not (tag `no`) report a failure or degraded state an operator should look into |
  ```

  ```markdown
  | `agree_noise` | you would also hide (tag `yes`) or keep (tag `no`) this message while triaging |
  ```

- Replace lines 19-21 (from `Write \`agree\` or \`disagree\`` to
  `Do not change \`template_id\`.`) with:

  ```markdown
  In each `agree_*` cell write exactly `agree` or `disagree` (upper or lower case is
  fine). Do not write `yes`/`no`, `y`/`n` or `a`/`d`: the tag columns already hold
  `yes`/`no`, so those would be ambiguous, and `spot-check score` rejects them with the
  line and column. Leave a cell empty only if you cannot judge it; empty cells are not
  counted. Say why in `note` when you disagree. Do not change `template_id`.
  ```

- Leave the rest of the file unchanged.

**Step 3: tests** (details in section 5)

- `tests/unit/test_spotcheck.py`: update `test_read_sheet_variants`, add one case to
  `test_read_sheet_errors`, add `test_parse_mark_accepts` and `test_parse_mark_rejects`.
- `tests/unit/test_spotcheck_cli.py`: in `test_cli_spot_check_errors`, use `no`
  instead of `maybe`.

Then run the quality gate from board.md in the story worktree, push, comment on PR #40
that the v2 commit is done (what changed, gate result), and move the story to In
review. **Stop there.** U1–U5 below are for the user.

### User steps (unchanged from v1; not for the implementer or the reviewer)

These need the key, cost money (about $0.02 for 281 templates) or need a person. They
happen while the PR is in Human review, before it is merged. An agent may help with U2
or U4 only when the user asks in their own message.

- **U1, real tagging run (AC1).** In the main checkout
  `/Users/emny/src/logdash-eriksberg` (it has `.env`): `git fetch origin`, then
  `git checkout --detach origin/feature/semantic-template-tags-33-tag-spot-check`,
  `uv sync`, then `uv run logdash build --only tags`. Expect
  `tagged 281 of 281 templates`. If some are untagged, run
  `uv run logdash build --only tags --force` once.
- **U2, sheet (AC2).** Same checkout: `uv run logdash spot-check sample`. Commit
  `spot-checks/tags-spot-check.csv` and `spot-checks/tags-spot-check.json` with
  `Add tag spot-check sheet (story #33)`, then
  `git push origin HEAD:feature/semantic-template-tags-33-tag-spot-check`.
- **U3, marks (AC3).** A team member fills the four `agree_*` columns (only `agree` or
  `disagree`) and `note` as `spot-checks/README.md` says. Commit
  `Record tag spot-check marks (story #33)`.
- **U4, score (AC3).** `uv run logdash spot-check score`. Commit
  `spot-checks/tags-spot-check-results.md` with `Score tag spot check (story #33)` and
  push. Then merge the PR.
- **U5, record (AC3).** When all stories are Done and the orchestrator opens the
  feature PR to `main`, its body includes the contents of
  `spot-checks/tags-spot-check-results.md` under a heading "Spot check".

## 5. Tests

All in commit 1.

| Test | Change | Proves | AC |
|---|---|---|---|
| `test_read_sheet_variants` (`test_spotcheck.py`) | Change the row-1 marks from `["Agree", " y ", "D", "no"]` to `["Agree", " agree ", "DISAGREE", " disagree "]`. Keep the expected `[True, True, False, False]` and the rest of the test. | case and outer whitespace are ignored for both marks | AC3 |
| `test_read_sheet_errors` (`test_spotcheck.py`) | Add the case `(sheet_text([(1, ["no", "", "", ""], "")]), r":2: agree_actionable: 'no' is not agree or disagree")`. The fixed tag cell `actionable` in `sheet_text` is `yes`, so a `no` there is exactly the ambiguous mark. | `no` is rejected and the error names file line and column | AC3 |
| `test_parse_mark_accepts`, `test_parse_mark_rejects` (new, `test_spotcheck.py`, both parametrised) | `test_parse_mark_accepts`: `("agree", True)`, `("AGREE", True)`, `(" Disagree\t", False)`, `("", None)`, `("   ", None)`, `(None, None)`; asserts `sc.parse_mark(value, "w") == expected`. `test_parse_mark_rejects`: values `"yes"`, `"no"`, `"y"`, `"n"`, `"a"`, `"d"`, `"Yes"`, `"agreed"`, `"x"`; asserts `pytest.raises(ValueError, match=r"^w: .* is not agree or disagree$")` for `sc.parse_mark(value, "w")`. | only the two explicit words count; every former shorthand now fails | AC3 |
| `test_cli_spot_check_errors` (`test_spotcheck_cli.py`) | Replace `rows[0]["agree_noise"] = "maybe"` with `rows[0]["agree_noise"] = "no"`, and the final assertion with `assert "agree_noise: 'no' is not agree or disagree" in capsys.readouterr().err`. Keep `== 2`. | the CLI rejects `no` end to end with exit code 2 and names the column | AC3 |

No other test changes. `test_cli_spot_check_sample_and_score` and `test_score_and_report`
already use only `agree`/`disagree`/empty and must still pass unchanged. Coverage stays
above the 90 % gate (the change removes branches rather than adding them).

**AC coverage.** Unchanged from v1: the tooling for AC2 and AC3 is proven by tests;
AC1, the committed sheet (AC2) and the marks and recorded rate (AC3) are manual checks
in U1–U5.

## 6. Open questions

Each has a default; none blocks the plan.

1. **Should the error message list the allowed words more explicitly** (for example
   `... is not agree or disagree; write exactly agree or disagree`)? *Default: no.*
   The existing text already names both words, and the user asked to keep the existing
   error.
2. **Review notes 1, 3 and 4 on PR #40** (duplicate template texts in `sample`, the
   in-process determinism test, a cross-module test helper). *Default: not addressed
   in v2.* The user's change request covers note 2 only, and the reviewer marked all
   four as non-blocking. A human can ask for them in a later version.

v1's open questions (summary counted as a tag, strata 10/10/10, CSV format, result in a
committed file copied into the feature PR) keep their v1 defaults.

## 7. Changes from previous version

| Finding or decision | How v2 addresses it |
|---|---|
| User decision on PR #40 (comment "Change requested by the user: explicit agree/disagree marks"): only `agree` and `disagree` are accepted, case and surrounding whitespace ignored | Step 1: `MARKS = {"agree": True, "disagree": False}`, `parse_mark` strips and lowercases, then looks up `MARKS` |
| Drop `yes`/`no`/`y`/`n` and the `a`/`d` shorthands | Step 1 removes `AGREE_WORDS`/`DISAGREE_WORDS`; `test_parse_mark_rejects` covers each former shorthand |
| Any other non-empty value fails with the existing clear error naming file, line and column | Error text and `read_sheet`'s `where` are unchanged; new `test_read_sheet_errors` case matches `:2: agree_actionable: 'no' is not agree or disagree` |
| Update `spot-checks/README.md` (line 19) to match | Step 2 rewrites lines 19-21 and labels `yes`/`no` in the table as tag values |
| Update or add tests, including one showing `no` is rejected | Section 5: updated `test_read_sheet_variants` and `test_cli_spot_check_errors`; new error case and `test_parse_mark_*` |
| Review note 2 on PR #40 (yes/no marking ambiguity) | Resolved by the rows above |
