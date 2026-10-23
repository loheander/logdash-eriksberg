# Anomaly triage panel with the Jev decision model (v2)

Changes from v1, after review: the model is called live on click, not during the build.
The sidebar shows the call delay and animates the answers into place. Raw host names
are sent (the data set is open). `subsystem` is mapped in code, not asked.

## Goal

A live demo of what a decision model adds to the anomaly list. The detectors say *how
unusual* a window is (`score`, `overall`). They do not say *whether anyone must act*,
*what kind of event it is* or *how bad it is*. On a new page, `/triage`, a click on an
anomaly sends one request to `typesafe/jev-1.14`. A timer runs while the request waits,
and the delay is shown when it returns. The answers then move into place in a sidebar:
two nouls, one choice and one score gauge, each with its confidence.

## Input list: anomalies

The rows of `cache/anomalies/anomalies.parquet` (1,603 in the current cache). Each row is
a small case: detector, entity, window, score and written `evidence`. A line in the log
explorer is a templated message that its level and template already classify. Rows from
the current cache where the model's answer should differ from the detector score:

- `volume`, `neutron-l3-agent`, score 1.0: "96 episodes in all, repeating about every 77
  min". The score is the maximum, but this is probably a periodic job: not actionable,
  informational.
- `severity`, `barbican-api`, score 0.75: "ERROR burst: peak 101/min vs baseline 0/min …
  over 112 min". Its rank is lower, but this is a service fault: actionable, high severity.
- `contextual`, host `eselda12u01s09`: "could not open network device (No such device)".
  This is a configuration problem on one host, of medium severity.

These three are the demo script and the anchored examples of `severity`.

## Questions to the model

The shared `context` describes the data set (24 h of OpenStack logs, 162 hosts, 40
services) and the purpose (on-call triage), with one sentence per detector type on what it
measures. The `state` has named fields:

- `detector`, `detector_description`, `type` and `kind`;
- `entity`, `subsystem` (from code, see below), `window_start` and `window_minutes`;
- `evidence`, in full;
- `templates`: the masked text of `template_id` and of each `T<n>` named in the evidence,
  at most 5;
- `levels`: line counts per level for the entity in the window.

| id | type | question | options / levels |
|---|---|---|---|
| `actionable` | noul | Would an on-call operator need to investigate or fix this? | true: a fault, a degradation or a misconfiguration that persists or recurs. false: expected behaviour such as a scheduled or periodic job, a load change, a restart that recovered, or a logging artefact |
| `user_impact` | noul | Does this probably affect tenants or API clients? | true: failed or slow API requests, instance or volume operations that fail, errors returned to clients. false: internal only, or no failure |
| `nature` | choice | What kind of event is it? | `error_fault`, `config`, `load`, `periodic`, `lifecycle`, `noise`, `unclear`, each defined as in v1 |
| `severity` | score | How severe is this for the operation of the cloud? | 0 informational, 1 low, 2 medium (one host or service degraded), 3 high (a service fails or many hosts affected), 4 critical (tenant outage), with the v1 wording and the three examples above |

The questions are independent of each other. The spec is a module constant in
`triage.py`. Its hash and the model id are shown in the sidebar footer.

## Subsystem, in code

`subsystem` is a deterministic function of the `program` (the service), by prefix:

| subsystem | programs |
|---|---|
| network | `neutron-*`, `openvswitch` |
| compute | `nova-*`, `libvirt`, `placement-*` |
| storage | `cinder-*`, `glance-*` |
| identity_secrets | `keystone*`, `barbican*` |
| orchestration | `heat-*`, `octavia-*` |
| control_plane | `haproxy`, `mariadb*`, `horizon-*` |
| observability | `fluent` |
| unknown | null or unmatched |

- **Service rows:** the entity is the program.
- **Host and fleet rows:** the subsystem of the program with the most lines in the window,
  read at click time in the same query as the level counts.
- **Request rows:** the programs of the request's lines.

A test checks that every program in the synthetic fixtures maps to a subsystem other than
`unknown`. The value is shown as a badge in the sidebar header and as a table column. It
is also sent in the state, as a fact rather than a question.

## Live call flow

