---
name: review-issue
description: Review the PR of one In review issue on the logdash board. Look systematically for ways the code can go wrong, post numbered findings for the implementer, verify the fixes, then use the Jev human-review gate to either merge and move the issue to Done or move it to Human review. Use when asked to review an issue, ticket or pipeline PR, or when the reviewer agent picks up an item.
argument-hint: <issue number>
---

# Review an issue

Input: one issue number. You judge and decide; you don't fix code. Read
`.claude/sdlc/protocol.md` first; its rules bind you. You run in your own worktree:
run git and `gh` commands one at a time and pass multi-line text with `--body-file`.

## 1. Claim and decide which pass this is

1. `python3 -I .claude/sdlc/board.py show <n>`. Stop and report if the status is not
   In review, or if `agent-busy`, `changes-requested` or `needs-human` is set.
2. `gh issue edit <n> --add-label agent-busy`.
3. Find the open PR (the `prs` entry from `board.py show`); if there is none, remove
   `agent-busy`, report it and stop. Read it:
   `gh pr view <pr> --json number,title,body,headRefName,mergeable,comments,commits,files`.
4. Pick the pass from the PR comments:
   - no `## Review round` comment yet → **full review**, round 1;
   - the newest is `## Fixes for review round <r>` → **verification**, round r + 1;
   - anything else (for example a review round without a fix reply) → stop, remove
     `agent-busy`, and report the inconsistency.

## 2. Gather the evidence

1. Read the issue and its newest `## Implementation plan` comment: the acceptance
   criteria and the plan's **Risks** section are your checklist.
2. `gh pr checkout <pr>`, then `uv sync`, then run the full quality gate from the
   protocol yourself. Don't trust the PR body's numbers. If the plan's Risks mention
   latency, run the slow benchmark against the real cache too.
3. Read the whole diff (`git diff origin/main...HEAD`), and read enough of the
   surrounding code to judge each change in context, including callers of changed
   functions.

## 3. Look for ways it can go wrong

Go through every lens. Each one is a different way logdash code has failed or could
fail. Note "nothing found" for a lens rather than skipping it.

1. **Plan and acceptance criteria**: each criterion met and tested; plan steps done;
   deviations listed and justified; no unplanned scope.
2. **Correctness**: wrong logic, off-by-one in windows or bins, joins that drop or
   duplicate rows, wrong aggregation, UTC handling, NaN/None/empty input, dtype
   surprises between Polars, DuckDB and pandas, division by zero, ordering assumptions.
3. **Data and cache**: a precompute stage whose fingerprint or dependencies miss a
   new input, schema changes without a rebuild, the app reading `data/` (it must only
   read `cache/`), behaviour when a cache stage is missing or stale.
4. **Performance**: work in a callback that belongs in the precompute, full scans of
   38.8 M rows at request time, memory blow-ups, anything that threatens the 300 ms
   budget or the DuckDB memory limit.
5. **UI**: light and dark theme (theme tokens, not hard-coded colours), 162 hosts and
   40 services at real scale, empty filter results, duplicate component ids, callback
   exceptions, unreadable labels.
6. **Tests**: missing tests for new behaviour, assertions too weak to fail on broken
   code, tests that only exercise mocks, coverage reached without asserting anything,
   slow or flaky tests.
7. **Robustness and security**: user input reaching SQL strings unparameterised,
   external calls (OpenRouter/Jev) without timeouts or failure handling, secrets in code
   or logs, unpinned new dependencies.
8. **Maintainability**: duplication of existing helpers, dead code, needless
   abstraction, naming and comments that don't match the surrounding code, README or
   config table not updated.

In verification passes, check every finding of the previous round against the fix
commits and the reply, then look at the new commits through the same lenses. Fixes
introduce bugs too.

## 4. Write the findings

Each finding needs a concrete failure scenario (input or state → wrong result) or a
`path:line` reference, and a suggested fix. No vague advice. Severity:
- **blocker**: wrong results, crash, gate failure, unmet acceptance criterion,
  security problem, budget broken.
- **should**: a real defect or a missing test in a plausible case, but limited damage.
- **nit**: style, naming, small clarity improvements.

Post them with `gh pr comment <pr> --body-file <file>`:

```markdown
## Review round <r>

Gate: <tests passed, coverage, ruff, mypy, benchmark> on <short sha>

| Id | Severity | Where | What goes wrong | Suggested fix |
|---|---|---|---|---|
| R1 | blocker | `src/logdash/x.py:42` | <scenario> | <fix> |

Lenses with nothing found: <list>
Previous round: R1 fixed, R2 accepted reply, R3 still open (verification passes only)
```

Number findings across rounds (round 2 continues after the last id of round 1), so
that ids stay unique on the PR.

## 5. Hand back or decide

- **Blocker or should findings open, round < 3**: post the round, then
  `gh issue edit <n> --add-label changes-requested --remove-label agent-busy`. Stop;
  the orchestrator sends the implementer. The status stays In review.
- **Approach wrong** (fixing it means a different design, not a patch): post the round
  explaining why, comment `## Replan needed` on the issue, add `needs-replan`, move the
  issue to Backlog, remove `agent-busy`, and stop.
- **Blocker or should findings still open after round 3**: go to the decision, forced
  to Human review, with the open findings listed.
- **Nothing open but nits**: go to the decision. Open nits are listed, not fixed.

Post the round comment even when it has no findings, so the record is complete.

## 6. Decision

1. Check that the PR is still mergeable (`mergeable` is `MERGEABLE`) and the gate is
   green on the head commit. If it conflicts, that is a blocker finding: post it as a
   new round and hand back.
2. Write a short review summary to a file: what the PR does, the lenses that found
   something, what was fixed, what remains.
3. Run the gate: `python3 -I .claude/sdlc/human_review_gate.py <pr> --review-file <file>`.
   It returns `decision` (`merge` or `human`), `risk`, `reasons` and the Jev answers.
   It fails closed: if Jev fails, the decision is `human`. Never override a `human`
   decision. You may escalate a `merge` decision to `human` if your review left a doubt
   Jev could not see; say so in the reasons.
4. `python3 -I .claude/sdlc/board.py set <n> Risk <risk>`.
5. Post `## Review decision` on the PR: the decision, the reasons, the Jev values
   (impact, blast radius, P(needs human) with confidence), the review rounds it took,
   and any open nits or findings.
6. `gh pr edit <pr> --add-label retro-pending`.
7. Then:
   - **merge**:
     `gh pr merge <pr> --squash --delete-branch --subject "<PR title> (#<pr>)"`.
     Confirm it merged (`gh pr view <pr> --json state`). Close the issue if `Closes`
     did not (`gh issue close <n> --reason completed`) and move it to Done.
   - **human**: `gh pr ready <pr>` if it is a draft, then move the issue to
     Human review. Don't merge.
8. `gh issue edit <n> --remove-label agent-busy`.
9. Report the decision, the reasons and the PR URL.
