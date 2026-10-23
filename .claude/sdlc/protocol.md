# SDLC protocol

Shared rules for the orchestrator and every agent (planner, implementer, reviewer,
retrospective). Read this file in full before you touch the board, an issue, a branch
or a PR. Skills say *how* to do one job; this file says what everyone may and may not do.

## The board is the queue

GitHub Project "Logdash @ Eriksberg" (`loheander` #1), repo `loheander/logdash-eriksberg`.
The **Status** field is the state machine. All other state lives in labels and in
issue and PR comments, so any agent can pick up where another stopped.

| Status | Meaning | Picked up by | Moves to |
|---|---|---|---|
| Backlog | No plan yet | planner | Ready |
| Ready | Plan comment on the issue | implementer | In progress |
| In progress | Implementer is working | (implementer) | In review |
| In review | PR open, gate green | reviewer, and implementer when `changes-requested` is set | Human review, or Done |
| Human review | Agent review done, PR waits for a human | human merges; orchestrator notices | Done |
| Done | PR merged, issue closed | – | – |

Back transitions:
- **Implementer finds the plan unworkable** → Backlog with `needs-replan`, reason on the issue.
- **Reviewer finds the approach wrong** (not fixable inside the plan) → Backlog with `needs-replan`.

Only the agent named in "Picked up by" changes a status, and only along this table.
Humans may move anything, and an agent never moves an item a human has moved back.

## Labels

| Label | Meaning | Set by | Cleared by |
|---|---|---|---|
| `agent-busy` | An agent is working on this issue; nobody else starts it | any agent at start | the same agent at hand-off |
| `needs-replan` | Back in Backlog; the planner writes the next plan version | implementer, reviewer, human | planner |
| `needs-human` | Blocked on a question only a human can answer; agents skip it | any agent | human |
| `changes-requested` | Review findings are open; implementer must fix them | reviewer | implementer |
| `epic` | Split into sub-issues; never implemented itself | planner | – |
| `story` | Sub-issue created by the planner from an epic | planner | – |
| `retro-pending` | On a **PR**: finished by the pipeline, not yet seen by a retrospective | reviewer | retrospective |
| `retro-done` | On a **PR**: covered by a retrospective | retrospective | – |
| `retrospective` | On a **PR**: changes to the pipeline itself, made by the retrospective | retrospective | – |

The orchestrator creates missing labels on first run (`board.py ensure-labels`).

## Claiming

The orchestrator dispatches each agent with one issue number. Every agent still
re-reads the issue's status and labels first and **stops without changes** if the
status is not the one it expects, or if `agent-busy` or `needs-human` is set. It
then adds `agent-busy`, and removes it as its very last action, also when it fails.

## Comment formats

Agents find each other's work by these headings, so keep them exact.

| Where | Heading | Written by |
|---|---|---|
| Issue | `## Implementation plan v<n>` | planner |
| Issue | `## Split into stories` | planner |
| Issue | `## Question for a human` | any agent (with `needs-human`) |
| Issue | `## Replan needed` | implementer, reviewer |
| PR | `## Review round <n>` | reviewer |
| PR | `## Fixes for review round <n>` | implementer |
| PR | `## Review decision` | reviewer |
| PR | `## Retrospective notes` | retrospective (optional) |

## Branches, plans, commits and PRs

- One issue, one branch, one PR. Branch `issue-<n>-<slug>` from `origin/main`
  (slug: kebab-case, at most 5 words). The PR targets `main`.
- PR title `<issue title> (issue #<n>)`. Body starts with `Closes #<n>` and ends with
  the attribution line from the session's system instructions.
- The plan of record is the newest `## Implementation plan v<n>` comment. The
  implementer also commits it verbatim as `plans/yyyy-mm-dd-<slug>-v<n>.md` (CLAUDE.md).
- Architectural decisions that bind other issues go in `adrs/ADR<nnn>-<title>.md`
  (Status, Context, Decision, Consequences), numbered after the highest existing one.
- Commit messages: imperative subject ending in `(issue #<n>)`, then the attribution
  lines from the session's system instructions. One logical step per commit; review
  fixes are separate commits, never amended or force-pushed.
- Merges are squash merges done by the reviewer (`gh pr merge --squash --delete-branch`)
  or by a human. Nobody pushes to `main` directly.

## Quality gate

Run in the issue's worktree before handing off code, and again by the reviewer:

```bash
uv sync
LOGDASH_THREADS=4 uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

All must pass (coverage gate 90 %). Never weaken a test, a threshold or the gate to
make it pass. For latency-sensitive changes (anything a callback or page layout runs),
also run `uv run pytest -m slow --no-cov` against the real cache: link it into the
worktree with `ln -s "$(git rev-parse --path-format=absolute --git-common-dir)/../cache" cache`.
The budget is 300 ms per interaction.

## Commands

Use `.claude/sdlc/board.py` for the board. It reads board state from the issue side,
because `gh project item-list` returns stale or empty results for a while after writes.

```bash
B="python3 -I .claude/sdlc/board.py"
$B snapshot                         # every board item: number, status, priority, size, labels, PRs
$B show <issue#>                    # one item, same fields
$B status <issue#> "In review"      # move an item
$B set <issue#> Risk high           # set any single-select field
$B add <issue#> [Status]            # put an issue on the board (default Backlog)
$B ensure-labels                    # create the pipeline labels if missing
python3 -I .claude/sdlc/issue_triage.py <issue#> [--apply]   # Jev: Size, Priority, Risk, type label
python3 -I .claude/sdlc/human_review_gate.py <pr#> --review-file <f>   # Jev: merge or human
gh issue view <n> --json number,title,body,labels,comments
gh pr view <pr> --json number,title,body,headRefName,mergeable,comments,files
gh issue comment <n> --body-file <file>
gh pr comment <pr> --body-file <file>
```

Always use `--json` with `gh issue view`/`gh pr view`. In agent worktrees, run git and
`gh` commands one at a time (no `&&` chains) and pass multi-line text with
`--body-file` from a file in your scratchpad, because chained commands and inline
multi-line bodies are refused there.

## Machine limits

16 logical CPUs, 31 GB, and `logdash serve` may run alongside. At most **three**
agents run at once, and tests run with `LOGDASH_THREADS=4`.
