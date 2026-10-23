---
name: orchestrate-kanban
description: Run the agent SDLC on the logdash GitHub Project board. Read the board, dispatch planner, implementer, reviewer and retrospective sub-agents by status and label, reconcile what they report, and keep going until nothing can move. Use when asked to orchestrate, run the pipeline, work the board or kanban, or to start the SDLC.
argument-hint: "[dry-run | once | only <issue#>]"
---

# Orchestrate the kanban

You are the orchestrator, the main session. You never plan, implement or review
code yourself, and you never edit a ticket's content. You read the board, start
sub-agents, and check what they did. Read `.claude/sdlc/protocol.md` first.

Arguments:
- none: work until nothing can be dispatched and no agent is running, then report.
- `dry-run`: do the pre-flight and print the dispatch list, without starting
  anything or changing the board.
- `once`: one dispatch round; wait for those agents, reconcile, report, stop.
- `only <issue#>`: like the default, but only touch that issue (and its stories).

## 1. Pre-flight

1. `git fetch origin`. Agents branch from `origin/main`, so the pipeline must be there:
   `git cat-file -e origin/main:.claude/sdlc/protocol.md`. If that fails, stop and tell
   the user to merge the SDLC setup to `main` first.
2. `gh auth status` must list the `project` scope.
3. `python3 -I .claude/sdlc/board.py ensure-labels`.
4. `python3 -I .claude/sdlc/board.py snapshot > <scratchpad>/board.jsonl` and read it.

## 2. Housekeeping (no agent needed)

Do these before dispatching, and report each one:

| Board state | Action |
|---|---|
| Human review, its PR merged | close the issue if open (`gh issue close <n> --reason completed`), move it to Done |
| Human review, its PR closed unmerged | report it for the user; change nothing |
| `epic`, every sub-issue closed | close it, move it to Done |
| Done, issue still open, PR merged | close the issue |
| `agent-busy`, no agent of this session running on it, issue not updated for 2 h (`gh issue view <n> --json updatedAt`) | remove `agent-busy` and report it as stale; otherwise leave it |
| In progress without `agent-busy` | report it (a human or an earlier run owns it); don't touch it |

## 3. Dispatch

An issue is **eligible** if it is open, on the board, and has none of `agent-busy`,
`needs-human` or `epic`, and no agent of yours is running on it. Pick from the
eligible issues in this order, highest first. Within a row, go by Priority (P0, P1,
P2, unset last), then by issue number:

| # | Board state | Agent | Prompt |
|---|---|---|---|
| 1 | In review, `changes-requested` | `implementer` | `Issue #<n>. Fix mode.` |
| 2 | In review, no `changes-requested` | `reviewer` | `Issue #<n>.` |
| 3 | Ready, every "Depends on" issue in its newest plan comment is Done | `implementer` | `Issue #<n>. Implement mode.` |
| 4 | Backlog (including `needs-replan`), every "Depends on" issue has a plan | `planner` | `Issue #<n>.` |

**Planning order (row 4).** A Backlog issue's dependencies are the issues on the
`Depends on:` line of its body (stories have one; an issue without it has none). A
dependency **has a plan** when its status is Ready, In progress, In review, Human
review or Done. A dependency that is an `epic` has a plan when every one of its
sub-issues has one. Hold an issue back while any dependency has no plan: its
planner would otherwise design against names that do not exist yet. Within a
Priority, plan first the issues whose dependencies are all Done, then those that the
most open issues list in their own `Depends on:` line (they unblock the most work),
then by issue number.

**Retrospective**: dispatch one `retrospective` agent (prompt: `Run the retrospective.`)
when `gh pr list --state all --label retro-pending --json number --jq length` is 5 or
more, no `retrospective` agent is running, and no PR labelled `retrospective` is open
(`gh pr list --label retrospective --state open`). Give it the next free slot ahead of
row 4. It skips `only` runs.

Limits: at most **3** agents at once (protocol: machine limits), at most **2** of them
planners, so a slot stays free for implementers and reviewers, and at most one agent
per issue. Never start an implementer on two issues whose plans touch the same files at
the same time; run them one after the other.

Start every agent with the Agent tool, `subagent_type` set to its name,
`isolation: "worktree"`, and only the prompt from the table. The agent's definition
and skill hold its instructions. Don't add your own guesses about the issue.

## 4. Reconcile

Each agent runs in the background and you are notified when it returns. Then:

1. Run `board.py show <n>` and compare it with what the agent says it did: status,
   labels (especially that `agent-busy` is gone), plan comment, PR, review comment.
   If they disagree, fix only the `agent-busy` label, and report the rest instead of
   repairing it silently.
2. Remove its worktree once the work is pushed: in the worktree,
   `git status --porcelain` and `git log --branches --not --remotes` are both empty;
   then `git worktree remove <path>`. A leftover worktree keeps the issue branch
   checked out, and the next agent for that issue cannot switch to it.
3. Loop guards: stop dispatching an issue, add `needs-human`, and comment
   `## Question for a human` on it when
   - it went back to Backlog with `needs-replan` for the 3rd time, or
   - the same agent failed or stopped on it twice in a row without changing anything.
4. Take a fresh snapshot and dispatch again (section 3).

## 5. Report

When nothing is running and nothing can be dispatched (or after one round with
`once`), report:
- what moved, as `#n: Backlog → Ready` lines, with PR links,
- merged PRs, and PRs waiting in Human review with the gate's reasons,
- issues stopped on `needs-human`, with their question,
- stale or inconsistent items you found,
- the `retro-pending` count, and the retrospective PR if one was opened.

For continuous operation, run this skill under `/loop` (for example
`/loop 30m /orchestrate-kanban`). Each tick re-reads the board, so humans can move
cards, answer `needs-human` questions or merge Human-review PRs at any time.
