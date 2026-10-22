---
name: plan-story
description: Conventions for writing an implementation plan for one story. Covers file naming, versioning and required sections. Use when writing or revising a plan under plans/.
---

# Plan conventions

> DRAFT: to be worked out together with the user during the first story. Only the
> parts marked *settled* are binding so far.

## File (settled)

`plans/yyyy-mm-dd-<slug>-vv.md`. The date is the day the version was written, `<slug>`
matches the branch slug, and `vv` is 01, 02, …. A replan is a new file; old versions
stay as history.

## Sections (proposal)

1. **Story**: link, goal, and acceptance criteria copied from the story.
2. **Context**: the existing code involved, as `path:line` references.
3. **Design**: the approach and the alternatives rejected. An ADR goes in `adr/` if the
   decision outlives this story.
4. **Steps**: ordered and small, each naming the files it touches and what changes.
   An implementer must be able to follow them without making design decisions.
5. **Tests**: which tests to add or change, and what each proves. Map them to the
   acceptance criteria.
6. **Risks and open questions**.
7. **Changes from previous version** (replans only): each review finding and how this
   version addresses it.

## Open questions for iteration 1

- How detailed should the steps be (signatures? pseudocode?) for a cheaper implementer?
- A size limit on a plan or on a story?
- When does a decision earn an ADR?
