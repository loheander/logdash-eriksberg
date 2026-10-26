### 1. Goal

`adrs/` has unique, consecutive ADR numbers, every ADR citation in code and docs still means the ADR it meant, the two stale skills from the earlier pipeline are gone, and every open issue or PR that claims an ADR number knows the corrected numbers. A small test keeps the numbering honest from now on.

**This plan does not use the numbering the issue suggests** (see Design and open question 1). Since the issue was written, #50 merged `adrs/ADR003-spike-forecasting-lab.md`, and two open PRs add an `ADR004`. Code, ADRs and seven in-flight plans already cite ADR001 (language model), ADR002 (tag filters) and ADR003 (forecasting lab) with today's meaning. So the three ADRs keep their numbers and the SDLC ADR moves to the end.

Acceptance criteria (from the issue):
- [ ] ADR numbers in `adrs/` are unique and consecutive, and each file's heading matches its file name.
- [ ] Every `ADR<nnn>` reference outside `plans/` points to the ADR it meant before the renumbering.
- [ ] `.claude/skills/orchestrate/` and `.claude/skills/plan-story/` are gone, and `git grep` finds no references to them outside `plans/`.
- [ ] Issues whose plans or splits claim ADR numbers have a comment with their corrected number.
- [ ] Quality gate passes.

### 2. Depends on

