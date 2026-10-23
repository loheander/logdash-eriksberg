# Anomaly triage panel with the Jev decision model (v1)

## Goal

Show what a decision model adds to the dashboard. The detectors already say *how unusual* a
window is (`score`, `overall`). They do not say *what it is*, *whether anyone must act* or
*how bad it is*. A new page lists the anomalies. A click on a row opens a sidebar with
the answers of `typesafe/jev-1.14` to a fixed set of questions about that row: two nouls,
two choices and one score. Each answer is drawn as a chart with its confidence.

## Which list view: anomalies, not the log explorer

The input is the anomaly table (`cache/anomalies/anomalies.parquet`), for these reasons:

| | Anomalies | Log explorer |
|---|---|---|
| Items | 1,603 rows, a fixed set per build | 38.8 M lines, paged by query |
| Cost to judge all | about 1,603 × $0.00004 ≈ $0.07 and 7 min at 4 workers, once per build | not possible; only on click |
| Latency budget (300 ms) | met: the judgements are precomputed and read from Parquet | broken: a live call takes about 1 s and needs the network |
| What one item says | a case: detector, entity, window, score and a written `evidence` that names templates, rates and baselines | one line, mostly a templated INFO message whose meaning is clear |
| What the model adds | a semantic judgement next to a statistical one (see below) | little: level and template already classify a line |

The strongest demonstration is the contrast with the detector score. Examples from the
current cache:

- `volume`, `neutron-l3-agent`, score 1.0: "spike … 96 episodes in all, repeating about
  every 77 min". The score is the maximum, but this is probably a periodic job: the model
  should answer *not actionable*, *periodic*, low severity.
- `severity`, `barbican-api`, score 0.75: "ERROR burst: peak 101/min vs baseline 0/min … over
  112 min". Its rank is lower, but this is a real service fault: *actionable*, key
  management, high severity.
- `contextual`, host `eselda12u01s09`: "could not open network device (No such device)".
  This is a networking misconfiguration on one host.

The explorer stays a later option (see "Out of scope").

## Questions to the model

Shared `context` (one string, same for every item): "Anomalies found by statistical detectors
in 24 h of logs from an OpenStack cloud (162 hosts, 40 services: nova, neutron, cinder,
keystone, glance, barbican, haproxy, rabbitmq, fluent, …). Each item is one detector hit: the
detector, what it watches, the entity (service, host, fleet or request), the time window, the
detector's evidence and the log templates involved. The answers help an on-call operator
triage the list." The context also gets one sentence per detector type that says what
that detector measures, so that the model can read the evidence.

Each item (`state`) has named fields: `detector`, `detector_description`, `type`, `kind`,
`entity`, `window_start`, `window_minutes`, `evidence` (full text, not shortened),
`templates` (masked text of `template_id` and of every `T<n>` named in the evidence, at most
5) and `levels` (line counts per level for the entity in the window, from `events`).

The questions are evaluated independently, so none of them refers to another answer.

| id | type | question | options / levels |
|---|---|---|---|
| `actionable` | noul | Would an on-call operator need to investigate or fix this? | true: a fault, a degradation or a misconfiguration that persists or recurs. false: expected behaviour such as a scheduled or periodic job, a load change, a restart that recovered, or a logging artefact |
| `user_impact` | noul | Does this probably affect tenants or API clients? | true: failed or slow API requests, instance or volume operations that fail, errors returned to clients. false: internal only (agents, housekeeping, logging) or no failure |
| `subsystem` | choice | Which part of the cloud is involved? | `compute` (nova, libvirt, instances), `network` (neutron, OVS, L3, DHCP, devices), `storage` (cinder, volumes, glance images), `identity_secrets` (keystone, barbican, tokens), `control_plane` (rabbitmq, database, haproxy, API gateways), `observability` (fluent, log shipping, metrics), `other` |
| `nature` | choice | What kind of event is it? | `error_fault` (errors or exceptions from a failing component), `config` (missing device, wrong setting, unmanaged resource), `load` (more or fewer requests, nothing failing), `periodic` (a recurring job or sync at regular intervals), `lifecycle` (start, stop, deploy, upgrade, first appearance of new code paths), `noise` (logging artefact, malformed or debug output), `unclear` |
| `severity` | score | How severe is this for the operation of the cloud? | 0 "Informational: no action, expected behaviour"; 1 "Low: worth a look when convenient"; 2 "Medium: a degradation on one host or service, fix this week"; 3 "High: a service fails or many hosts are affected, fix today"; 4 "Critical: an outage for tenants, act now" |

`severity` gets three anchored `examples` from the rows above: periodic l3-agent spike → 0,
the barbican ERROR burst → 3, the host with a missing network device → 2.

`actionable` and `severity` overlap on purpose: two kinds of answer to a similar question
show whether the model is consistent with itself. The page does not derive one from the
other.

Thresholds: `actionable` and `user_impact` are shown as P(yes), with a tick at 0.5. The
table's "actionable" column uses ≥ 0.6. This value is set after the pilot, not fixed now.

## Design

### Stage `triage` (new analysis, `src/logdash/analyses/triage.py`)

Every registered analysis is also a page, so the stage and the panel are one module. The
anomalies page does not change.

- `depends_on = ("anomalies", "templates")`. `version` includes the model id, a hash of the
  question spec and a client version.
