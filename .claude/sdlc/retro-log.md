# Retrospective log

Append-only. Each retrospective adds one section: date, PRs covered, patterns found,
and every change it made to agents, skills, the protocol or the gate thresholds, with
the evidence. Read the previous sections before changing anything, so that the system
does not flip back and forth between the same two rules.

## 2026-10-23 — baseline: human-review gate calibration

Not a retrospective; the starting point for the first one.

`human_review_gate.py` was piloted on the 18 PRs merged before the pipeline existed
(all were human-reviewed). Jev `typesafe/jev-1.14`, scores are expected levels 0..3.

| PR | Title | Changed lines | Impact | Blast radius | P(needs human) | Gate |
|---|---|---|---|---|---|---|
| #12 | Feature analysis (module 4) | 1565 | 2.42 | 1.59 | 0.93 | human |
| #13 | PCA (module 5) | 1227 | 2.91 | 1.17 | 0.95 | human |
| #14 | Anomaly detection framework and page (module 6) | 1629 | 2.82 | 1.62 | 0.95 | human |
| #15 | Detector: volume spike/drop (issue 4) | 481 | 2.16 | 1.2 | 0.89 | merge |
| #16 | Hosts and services (module 7) | 1831 | 2.62 | 1.21 | 0.94 | human |
| #17 | Detector: contextual, host vs peers (issue 5) | 704 | 2.17 | 1.16 | 0.94 | merge |
| #18 | Detectors: structural PCA rework and Isolation For | 785 | 2.43 | 1.38 | 0.96 | human |
| #19 | Detectors: novel and rare templates (issue 7) | 494 | 2.18 | 1.09 | 0.93 | merge |
| #20 | Detector: request sequences (issue 8) | 654 | 2.24 | 1.01 | 0.94 | merge |
| #21 | Detector: severity bursts (issue 9) | 702 | 2.08 | 1.23 | 0.93 | merge |
| #22 | Temporal heatmap: label every host (issue 11) | 216 | 1.06 | 1.75 | 0.72 | merge |
| #34 | Semantic tags: tagging build stage (story #28) | 2374 | 2.59 | 2.9 | 0.95 | human |
| #35 | Semantic tags: Templates page columns, tooltip and | 1123 | 1.84 | 1.28 | 0.91 | human |
| #36 | Anomaly triage with the Jev decision model | 2958 | 2.59 | 1.23 | 0.96 | human |
| #37 | Semantic tags: Explorer filters (story #30) | 1074 | 1.72 | 1.12 | 0.91 | merge |
| #38 | Semantic tags: Anomalies filters (story #31) | 864 | 1.73 | 1.32 | 0.92 | merge |
| #39 | Semantic tags: semantic severity signal in the sev | 1128 | 2.16 | 1.02 | 0.94 | merge |
| #40 | Semantic tags: spot check of 30 templates (story # | 1587 | 0.4 | 1.69 | 0.65 | human |

Findings:
- `needs_human` sat between 0.65 and 0.96 on every PR and did not separate them.
  Its threshold is 0.98, so it is recorded but rarely decides.
- Impact separated small UI changes (about 1) from detectors (about 2.1–2.2) and
  headline modules (2.4–2.9). Blast radius stayed between 1.0 and 1.75 except for the
  cache build stage (#34, 2.9).
- Thresholds: impact >= 2.5, blast radius >= 2.0, sum >= 3.5, any confidence < 0.35,
  plus the hard rules (sensitive paths, >= 1200 changed lines, P0). Result: 9 of 18
  would have auto-merged (isolated detectors, page filters, heatmap labels).
- Watch: whether auto-merged PRs later needed a fix (raise caution), and whether
  humans approve Human-review PRs without comments (lower caution).

## 2026-10-23 — baseline: planner triage with Jev

Not a retrospective; the starting point for tuning `issue_triage.py`, which the
planner now runs at hand-off to fill an empty Size, Priority, Risk and type label.
Jev `typesafe/jev-1.14`. Piloted read-only on 15 issues against the values humans or
planners had set. Each issue was left out of its own backlog context.

| Field | Exact match | Off by one level | Notes |
|---|---|---|---|
| Priority | 13 of 13 | 0 | Closed #1–#11 and open #23–#26, #44, #50. Confidence 0.32–0.49 on #24 and #44, so #44 would have been `unsure`. |
| Size | 6 of 14 | 8 | Jev sizes one level larger on 8 issues. On #23–#26, all of which the planner later split into stories, Jev's larger size is arguably the better one; on #2, #8 and #11 it is not. |
| Risk | 5 of 8 | 3 | Closed #1–#11 only. #7 and #8 sat at blast radius 0.99, just under `medium`. #11 got 1.88, close to the 1.75 the gate gave its PR (#22). |
| Type label | 15 of 15 | – | All `enhancement`. #58 (ADR and skill cleanup) got `none`; it already had `documentation`. |

Rules: fill only empty fields; a story copies its parent's Priority; Risk uses the
gate's `blast_radius` question and `risk_level()`; nothing is applied when an answer's
confidence is below the gate's `MIN_CONFIDENCE` (0.35).
Watch: whether planners or humans keep correcting Size downwards (tighten the size
criteria), and whether triage Risk and the gate's PR Risk drift apart.
