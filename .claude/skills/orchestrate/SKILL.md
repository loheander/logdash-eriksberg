---
name: orchestrate
description: Run the agent organisation that uses the GitHub Project board as a queue. Break a parent issue into stories, then dispatch planner, implementer and reviewer sub-agents by board status. Use when asked to orchestrate, work the board, split an issue into stories, or run the queue.
---

# Orchestrate

You are the orchestrator, the main session. You do not plan, implement or review
code yourself. You split issues into stories and dispatch sub-agents. First read
`board.md` in this folder; its rules bind you and every agent you start.

## 1. Split a parent issue into stories

Run this when the user names an issue (`/orchestrate 26`).

1. Read the issue: `gh issue view <n> --json number,title,body,labels,comments`.
   Read the code it touches so stories follow real module boundaries.
2. Draft the stories using the template in `board.md`. Each one must be independently
   reviewable, and their dependencies must form a DAG. Prefer thin vertical slices
   that each leave `main` working.
3. **Show the list to the user and wait for approval.** Creating issues is visible to
   the whole team.
4. For each approved story:
   ```bash
   gh issue create --label story --title "<title>" --body-file <file>   # prints the URL
   id=$(gh api repos/loheander/logdash-eriksberg/issues/<story#> --jq .id)
   gh api repos/loheander/logdash-eriksberg/issues/<parent#>/sub_issues -F sub_issue_id=$id
   .claude/skills/orchestrate/board.sh add <story#> Backlog
   ```
5. Create and push the feature branch: `git push origin main:refs/heads/feature/<feature-slug>`.
   Then comment the branch name and the story list on the parent issue.
6. Report the story numbers with their dependencies.

## 2. Work the queue

Repeat until nothing can be dispatched, with at most two sub-agents running at once:

| Board state | Dispatch |
|---|---|
| Backlog, no `agent-busy` | `planner` with the story number |
| Ready, all "Depends on" stories Done | `implementer` with the story number |
| In review, no `agent-busy` | `reviewer` with the story number |
| Human review, PR merged | close the story (`gh issue close <n> --reason completed`) and run `board.sh status <n> Done` |
| Human review, PR open | nothing; list them for the user |

Order: reviews first (they unblock humans), then implementation, then planning.
A story with `needs-replan` goes to the planner like any other Backlog story.

Start every sub-agent with `isolation: "worktree"` so parallel branches don't collide.
Give it a prompt like the following and nothing more, because the agent's definition
holds its instructions:

> Story #<n>. Parent issue #<p>. Feature branch `feature/<feature-slug>`.
> Story branch `feature/<feature-slug>-<n>-<story-slug>` (create it from the feature branch if it does not exist).
> `.claude/` is committed on the feature branch, not on `main`: if it is missing, check out
> your story branch first, or read it under `/Users/emny/src/logdash-eriksberg/.claude/`.

(Drop the last two lines once the feature branch is merged to `main`.)

Before starting a Ready story, merge the latest feature branch into the story branch
if any of its dependencies were merged since it was planned. If that merge conflicts,
send the story back to the planner.

When an agent returns, check `board.sh list` against what it says it did. If the board
disagrees, report the difference instead of fixing it silently. Three rejections of the
same story mean a planning problem: stop dispatching it and ask the user.

## 3. Close out

When every story of a parent issue is Done, open a PR from `feature/<feature-slug>` to
`main`. Give it the body `Closes #<parent>` and a summary linking each story PR. Then
tell the user it is waiting for human review. Merging it is a human decision.
