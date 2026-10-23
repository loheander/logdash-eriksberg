# Spot checks

`tags-spot-check.csv` is a sample of 30 log templates with the semantic tags a language
model gave them (issue #24). `tags-spot-check.json` records how it was sampled.

## Marking the sheet

Open the CSV in a spreadsheet (LibreOffice, Numbers, Google Sheets, or Excel via
Data > From Text/CSV) and save it back as CSV. Judge each tag on its own from the
template text, the service and the line count, not from the log level.

| Column | Write `agree` when |
|---|---|
| `agree_actionable` | you would also say the message does (tag `yes`) or does not (tag `no`) report a failure or degraded state an operator should look into |
| `agree_component` | the component is the one area the message is about, or a defensible choice when two fit |
| `agree_noise` | you would also hide (tag `yes`) or keep (tag `no`) this message while triaging |
| `agree_summary` | the summary says correctly, in plain language, what happened |

In each `agree_*` cell write exactly `agree` or `disagree` (upper or lower case is
fine). Do not write `yes`/`no`, `y`/`n` or `a`/`d`: the tag columns already hold
`yes`/`no`, so those would be ambiguous, and `spot-check score` rejects them with the
line and column. Leave a cell empty only if you cannot judge it; empty cells are not
counted. Say why in `note` when you disagree. Do not change `template_id`.

Then run `uv run logdash spot-check score` and commit the sheet and
`tags-spot-check-results.md`.
