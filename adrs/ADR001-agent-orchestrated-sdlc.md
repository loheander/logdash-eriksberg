# ADR001: Agent-orchestrated SDLC on the project board

## Status

proposed

## Context

Most logdash issues have been planned, implemented and reviewed with Claude Code, but
by hand: a person moved each ticket, started each session and reviewed every PR. An
earlier attempt (`feature/semantic-template-tags`) automated planning and
implementation, but sent every PR to a human. Most PRs are isolated detectors or page
changes that a careful agent review covers well, so human review time went to PRs
that did not need it.

## Decision

The GitHub Project "Logdash @ Eriksberg" is the work queue, and five Claude Code agents
work it (`.claude/agents/`, `.claude/skills/`, shared rules in `.claude/sdlc/protocol.md`):

- **planner** (`/plan-issue`): Backlog → Ready. Writes the plan as an issue comment,
  or splits a large issue into stories.
- **implementer** (`/implement-issue`): Ready → In progress → In review. Opens one PR
  per issue against `main`; in fix mode it answers review findings.
- **reviewer** (`/review-issue`): reviews in numbered rounds (at most 3) through fixed
  lenses, hands findings back to the implementer, then asks
  `.claude/sdlc/human_review_gate.py` whether a human must look. The gate combines hard
  rules (dependencies, ingest, `.claude/`, size, P0) with the Jev decision model's impact
  and blast-radius scores, and fails closed. **merge** → squash-merge and Done;
  **human** → Human review.
- **retrospective** (`/retrospective`): after every 5 finished PRs (`retro-pending`),
  finds recurring findings and mistakes and opens a PR that changes the agents, skills
  or gate thresholds. A human merges it.
- **orchestrator** (`/orchestrate-kanban`, `claude --agent orchestrator`): the main
  session; dispatches the others by board status and labels, and reconciles.

All state lives on GitHub (status, labels, comments with fixed headings), so any run
can resume another's work and humans can intervene by moving cards or answering
`needs-human` questions.

## Consequences

- Low-risk PRs merge without a human. The gate was calibrated on the 18 PRs merged
  before it existed; 9 would have auto-merged (`.claude/sdlc/retro-log.md`).
- Jev scores are a judgement on metadata (title, plan, file stats, review summary), not
  on the code itself. The hard rules and the fail-closed behaviour limit the damage of
  a wrong score; the retrospective watches for auto-merged PRs that later needed fixes.
- The pipeline must be on `main` before it runs, because agents branch from
  `origin/main`. Changes to the pipeline always go through human review.
- Plans live in issue comments and are also committed under `plans/` by the
  implementer, so CLAUDE.md's plan convention still holds.
