---
name: implement-issue
description: Implement one Ready issue on the logdash board from its plan comment, open a PR and move the issue to In review; or, in fix mode, address the open review findings on its PR. Use when asked to implement an issue or ticket, fix review comments, or when the implementer agent picks up an item.
argument-hint: <issue number> [fix]
---

# Implement an issue

Input: one issue number, and the word `fix` for fix mode. The plan is your
specification. Read `.claude/sdlc/protocol.md` first; its rules bind you.

You run in your own git worktree. Run git and `gh` commands one at a time, and pass
multi-line text with `--body-file`.

## Implement mode

### 1. Claim and set up

1. `python3 -I .claude/sdlc/board.py show <n>`. Stop and report if the status is not
   Ready, if `agent-busy` or `needs-human` is set, or if an issue under "Depends on" in
   the plan is not Done.
2. `gh issue edit <n> --add-label agent-busy`, then
   `python3 -I .claude/sdlc/board.py status <n> "In progress"`.
3. Read the issue and its newest `## Implementation plan v<k>` comment in full.
4. `git fetch origin`, then `git switch -c issue-<n>-<slug> origin/main`. If the branch
   already exists on origin (an earlier attempt), switch to it and merge `origin/main`.
5. `uv sync`.

### 2. Build

1. First commit: the plan comment, verbatim (without the heading line), as
   `plans/<today yyyy-mm-dd>-<slug>-v<k>.md`, plus any ADR the plan asks for.
2. Follow the plan's steps and commit grouping. Write the tests the plan lists, with
   the code they test. Match the surrounding code: naming, comment density, type hints,
   the `LOGDASH_*` config pattern, theme tokens instead of hard-coded colours.
3. Don't add scope. If you notice something worth doing outside the plan, mention it
   in the PR body under "Follow-ups"; don't do it.
4. Small deviations (a renamed helper, a moved constant) are fine: list each one with
   its reason in the PR body. A deviation that changes the design is not yours to
   make: see "When the plan is wrong".
5. Run the quality gate from the protocol until it passes. If the plan's Risks mention
   latency, run the slow benchmark too. Never weaken a test or the gate.
6. If the issue changes what users see, update `README.md` as the plan says.

### 3. Open the PR

1. `git push -u origin issue-<n>-<slug>`
2. Write the body to a file and run
   `gh pr create --base main --title "<issue title> (issue #<n>)" --body-file <file>`.
   Body:
   ```markdown
   Closes #<n>

   ## Changes
   - <file or module>: <what changed and why>

   ## Deviations from the plan
   - <deviation and reason, or "none">

   ## Checks
   <gate commands and results: tests passed, coverage, ruff, mypy, benchmark if run>

   ## Follow-ups
   - <out-of-scope ideas, or "none">

   <attribution line>
   ```
3. `python3 -I .claude/sdlc/board.py status <n> "In review"`, then
   `gh issue edit <n> --remove-label agent-busy`.
4. Report the PR URL and the gate results.

## Fix mode

The reviewer has posted `## Review round <r>` on the PR and set `changes-requested`.

1. `board.py show <n>`: stop unless the status is In review with `changes-requested`
   and without `agent-busy`. Add `agent-busy`.
2. Read the newest `## Review round <r>` comment on the PR (`gh pr view <pr> --json comments`).
   Switch to the PR branch (`git fetch origin`, `git switch issue-<n>-<slug>`), merge
   `origin/main` if the PR is behind or conflicting, and `uv sync`.
3. Handle every finding by its id (R1, R2, …):
   - **blocker** and **should**: fix it, with a test that would have caught it where one
     is possible. One commit per finding or per tight group, subject
     `Fix review R<i>: <what> (issue #<n>)`.
   - **nit**: fix it if it is cheap and safe; otherwise say why not.
   - If you disagree with a finding, don't fix it: explain why in your reply, with
     evidence (a test, a measurement, a file reference). The reviewer decides.
4. Run the full gate. Push (never force-push).
5. Comment `## Fixes for review round <r>` on the PR: one line per finding,
   `R<i>: fixed in <sha>`, `R<i>: not fixed: <reason>`.
6. `gh issue edit <n> --remove-label changes-requested --remove-label agent-busy`.
   The status stays In review; the reviewer verifies next.

## When the plan is wrong

If the plan cannot be followed without a design decision of your own (an API that
does not exist, a step that contradicts another, a performance budget that cannot be
met), stop. Commit nothing half-done. Comment `## Replan needed` on the issue with what
you found and the evidence, add `needs-replan`, move the issue to Backlog, and remove
`agent-busy`. Never hand off to review with a failing gate.