1. A click on a grid row (`cellClicked`) or on a point in the scatter sets a
   `triage-selected` store to the `row_key` (a hash of `detector, kind, entity, bin,
   window_s, template_id`, computed in `triage.py`).
2. A clientside callback opens the drawer at once and fills the header from the row data
   (no server trip). It records `performance.now()` and enables a 50 ms `dcc.Interval`
   that updates "asking jev-1.14 … 0.35 s".
3. The server callback builds the state (one DuckDB query for the level counts and the
   dominant program, pushed down to the hour partitions), then calls Jev. It times both
   with `time.perf_counter`. It returns the answers, `state_ms`, `jev_ms`, the cost and
   the `row_key`.
4. When the response arrives, the interval stops. The timer shows the round trip the
   browser measured, split as "state 40 ms · jev 910 ms · other 30 ms". The answers
   animate in.
5. A response whose `row_key` is no longer the selected one is ignored. Dash drops
   outdated responses already, and this check guards against a race.

There is no cache: each click is a new call, so the delay seen is real. An "ask again"
button repeats the call for the same row, which also shows whether the answers are
stable. With no `OPENROUTER_API_KEY`, the page shows a banner and the drawer shows the
error; the grid still works. Failed calls (4xx, or out of retries) and unexpected answer
types are shown as errors in the drawer, never as default values. The client retries
once at most (not four times), because someone is watching it.

The 300 ms budget applies to the page, the grid, the drawer opening and the state
query. It does not apply to the Jev call, which is the point of the demo. The latency
benchmark covers these with a fake poster.

## Sidebar layout and animation

`dmc.Drawer(position="right", size=460)`, from top to bottom:

- **Header:** detector, entity, subsystem badge, window and detector score, the full
  evidence, and links to the explorer and to `/anomalies`.
- **Call status:** the live timer, then the split delay and the cost.
- **Nouls:** a `dmc.Progress` bar each, showing P(yes) from 0 to 1, with a tick at 0.5 and
  the value as a label.
- **Choice `nature`:** one `dmc.Progress` row per option, sorted by probability, with the
  chosen option in the accent colour.
- **Score `severity`:** a semicircular gauge from 0 to 4 with coloured bands per level. Its
  needle points to the expected value. Below it, one probability bar per level.
- **Confidence:** a `dmc.RingProgress` next to each answer. A "check" badge appears, and
  the answer turns muted, when the confidence is below 0.5 or the top two probabilities
  are within 0.1.
- **Footer:** the model id and the spec hash.

Animation: the components stay mounted, and only their values change, so the
transitions are CSS transitions of 250 ms with ease-out:

- **Bars and rings:** `dmc.Progress` and `dmc.RingProgress` with `transitionDuration=250`.
- **Gauge needle:** an inline SVG whose needle has `transform: rotate(...)` with a CSS
  transition. It replaces the Plotly `Indicator`, which does not reliably animate
  between updates.
- **Numbers:** they count up in a small clientside callback, over the same 250 ms.

While a call waits, the previous answers stay in place but dimmed. When it returns, they
move from the old values to the new ones, so switching between two rows shows how the
answers differ. On the first open, they move up from 0.

## Page `/triage`

- **Module:** `src/logdash/analyses/triage.py`, added to `BUILTIN`. `depends_on =
  ("anomalies", "templates")`. `compute` writes nothing and only notes the spec hash, as
  the explorer does. `/anomalies` stays as it is.
- **Controls:** the type filter and the sort, as on `/anomalies`.
- **Scatter:** detector `score` against time, coloured by subsystem (precomputed for
  service rows, `unknown` for the others until clicked). A click on a point also opens
  the drawer. A plot of score against judged severity is not possible without
  precomputing, so it is left out.
- **Grid:** `dash_ag_grid` with window, type, kind, entity, subsystem, score and evidence
  (shortened, with the full text in the tooltip). The rows judged in this session get a
  small severity chip, held in a `triage-session` store in the browser. As the demo
  goes on, the list fills with judgements.

## Client (`src/logdash/jev.py`)

A stdlib client based on the skill's `jev_decide.py`:

- `POST https://openrouter.ai/api/alpha/decisions`, with a 10 s timeout and at most one
  retry;
- examples folded into `instructions`;
- answers normalised to `type, value, confidence, probabilities, legend`.