- `compute`:
  1. Read `anomalies.parquet`. Give each row a stable `row_key`: the hash of `detector, kind,
     entity, bin, window_s, template_id`. A helper in `triage.py` computes it, so the
     anomaly schema stays the same and `FRAMEWORK_VERSION` is not bumped.
  2. Build the state of each row: the template texts come from `templates.parquet`, the
     level counts from one grouped DuckDB query over `events` for all windows.
  3. Reuse the earlier answers from `judgements.parquet` when `row_key`, model and spec hash
     match. Call Jev only for the rows that are new. A rebuild after a detector change
     therefore costs only the changed rows.
  4. Write `judgements.parquet`: `row_key`, one column per answer value, its confidence and
     its probabilities (a JSON string), `cost`, `model`, `error`. Write `meta.json` with the
     totals (rows judged, reused, failed, cost, seconds) and the spec.
- Opt-in: the stage calls the model only when `LOGDASH_JEV=1` and `OPENROUTER_API_KEY` is set.
  Otherwise it writes an empty table and a note in `meta.json`, and the page says that
  judgements are off. A missing key or the network never fails `logdash build`. An item that
  fails (4xx, or out of retries) becomes an `error` row. A wrong `type` in an answer is an
  error too, never a default label.
- New settings in `config.py`: `jev` (bool, off), `jev_model` (`typesafe/jev-1.14`),
  `jev_workers` (4). `jev_model` is part of `fingerprint_params`, the key is not.

### Client (`src/logdash/jev.py`)

A small stdlib client based on the skill's `jev_decide.py`: `POST /api/alpha/decisions`,
retry on 429, 5xx and network errors, examples folded into `instructions`, and answers
normalised to `type, value, confidence, probabilities, legend`. The poster is injectable,
so the tests run offline. No new dependency.

### Page `/triage` ("Anomaly triage")

- The controls are the anomalies page's type filter and sort, plus two more sort keys,
  `severity` and `actionable`, so that the reordering by the model is one click away.
- Card 1, "Detector score vs judged severity": a scatter of `score` (x) against expected
  `severity` (y), coloured by `subsystem`, with opacity from the severity confidence. Its
  top-left (low score, high severity) and bottom-right (high score, informational) corners
  are what the model adds. A click on a point opens the sidebar too.
- Card 2: the table, as `dash_ag_grid` (already a dependency) instead of the static
  `ui.link_table`, because row clicks need `cellClicked`. Columns: window, type, entity,
  score, severity (rounded, with a low-confidence marker), actionable P, subsystem and the
  explorer link.
- Sidebar: `dmc.Drawer(position="right", size=460)`, filled by one callback on the row key.
  From top to bottom:
  - the header: detector, entity, window, detector score, the full evidence and links to the
    explorer and to the anomalies page.
  - Nouls: one horizontal bar each, from 0 to 1, filled to P(yes), with a tick at the
    threshold and the value printed.
  - Choices: a horizontal bar chart of the probabilities of all options, sorted, with the
    chosen option highlighted. This shows whether the choice was clear or close.
  - Score: a bar chart of the probability of each level (0 to 4, legend labels on the axis),
    with a vertical line at the expected value.
  - Confidence: a `dmc.RingProgress` next to each chart, showing the `confidence` the API
    returns. When the confidence is below 0.5, or the top two probabilities are within 0.1,
    a "check" badge appears and the chart is drawn in a muted colour.
  - A footer: the model id, the cost of the call and the build time of the judgement.
  The charts use Plotly and `figures.style`, so they follow the theme.
- Hint line: the rows judged, failed and reused, the total cost, and "judgements off" when
  the stage did not call the model.

### Data leaving the machine

The calls send the evidence, the template texts (already masked: `<IP>`, `<UUID>`, `<PATH>`),
the host and service names and the request ids to OpenRouter and TypeSafe. Raw payloads are
not sent. This is the reason for the opt-in flag, and the README states it.
Pseudonymising host names is open question 1.

## Steps

1. `jev.py` client and its unit tests (fake poster: success, retry, 4xx, wrong type).
2. State building and `row_key`, with tests on the synthetic fixtures.
3. Pilot: for 12 hand-picked rows (two per detector type, including the three examples
   above), judge the expected answers by hand, run the model and compare. Adjust the
   criteria and examples, then fix the spec and record the pilot in the plan's status.
4. The `triage` stage: incremental reuse, opt-in, errors and meta.
5. The page: grid, scatter, drawer and its five charts with confidence. The tests in
   `test_triage.py` use a fixture `judgements.parquet`, so no network is needed.
6. Add the triage page and drawer callbacks to the latency benchmark (under 300 ms).
7. README: the settings, the opt-in, what is sent, and a screenshot.

## Out of scope for v1

- Live calls from the log explorer (click a line → judge it with a spinner). This
  could come later, with the same client and its own question set (for example "is this
  line an error the level hides?").
- Free-text explanations: Jev returns probabilities only, and the evidence remains the
  explanation.
- Agreement with labels from operators beyond the 12-row pilot.

## Open questions

1. Should host names and request ids be replaced by stable pseudonyms (`host-017`) before
   sending? The `contextual` evidence contains host names in other places as well.
2. Is the set of `subsystem` options right for this cloud, or is the list of services in the
   data enough to give each `program` its subsystem without asking the model?
3. Is a separate `/triage` page acceptable, or should the drawer later be added to
   `/anomalies` too?
