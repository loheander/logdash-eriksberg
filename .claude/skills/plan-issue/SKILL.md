---
name: plan-issue
description: Plan one Backlog issue on the logdash board. Study the code, write an implementation plan as a comment on the issue, and move the issue to Ready. Split it into stories if it is too large for one PR. Use when asked to plan an issue or ticket, or when the planner agent picks up a Backlog item.
argument-hint: <issue number>
---

# Plan an issue

Input: one issue number. You write the plan; you do not write production code or
open branches. Read `.claude/sdlc/protocol.md` first; its rules bind you.

## 1. Claim

1. `python3 -I .claude/sdlc/board.py show <n>`. Stop and report if the status is not
   Backlog, or if `agent-busy`, `needs-human` or `epic` is set.
2. `gh issue edit <n> --add-label agent-busy`.

## 2. Understand

1. Read the issue with its comments: `gh issue view <n> --json number,title,body,labels,comments`.
   If it has a parent, read the parent too.
2. If `needs-replan` is set, the newest `## Replan needed` comment on the issue, and the
   review comments on the linked PR if one exists, are your main input. The new plan
   version must answer every point in them.
3. Plan against the current `main`: `git fetch origin`, then
   `git switch --detach origin/main` in your worktree.
4. Read the code the issue touches, deeply enough that the implementer never has to
   make a design decision. Use `README.md`, `adrs/`, earlier plans in `plans/`, and the
   tests near the code. Note the conventions you find (config via `LOGDASH_*` env vars
   in `config.py`, precompute stages writing to `cache/`, the 300 ms latency budget for
   callbacks, light and dark themes).

## 3. Decide the size

If the issue has no Size, get one from Jev first:
`python3 -I .claude/sdlc/issue_triage.py <n>` (without `--apply` it writes nothing) and
use its `proposed.Size`. If it fails or lists Size in `unsure`, judge the size yourself.

Split instead of planning whole if any of these hold:
- the issue is Size L or XL and has parts that can ship separately,
- the plan would touch more than about 3 modules or exceed about 400 lines,
- the issue itself says splitting is fine and the parts are independent.

To split:
1. Write 2 to 6 stories. Each one is a thin vertical slice that leaves `main` working
   and is reviewable in about 30 minutes. Their dependencies form a DAG.
2. For each story, write its body to a file and create it:
   ```bash
   gh issue create --label story --title "<parent short title>: <story title>" --body-file <file>
   ```
   Body template:
   ```markdown
   Parent: #<parent>
   Depends on: #<story>, … (or "none")

   ## Goal
   <one or two sentences: the user-visible outcome>

   ## Acceptance criteria
   - [ ] <observable, testable statement>

   ## Out of scope
   - <what another story does instead>
   ```
   Fill in "Depends on" with the real numbers once earlier stories exist.
3. Link each one as a sub-issue and put it on the board, copying the parent's Priority:
   ```bash
   gh api repos/loheander/logdash-eriksberg/issues/<parent>/sub_issues -F sub_issue_id=$(gh api repos/loheander/logdash-eriksberg/issues/<story> --jq .id)
   python3 -I .claude/sdlc/board.py add <story> Backlog
   python3 -I .claude/sdlc/board.py set <story> Priority <P>
   ```
4. Comment `## Split into stories` on the parent with a table of the stories and their
   dependencies, add the `epic` label, remove `agent-busy`, and stop. The parent stays
   in Backlog; the orchestrator moves it to Done when every story is Done. Each story
   gets its own planner run later.

## 4. Write the plan

Write the plan to a file in your scratchpad, then post it with
`gh issue comment <n> --body-file <file>`. Its first line is exactly
`## Implementation plan v<n>`, where `<n>` is one more than the newest plan comment on
the issue (v1 if none). Never edit an earlier plan comment.

Sections:

1. **Goal**: the outcome in one or two sentences, and the acceptance criteria as a
   checklist (copied from the issue, or written by you if the issue has none).
2. **Depends on**: issue numbers that must be Done first, or "none".
3. **Context**: the existing code involved, as `path:line` references, and what each does.
4. **Design**: the approach, and the alternatives you rejected with the reason.
5. **Steps**: ordered and grouped into commits ("Commit 1: steps 1–3"). Each step names
   the files it touches and what changes. Give signatures, constants, config variable
   names and defaults, exact user-facing text, and error handling.
6. **Tests**: each test to add or change, what it proves, and which acceptance
   criterion it covers. Every criterion is covered by a test or a stated manual check.
7. **Risks**: what could go wrong (performance, data correctness, edge cases such as
   empty filters, a missing cache stage, NaN, UTC handling, 162 hosts), and how the plan
   guards against each. The reviewer checks these.
8. **Docs**: README or ADR changes. Write an ADR (`adrs/ADR<nnn>-<title>.md`) only for
   a decision that binds other issues; the implementer commits it.
9. **Open questions**: each with the default this plan uses.
10. **Changes from v<n-1>** (replans only): each finding and how this version answers it.

Open questions:
- **Non-blocking** (a sensible default exists): pick the default, list it, carry on.
- **Blocking** (no sensible default, or the answer changes scope or product direction):
  do not post a plan. Comment `## Question for a human` on the issue, add `needs-human`,
  remove `agent-busy`, and stop.

## 5. Hand off

1. Classify the issue with Jev, now that the plan comment is posted:
   `python3 -I .claude/sdlc/issue_triage.py <n> --apply`. It reads the issue and the
   newest plan and fills only what is empty, by these rules:
   - **Size** (XS to XL) from the plan, on the same scale as the split rule in step 3.
   - **Priority** (P0 to P2): a story copies its parent's; otherwise Jev ranks it
     against the open backlog.
   - **Risk** (low, medium, high) from the human-review gate's `blast_radius` question
     and thresholds, so planning and review grade risk the same way. The reviewer's
     gate overwrites it with the PR's risk.
   - **Type label** (`bug`, `enhancement` or `documentation`), only if the issue has
     none of them.

   Never overwrite a value a human set, and never edit the rules or thresholds in the
   script; that is the retrospective's job. If the script fails (exit 1), or lists a
   field in `unsure`, decide that field yourself: set Size from the plan
   (`python3 -I .claude/sdlc/board.py set <n> Size <XS|S|M|L|XL>`) and leave Priority,
   Risk and the label empty for a human.
2. `gh issue edit <n> --remove-label needs-replan --remove-label agent-busy`
3. `python3 -I .claude/sdlc/board.py status <n> Ready`
4. Report: the plan comment URL, the dependencies, any open questions, and the triage
   result (each field applied or skipped, with Jev's value and confidence).
