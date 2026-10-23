---
name: retrospective
description: Run a retrospective over the pipeline PRs labelled retro-pending. Find recurring review findings, implementation mistakes, plan gaps and human-review outcomes, then improve the agents, skills, protocol and human-review gate, and open a PR with the changes. Use when asked for a retrospective, to improve the SDLC agents, or when the orchestrator triggers one after 5 finished PRs.
---

# Retrospective

You improve the system, not the product code. Read `.claude/sdlc/protocol.md` and
`.claude/sdlc/retro-log.md` in full first. The log tells you what earlier
retrospectives changed and why. Don't undo an earlier change without new evidence
that it made things worse.

## 1. Collect

1. The PRs to cover: `gh pr list --state all --label retro-pending --json number,title,state,mergedAt,labels --limit 50`.
   If there are fewer than 5, stop and report (unless the user asked explicitly).
2. For each PR, gather, with `gh pr view <pr> --json title,body,comments,commits,files,state,mergedAt,reviews`
   and the linked issue's comments:
   - the plan versions, and any `## Replan needed` or `## Question for a human`,
   - every `## Review round` finding (id, severity, lens, file) and how it was resolved,
   - the number of review rounds, and any forced escalation after round 3,
   - the `## Review decision`: gate verdict, reasons, Jev values,
   - for Human review PRs: what the human did (merged as is, commented, asked for
     changes, reverted). Human comments are the strongest signal you have,
   - for auto-merged PRs: whether a later PR fixed or reverted something they
     introduced (`git log --oneline origin/main` and the later PRs' bodies).
3. Write the raw table to your scratchpad: one row per finding, with the columns PR,
   id, severity, lens, category (your own short tag), and root cause (plan gap,
   implementation slip, missing convention, test gap, review miss).

## 2. Find patterns

A pattern is something that happened in **at least 2 of the PRs**, or once with
serious consequences (a human found a blocker the reviewer missed, or an auto-merged
change was reverted). For each pattern, decide where it should have been caught
earliest, and fix it there:

| Root cause | Where to fix it |
|---|---|
| Plan left a decision open or missed a risk | `plan-issue` (Risks checklist, Context, step detail) |
| Implementer repeated a mistake despite a good plan | `implement-issue`, or `.claude/agents/implementer.md` |
| Reviewer missed something a human or a later PR found | a `review-issue` lens, or a new one |
| Reviewer flagged noise that was always rejected | `review-issue` severity guidance |
| Humans approved Human-review PRs without changes, repeatedly | gate thresholds (less caution) |
| An auto-merged PR needed a fix or revert | gate thresholds or hard rules (more caution) |
| Agents tripped over the board, labels or commands | `protocol.md`, `board.py`, `orchestrate-kanban` |
| A missing project convention | `CLAUDE.md` (only for conventions that hold for humans too) |

Also look at the throughput signals: review rounds per PR, replans, `needs-human`
stops, and agents that stopped on bad state. Something that costs a round on most
PRs is worth a rule.

## 3. Change the system

1. `git fetch origin`, then `git switch -c retro-<yyyy-mm-dd> origin/main`.
2. Make small, specific edits: a concrete rule or check with a one-line reason, and an
   example from the PR that motivated it. Don't add generic advice ("be careful with
   edge cases"). Delete or merge rules that the evidence shows are useless, so the
   files don't only grow.
3. Gate thresholds: change `human_review_gate.py` constants by small steps, and only
   with at least 3 supporting PRs. Re-run the gate on the covered PRs and record the
   before and after decisions.
4. Append a section to `.claude/sdlc/retro-log.md`: date, PRs covered, the pattern table
   (pattern, PRs, root cause), each change with its evidence, the throughput numbers,
   and what the next retrospective should watch.
5. Commit with the subject `Retrospective <yyyy-mm-dd>: <main theme>` and the
   attribution lines, then push.
6. Open the PR: `gh pr create --base main --label retrospective --title "Retrospective <yyyy-mm-dd>" --body-file <file>`,
   with the pattern table and the list of changes in the body. **Never merge it
   yourself**: changes to the pipeline always go to a human, and the gate's hard
   rules send `.claude/` changes to a human anyway.
7. Relabel the covered PRs, one command per PR:
   `gh pr edit <pr> --remove-label retro-pending --add-label retro-done`.
   You may add a `## Retrospective notes` comment on a PR that a pattern came from.
8. Report the PR URL, the patterns, and the changes.

If you find no pattern, still append a short log section, relabel the PRs, and report
that no change was warranted. A retrospective that changes nothing is fine.
