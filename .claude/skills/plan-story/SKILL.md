---
name: plan-story
description: Conventions for writing an implementation plan for one story. Covers file naming, versioning, required sections, ADRs and open questions. Use when writing or revising a plan under plans/.
---

# Plan conventions

Settled on story #28 (first iteration). Change them together with the user, not per plan.

## File

`plans/yyyy-mm-dd-<story-slug>-v<n>.md`:
- `yyyy-mm-dd` is the day this version was written.
- `<story-slug>` is the story part of the branch name (`tagging-stage` in
  `feature/semantic-template-tags-28-tagging-stage`).
- `<n>` is 1, 2, ….

A replan is a new file with the next `v<n>`. Old versions stay unchanged as history.
Commit each version on its own.

## Sections

1. **Story**: link, goal, and acceptance criteria copied from the story.
2. **Context**: the existing code involved, as `path:line` references.
3. **Design**: the approach and the alternatives rejected.
4. **Steps**: ordered, each naming the files it touches and what changes. Give
   signatures, constants, exact user-facing text (prompts, labels) and error handling.
   The implementer is a cheaper model and must not have to make design decisions.
   Group the steps into **commits** ("Commit 1: steps 1–3"), so that work which has to
   land together, such as test safety with the tests it protects, is in one commit.
5. **Tests**: each test to add or change, what it proves, and which acceptance
   criterion it covers. Every criterion is covered by at least one test or by a stated
   manual check.
6. **Open questions**: each one with the default the plan uses (see below).
7. **Changes from previous version** (v2 and later): each review finding or human
   decision, and how this version addresses it.

Length: no hard limit. If a plan grows past about 400 lines, check whether the story
should be split. If it should, say so in the report instead of planning it whole.

## ADRs

Write an ADR when a decision binds other stories or future features, not for choices
local to one story. Put it at `adrs/ADR<nnn>-<title-slug>.md`, numbered from `ADR001`
and zero-padded. Its sections are Status, Context, Decision and Consequences. It starts
as `Status: proposed` and becomes `accepted` when the feature branch is merged to `main`.

## Open questions

- **Non-blocking**: the plan picks a default and lists the question in the plan and in
  the PR body. The story moves to Ready, and a human or the reviewer can overrule it.
- **Blocking** (no sensible default, or the answer changes the scope): comment the
  question on the story, leave the story in Backlog without a plan commit, and report it.

## PR

Draft PR into the feature branch, titled `<story title> (story #<n>)`. The body starts
with `Story #<n>`, then gives a short design summary and the open questions.
