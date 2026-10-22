# Board protocol

Shared rules for the orchestrator and every sub-agent (planner, implementer, reviewer).
Read this file in full before touching the board, an issue, a branch or a PR.

## The board is the queue

GitHub Project "Logdash @ Eriksberg" (`loheander` #1), repo `loheander/logdash-eriksberg`.
The **Status** field is the queue. A story moves only forward through this table,
except for the two "back" transitions listed.

| Status | Meaning | Who picks it up | Moves it to |
|---|---|---|---|
| Backlog | Story exists, no approved plan | planner | Ready |
| Ready | Plan committed on the story branch | implementer | In progress |
| In progress | Implementer is working | (implementer) | In review |
| In review | Implementation committed and pushed | reviewer | Human review, or back to Backlog |
| Human review | Agent review passed, PR waits for a human | human merges; orchestrator notices | Done |
| Done | PR merged into the feature branch, story closed | – | – |

Back transitions:
- **Reviewer rejects** → Backlog, label `needs-replan`, findings in a PR comment.
- **Implementer finds the plan unworkable** → Backlog, label `needs-replan`, reason in a PR comment.

Only the agent named in "Who picks it up" changes a story's status, and only to the
next status listed. Humans may move anything.

## Labels

| Label | Meaning |
|---|---|
| `story` | Sub-story created by the orchestrator from a parent issue |
| `needs-replan` | Back in Backlog after rejection; the planner must write a new plan version |
| `agent-busy` | A planner or reviewer is working on it; nobody else starts it |

## Claiming

The orchestrator dispatches each sub-agent with an explicit story number. Even so,
every agent first re-reads the story's status and labels and **stops without changes**
if the status is not the one it expects or `agent-busy` is set. Planner and reviewer
add `agent-busy` when they start and remove it when they hand off. For the
implementer, moving to In progress is the claim.

## Stories

A story is a GitHub issue with label `story`, linked as a **sub-issue** of its parent
issue, added to the board. Body template:

```markdown
Parent: #<parent>
Depends on: #<story>, … (or "none")

## Goal
<one or two sentences: the user-visible outcome>

## Acceptance criteria
- [ ] <observable, testable statement>

## Out of scope
- <what a later story does instead>
```

A story should be implementable in one PR that a human can review in about 30 minutes.

## Branches, plans and PRs

Slugs are kebab-case and at most 5 words.

- Each parent issue has a **feature branch** from `main`: `feature/<feature-slug>`.
  The orchestrator creates it when it splits the issue.
- Each story has a branch from the feature branch:
  `feature/<feature-slug>-<story#>-<story-slug>`. Its PR targets the **feature branch**,
  not `main`.
- When all stories are Done, the orchestrator opens a PR from the feature branch to
  `main` for human review. No PR is merged without review.
- Plans live at `plans/yyyy-mm-dd-<story-slug>-vv.md` (vv = 01, 02, …; a replan adds a
  new version, never edits an old one). Conventions: the `plan-story` skill.
- Architectural decisions: `adr/ADRXXX-title.md`.
- The planner opens a **draft PR** for the story branch, with body `Story #<story>`.
  (`Closes` only fires on merges to `main`, so the orchestrator closes merged stories.)
  All discussion about the story (rejections, review findings) goes on that PR.
- Every iteration is a commit: plan versions, implementation steps and fixes are
  separate commits.
- Commit messages: imperative subject with `(story #<n>)`, then the attribution lines
  from the session's system instructions.
- PR bodies end with the attribution line from the session's system instructions.

## Quality gate

Run in the story's worktree before handing off code:

```bash
uv sync
LOGDASH_THREADS=2 uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

All must pass (coverage gate 90 %). Never weaken a test or the gate to make it pass.

## Commands

Use `board.sh` (same folder) for the board. For issues, always use `--json`:
plain `gh issue view` prints nothing in this environment.

```bash
B=.claude/skills/orchestrate/board.sh
$B list Ready                     # stories in a status
$B status <issue#> "In review"    # move a story
$B add <issue#> Backlog           # put an issue on the board
gh issue view <n> --json number,title,body,labels,comments
gh issue edit <n> --add-label agent-busy / --remove-label agent-busy
gh pr comment <pr> --body-file <file>
```

## Machine limits

4 logical CPUs, 16 GB, and `logdash serve` runs alongside. At most **two** sub-agents
run at once, and tests run with `LOGDASH_THREADS=2`.