The poster is injectable, so the tests run offline. The key comes from
`OPENROUTER_API_KEY`. New settings: `jev_model` (`typesafe/jev-1.14`) and `jev_timeout_s`
(10). Raw host names and request ids are sent as they are.

## Steps

1. `jev.py` and its tests (fake poster: success, one retry, 4xx, wrong answer type,
   timeout).
2. `subsystem()` mapping, `row_key`, the state builder and the click-time DuckDB query,
   with tests on the synthetic fixtures. Time the query on the real cache for host and
   request rows, and add an index or narrow the window if it is above 100 ms.
3. Pilot with the real key: 12 hand-judged rows (two per detector type, including the
   three demo rows). Adjust the criteria and examples, then fix the spec. Note the delays
   seen (p50 and maximum).
4. The page: grid, scatter and drawer skeleton with the clientside open and timer.
5. The answer components and their transitions: bars, rings, the SVG gauge and the
   number count-up. Check them in both themes with the `run` skill and a recorded GIF
   of the three demo rows.
6. Tests in `test_triage.py` (layout, callbacks with a fake poster, the stale-response
   guard, the no-key banner). Add the page to the latency benchmark.
7. README: the page, the key, what is sent, and the demo script.

## Out of scope

- Precomputed judgements and sorting or filtering by them.
- Live judging of lines in the log explorer.
- Free-text explanations (Jev returns probabilities only).

## Status (2026-10-22)

Implemented: `src/logdash/jev.py`, `src/logdash/analyses/triage.py`, the clientside half in
`src/logdash/app/assets/triage.js` (and `triage.css`), tests in `tests/unit/test_jev.py` and
`tests/unit/test_triage.py`, the latency benchmark and the README. The spec hash is
`55753de436`.

**Pilot (step 3).** 12 rows, two per detector type, including the three demo rows, judged by
hand and then by `typesafe/jev-1.14`. Agreement is the share of rows where the rounded noul,
the chosen option and the severity within 0.75 match the hand judgement:

| round | actionable | user impact | nature | severity | Jev p50 | Jev max |
|---|---|---|---|---|---|---|
| 1 (v2 criteria as written) | 9/12 | 8/12 | 10/12 | 6/12 | 300 ms | 444 ms |
| 2 (fixed spec) | 10/12 | 12/12 | 12/12 | 11/12 | 297 ms | 365 ms |

In round 1 the model rated severity high across the board and missed the periodic l3-agent
row (severity 1.6, nature `load`), even though that row is one of the examples. Round 2 adds to
the context that a high score, z or p value is not harm and that a repeating spike is a
scheduled job. It names the "repeating about every N min" cue in `periodic` and in
actionable = false, and "a routine change seen on many hosts at once" in actionable = false.
It adds "WARNING lines alone are not client errors" to user impact = false and a severity
instruction to judge the effect, not how unusual the window is. A malformed-line example
goes in `noise`. The remaining misses are borderline: the fleet minute and "Cannot find OS
information" are actionable at 0.56 and 0.61 against a hand judgement of no, and the novel
"Booting with volume" template gets severity 0.8 against 0. In the browser the full round
trip was 0.30 to 0.51 s and a call cost about $0.00008.

**State query (step 2).** The click-time query is 3 to 17 ms on the real cache for every
kind, so no index was added. A request's lines are found by id over the whole day, because
they can fall outside the window.

**Deviations from the plan.**

- The gauge is built from plain elements (a `conic-gradient` dial and a rotated needle with a
  CSS transition), not an inline SVG. `dash.html` has no SVG elements, and a Markdown SVG
  would be re-rendered rather than transitioned. The animation behaves as planned.
- A noul answer has no `confidence` in the API response, so the ring shows `|2p - 1|`.
- The rings get their 250 ms transition from `triage.css`, because `RingProgress` has no
  `transitionDuration`.
- The session store keeps `{severity, subsystem}` per row, so a judged host, fleet or request
  row also shows its subsystem (from the click-time query) in the grid and the scatter.
- The tests block all real HTTP requests and unset `OPENROUTER_API_KEY` (an autouse fixture in
  `tests/conftest.py`). The clientside functions are tested with node when it is installed.