None. (Open PRs #65 and #66 interact with this one by number only; see Risks.)

### 3. Context

ADRs on `main` (`77b734b`):

| File | Heading (line 1) | Cited by (outside `plans/`) |
|---|---|---|
| `adrs/ADR001-agent-orchestrated-sdlc.md` | `# ADR001: Agent-orchestrated SDLC on the project board` | `README.md:90` (link). Nothing else. |
| `adrs/ADR001-language-model-calls-at-build-time.md` | `# ADR001: Language model calls happen only in explicit builds, memoised by content` | `src/logdash/analyses/base.py:31`, `src/logdash/analyses/tags.py:3`, `src/logdash/app/tag_filters.py:1`, `src/logdash/cli.py:106`, `src/logdash/tagging/__init__.py:1`, `src/logdash/tagging/openrouter.py:65`, `adrs/ADR002-semantic-tag-filters-on-pages.md:11,19` |
| `adrs/ADR002-semantic-tag-filters-on-pages.md` | `# ADR002: Semantic tag filters on dashboard pages` | `src/logdash/analyses/anomalies.py:16,226,241`, `src/logdash/analyses/explorer.py:70`, `src/logdash/app/tag_filters.py:1,56`, `adrs/ADR003-spike-forecasting-lab.md:14` |
| `adrs/ADR003-spike-forecasting-lab.md` | `# ADR003: Spike forecasting lab` | `README.md:65` (link), `src/logdash/analyses/forecast.py:4` |

Every bare `ADR001` outside `plans/` and the two ADR files means the language-model ADR; every `ADR002` means the tag filters; every `ADR003` means the forecasting lab. The only citation of the SDLC ADR is the README link. (Found with `git grep -n -E 'ADR ?0*[0-9]+' -- ':!plans/'`; `tests/` cites no ADR.)

Other places:
- `.claude/sdlc/protocol.md:75-76`: "Architectural decisions that bind other issues go in `adrs/ADR<nnn>-<title>.md` (Status, Context, Decision, Consequences), numbered after the highest existing one."
- `.claude/skills/orchestrate/` (`SKILL.md`, `board.md`, `board.sh`) and `.claude/skills/plan-story/SKILL.md`: the stale skills. Outside those two directories, `git grep -n -E 'plan-story|skills/orchestrate/|board\.(sh|md)|/orchestrate([^-]|$)' -- ':!plans/'` finds nothing today. (`orchestrate-kanban` is the live skill and stays.)
- `tests/unit/`: flat `test_<topic>.py` files; `tests/conftest.py:62` has one autouse fixture (blocks model network calls), harmless for a file-system test. `pyproject.toml`: ruff checks `tests`, mypy checks only `src`, coverage is measured on `logdash` only.

In-flight work that claims or cites ADR numbers (from issue bodies, plan comments and open PRs):

| Where | Claim |
|---|---|
| #23 split comment; bodies of #51, #52, #59, #60 | forecasting contract is `adrs/ADR002-spike-forecasting-lab.md` (it landed as ADR003) |
| #26 split comment; #45 body (acceptance criterion) | #45 writes ADR003 for the labels store (taken) |
| #55 plan; open PR #66 | adds `adrs/ADR004-chat-query-tools.md` |
| #57 plan | cites "ADR004" meaning #55's query-tool ADR |
| #59 plan; open PR #65 | adds `adrs/ADR004-forecast-background-runs.md` (collides with PR #66) |
| #60 plan | adds `adrs/ADR005-forecast-windowing.md`, "next free after #59's ADR004" |
| #25 split comment; #44 plan | #44's ADR "after the highest on main"; amends the build-time ADR (ADR001) |
| #47 plan | `cli.py:106` comment cites ADR001 (language model) |
| #56 plan | cites "ADR002 semantics" (tag filters) throughout |
| open PR #27 (human, dark mode) | adds `adrs/ADR001-central-switches-have-central-code.md` |

### 4. Design

**Numbering: keep ADR001 to ADR003, move the SDLC ADR to the next free number.** After this PR:

| Number | File | Change |
|---|---|---|
| ADR001 | `adrs/ADR001-language-model-calls-at-build-time.md` | none |
| ADR002 | `adrs/ADR002-semantic-tag-filters-on-pages.md` | none |
| ADR003 | `adrs/ADR003-spike-forecasting-lab.md` | none |
| ADR`<N>` | `adrs/ADR<N>-agent-orchestrated-sdlc.md` | was ADR001 |

`<N>` is one more than the highest ADR number on `origin/main` when you start, not counting the SDLC ADR. On `77b734b` that is **ADR004**. If PR #65 or #66 has merged an ADR004 by then, `<N>` is 5 (and so on). Use the same `<N>` everywhere below.

With this numbering every existing citation keeps its meaning, so no line of `src/` changes, the ADR texts that cite each other stay right, the committed `plans/` stay accurate, and the plans of #44, #47, #56, #59 and #60 stay accurate. Only the README link to the SDLC ADR changes.

Rejected:
- *The issue's suggestion, extended* (SDLC ADR001, language model ADR002, tag filters ADR003, forecasting lab ADR004). It was written before ADR003 existed. It now needs a three-way overlapping swap in 14 code and ADR citations, it rewrites the merged ADR003 (whose Context explains its own number), it collides with the ADR004 of PRs #65 and #66, and it silently changes the meaning of the ADR numbers cited in open PRs #65 and #66 and in the plans of #47, #56, #59 and #60. Those would then merge citations that point at the wrong ADR. The issue's reason (the README links the SDLC ADR) costs one line either way.
- *Give the SDLC ADR a number after everything open PRs claim* (for example ADR006): leaves a gap while those PRs are open, so the numbers are not consecutive.

**A guard test.** `tests/unit/test_adrs.py` checks file names, consecutive numbers, headings, that every `adrs/ADR<nnn>-*.md` link and every bare `ADR<nnn>` citation in docs and code names an ADR that exists, and that the stale skills stay gone. It makes the acceptance criteria executable and catches the next parallel-branch collision as soon as a branch is merged with `main` (PRs #65 and #66 will fail it once they merge `main`, which is the point).

**One protocol line** tells agents to take the number from `origin/main` at commit time and to renumber when `main` gains it first, and names the test. Without it the test's failure on a rebased branch would surprise the next implementer.

**Issue comments** are posted after the PR is open, so they can cite the PR. Each gives the full post-merge table plus one issue-specific line. They do not use a protocol heading (`### ADR numbers after #58`), so no agent mistakes them for a plan or a question.

Rejected: editing the bodies of #45, #51, #52, #59, #60 to fix their numbers. The issue asks for comments, and agents and humans already treat bodies as the human's text.

### 5. Steps

Branch `issue-58-renumber-adrs-stale-skills` from `origin/main`.

**Commit 1: plan file.** `plans/<today>-renumber-adrs-stale-skills-v1.md`, this comment verbatim without its heading line.

**Commit 2 (steps 1 to 3): "Move the SDLC ADR to ADR<N> (issue #58)"**

1. Decide `<N>`: `git ls-tree --name-only origin/main adrs/`; take the highest three-digit number among files other than `ADR001-agent-orchestrated-sdlc.md`, add one. Write it down in the PR body.
2. `git mv adrs/ADR001-agent-orchestrated-sdlc.md adrs/ADR<N>-agent-orchestrated-sdlc.md`. In the moved file:
   - Line 1 becomes `# ADR<N>: Agent-orchestrated SDLC on the project board`.
   - Under `## Status`, keep `proposed` and add a second paragraph, exactly:
     `Numbered ADR001 until #58. That number belongs to ADR001-language-model-calls-at-build-time.md, which code cites as ADR001.`
   - Change nothing else.
3. `README.md:90`: `[ADR001](adrs/ADR001-agent-orchestrated-sdlc.md)` becomes `[ADR<N>](adrs/ADR<N>-agent-orchestrated-sdlc.md)`. No other README change.

Do not touch `src/`, the other three ADRs, or any file under `plans/`.

**Commit 3 (step 4): "Remove the stale orchestrate and plan-story skills (issue #58)"**

4. `git rm -r .claude/skills/orchestrate .claude/skills/plan-story`. Then run
   `git grep -n -E 'plan-story|skills/orchestrate/|board\.(sh|md)|/orchestrate([^-]|$)' -- ':!plans/'`
   and confirm it prints nothing (exit 1). If it prints a hit, the hit is a reference to a deleted skill: remove or reword that sentence so it points at the live equivalent (`orchestrate-kanban`, `plan-issue`, `.claude/sdlc/board.py`), and list it in the PR body.

**Commit 4 (steps 5 to 6): "Guard ADR numbering with a test (issue #58)"**

5. New `tests/unit/test_adrs.py`:

   ```python
   """ADR files: unique, consecutive numbers, matching headings, citations that resolve (#58)."""

   import re
   from pathlib import Path

   import pytest

   ROOT = Path(__file__).resolve().parents[2]
   ADR_DIR = ROOT / "adrs"
   FILE_NAME = re.compile(r"ADR(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
   LINK = re.compile(r"adrs/(ADR\d{3}-[a-z0-9-]+\.md)")
   CITATION = re.compile(r"\bADR(\d{3})\b")
   STALE_SKILLS = ("orchestrate", "plan-story")
   STALE_REFERENCE = re.compile(r"plan-story|skills/orchestrate/|board\.(?:sh|md)")


   def adr_files() -> list[Path]:
       return sorted(ADR_DIR.glob("*.md"))


   def citing_files() -> list[Path]:
       """Docs and code that may cite ADRs. ``plans/`` is a historical record and is skipped,
       and so is ``.claude/worktrees/`` (other branches' checkouts)."""
       files = [ROOT / "README.md", ROOT / "CLAUDE.md", *adr_files()]
       files += sorted((ROOT / "src").rglob("*.py"))
       for sub in ("agents", "skills", "sdlc"):
           files += sorted((ROOT / ".claude" / sub).rglob("*.md"))
       return [path for path in files if path.is_file()]


   def number(path: Path) -> str:
       match = FILE_NAME.fullmatch(path.name)
       assert match, f"{path.name} is not named ADR<nnn>-<kebab-title>.md"
       return match.group(1)


   def test_adr_file_names_follow_the_convention() -> None:
       assert adr_files()
       for path in adr_files():
           number(path)


   def test_adr_numbers_are_unique_and_consecutive() -> None:
       numbers = sorted(int(number(path)) for path in adr_files())
       assert numbers == list(range(1, len(numbers) + 1))


   @pytest.mark.parametrize("path", adr_files(), ids=lambda path: path.name)
   def test_adr_heading_matches_file_name(path: Path) -> None:
       first = path.read_text(encoding="utf-8").splitlines()[0]
       assert first.startswith(f"# ADR{number(path)}: ")


   def test_adr_links_point_to_existing_files() -> None:
       missing = [
           f"{path.relative_to(ROOT)}: {name}"
           for path in citing_files()
           for name in LINK.findall(path.read_text(encoding="utf-8"))
           if not (ADR_DIR / name).is_file()
       ]
       assert missing == []


   def test_adr_citations_name_existing_adrs() -> None:
       existing = {number(path) for path in adr_files()}
       unknown = [
           f"{path.relative_to(ROOT)}: ADR{cited}"
           for path in citing_files()
           for cited in CITATION.findall(path.read_text(encoding="utf-8"))
           if cited not in existing
       ]
       assert unknown == []


   def test_stale_pipeline_skills_are_gone() -> None:
       for name in STALE_SKILLS:
           assert not (ROOT / ".claude" / "skills" / name).exists()
       stale = [
           str(path.relative_to(ROOT))
           for path in citing_files()
           if STALE_REFERENCE.search(path.read_text(encoding="utf-8"))
       ]
       assert stale == []
   ```

   Keep it as written unless ruff or the run asks otherwise. (The planner ran it: with steps 1 to 4 applied for `<N>` = 4, all 9 cases pass and `ruff check` and `ruff format --check` are clean; on `main` it fails in `test_adr_numbers_are_unique_and_consecutive` and `test_stale_pipeline_skills_are_gone`.) It reads only files in the checkout, needs no fixture and no network, and adds nothing to `src/` coverage.
6. `.claude/sdlc/protocol.md:75-76`: replace "numbered after the highest existing one." with
   "numbered one after the highest ADR on `origin/main` when you commit it. If `main` gains that number before your PR merges, renumber yours and its citations; `tests/unit/test_adrs.py` fails on duplicate numbers, gaps and citations of missing ADRs."
   Change nothing else in the protocol.

**After the PR is open (step 7): comments on issues and on PR #27.** No commit.

7. For each target below, write a file in your scratchpad and post it with `gh issue comment <n> --body-file <file>` (for PR #27, `gh pr comment 27 --body-file <file>`). Body, with `<pr>` your PR number, `<N>` from step 1 and `<N+1>` one more:

   ```markdown
   ### ADR numbers after #58

   PR #<pr> (issue #58) removes the duplicate ADR001 on `main`. When it merges, the ADRs are:

   | ADR | File | Change |
   |---|---|---|
   | ADR001 | `adrs/ADR001-language-model-calls-at-build-time.md` | none |
   | ADR002 | `adrs/ADR002-semantic-tag-filters-on-pages.md` | none |
   | ADR003 | `adrs/ADR003-spike-forecasting-lab.md` | none |
   | ADR<N> | `adrs/ADR<N>-agent-orchestrated-sdlc.md` | was ADR001 |

   Existing citations of ADR001, ADR002 and ADR003 keep their meaning. A new ADR takes one more than the highest number on `main` when it is committed (ADR<N+1> once PR #<pr> merges). If `main` gains that number first, renumber yours; `tests/unit/test_adrs.py` fails on duplicates and gaps.

   For this issue: <line>
   ```

   If `<N>` is above 4, add a row for each ADR between ADR003 and ADR`<N>` (file name, change "none") so the table lists every file in `adrs/`.

   Targets and their `<line>`:

   | Target | `<line>` |
   |---|---|
   | #23 | The split comment calls the forecasting contract `adrs/ADR002-spike-forecasting-lab.md`. It is `adrs/ADR003-spike-forecasting-lab.md`. |
   | #51 | The body calls the forecasting contract `adrs/ADR002-spike-forecasting-lab.md`. It is `adrs/ADR003-spike-forecasting-lab.md`. |
   | #52 | The body calls the forecasting contract `adrs/ADR002-spike-forecasting-lab.md`. It is `adrs/ADR003-spike-forecasting-lab.md`. |
   | #59 | The body calls the forecasting contract ADR002; it is ADR003, as the plan already says. PR #65 adds `adrs/ADR004-forecast-background-runs.md`, and PR #66 adds an ADR004 too. Whichever merges after another ADR004 is on `main` renames its file to the next free number and updates its citations (README link, docstrings, test messages). |
   | #60 | The body calls the forecasting contract ADR002; it is ADR003. The plan's `adrs/ADR005-forecast-windowing.md` assumes #59's ADR is ADR004. Number the windowing ADR one after the highest on `main` when you commit, and cite #59's ADR by the number it has on `main` then. |
   | #55 | PR #66 adds `adrs/ADR004-chat-query-tools.md`, and PR #65 adds an ADR004 too. Whichever merges after another ADR004 is on `main` renames its file to the next free number and updates its citations. |
   | #57 | The plan cites ADR004 for #55's query-tool ADR. Use the number that ADR has on `main` when you implement (`ls adrs/*chat-query-tools*`). |
   | #26 | The split comment says #45 writes ADR003. ADR003 is the forecasting lab; #45's ADR takes the next free number on `main` when it is committed. |
   | #45 | The acceptance criterion names ADR003. ADR003 is the forecasting lab; this issue's ADR takes the next free number on `main` when it is committed. |
   | #25 | The split comment's note that "PR #41 adds another ADR001 and an ADR002" is resolved: the build-time ADR that #44 amends is `adrs/ADR001-language-model-calls-at-build-time.md` and keeps ADR001. |
   | #44 | The build-time ADR this plan amends is `adrs/ADR001-language-model-calls-at-build-time.md` and keeps ADR001. The new chat ADR takes the next free number on `main` when it is committed. |
   | PR #27 | This PR adds `adrs/ADR001-central-switches-have-central-code.md`, but ADR001 is taken on `main`. Rename it to the next free number before merging. |

   Skip a target that is closed by the time you post. Do not comment on #47, #53, #54 or #56: their plans already cite the numbers correctly.

PR body: `Closes #58`; a section "Numbering differs from the issue" with the Design rationale in three sentences and the value of `<N>`; the list of comments posted (target and link); and this merge note: "Before merging: merge `origin/main` into the branch and rerun the gate. If an ADR`<N>` landed on `main` meanwhile, `test_adr_numbers_are_unique_and_consecutive` fails; then move the SDLC ADR to the next free number (file, heading, README link)."

### 6. Tests

| Test | Proves | Criterion |
|---|---|---|
| `test_adr_file_names_follow_the_convention` | every ADR file is `ADR<nnn>-<kebab>.md` | 1 |
| `test_adr_numbers_are_unique_and_consecutive` | numbers are 1..n without duplicates or gaps; fails on today's `main` (two ADR001) | 1 |
| `test_adr_heading_matches_file_name` (one case per file) | each heading starts `# ADR<nnn>: ` with the file's number | 1 |
| `test_adr_links_point_to_existing_files` | the README links (lines 65 and 90) and `forecast.py:4` point at real files after the rename | 2 |
| `test_adr_citations_name_existing_adrs` | no citation names a missing ADR | 2 (partly) |
| `test_stale_pipeline_skills_are_gone` | both directories are gone and no doc, skill, agent or protocol file mentions them | 3 |

Manual checks:
- Criterion 2 (meaning, not just existence): `git grep -n -E 'ADR ?0*[0-9]+' -- ':!plans/'` and compare with the Context table. Only the README line 90 link and the moved ADR's own heading and status line differ from `main`. The reviewer repeats this.
- Criterion 3: the `git grep` of step 4 prints nothing.
- Criterion 4: the reviewer opens each target in step 7 and finds the comment.
- Criterion 5: the quality gate. No latency-sensitive code changes, so no `-m slow` run.

Before committing the test, check it fails on `main` (run it after commit 1 or with the rename stashed by a WIP commit): `test_adr_numbers_are_unique_and_consecutive` must fail there. Mention it in the PR body.

### 7. Risks

- **Open PRs #65 and #66 both add ADR004, and either may merge before this PR.** Step 1 computes `<N>` from `origin/main` at start, and the PR's merge note asks the human to merge `main` and rerun the gate, where the new test catches a duplicate. The comments on #55 and #59 tell those PRs to renumber.
- **The new test breaks other branches once they merge `main`.** Intended: a branch with a duplicate number or a dangling citation is exactly what the test exists to catch. The comments on #55, #59 and PR #27 say so in advance.
- **`.claude/worktrees/` in the main checkout.** `citing_files` reads only `.claude/agents`, `.claude/skills` and `.claude/sdlc`, never the worktrees directory, so the test does not read other branches' files or get slow.
- **A bare `ADR<nnn>` in a skill or protocol that is a placeholder.** The citation regex requires three digits, so `ADR<nnn>` and `ADRXXX` (`CLAUDE.md:2`) are not matched.
- **Swapping references wrongly.** The design swaps none: no line of `src/` and none of the other three ADRs change. The reviewer verifies with the manual `git grep`.
- **Deleting a skill that something still loads.** Nothing outside the two directories references them (step 4's grep), and `.claude/agents/orchestrator.md:10` runs `/orchestrate-kanban`, which stays.
- **Committed `plans/` cite the old numbers.** They cite ADR001 and ADR002 with the meaning those keep, and the test skips `plans/` anyway.

### 8. Docs

- `README.md:90` link (step 3).
- `adrs/ADR<N>-agent-orchestrated-sdlc.md`: renamed, heading and one status paragraph (step 2). No new ADR: the numbering rule is protocol, not architecture.
- `.claude/sdlc/protocol.md`: one sentence (step 6).

### 9. Open questions (non-blocking, defaults used)

1. **Numbering differs from the issue's suggestion.** Default: ADR001 to ADR003 keep their numbers and the SDLC ADR becomes ADR004 (or the next free number), for the reasons in Design. If a human prefers the issue's order (SDLC ADR001, language model ADR002, tag filters ADR003, forecasting lab ADR004), set `needs-replan`; that version must also rewrite citations in open PRs #65 and #66 and comment on #47, #56, #59 and #60.
2. **Protocol sentence (step 6).** Default: added, because the new test fails on rebased branches and agents should know why. Drop it in review if the protocol should only change through a retrospective; the test stands alone.
3. **Commenting on PR #27**, a human's PR outside the pipeline. Default: one comment, because merging it as is would bring a duplicate ADR001 back.
