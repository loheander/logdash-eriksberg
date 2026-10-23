#!/usr/bin/env python3
"""Decide whether a reviewed PR needs a human before it merges.

    human_review_gate.py <pr#> [--review-file F] [--item-only]

Builds one item from the PR (title, issue, plan, changed files, review summary),
asks the Jev decision model the questions in jev-human-review.json, and applies the
rule below. Prints one JSON object:

    {"decision": "human" | "merge", "risk": "low" | "medium" | "high",
     "reasons": [...], "jev": {...answers...}, "item": {...}}

Hard rules run first and do not need Jev. Any Jev failure means "human": the gate
fails closed. The thresholds are the retrospective's to tune; record each change in
.claude/sdlc/retro-log.md. Stdlib only; run with `python3 -I`.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "jev-human-review.json"
JEV = Path(os.path.expanduser("~/.claude/skills/jev-classify/scripts/jev_decide.py"))
REPO = "loheander/logdash-eriksberg"

# Hard rules: any match sends the PR to a human without asking Jev.
SENSITIVE_PATHS = ("pyproject.toml", "uv.lock", ".claude/", ".github/", "src/logdash/ingest/")
MAX_CHANGED_LINES = 1200  # additions + deletions, tests included
HUMAN_PRIORITIES = ("P0",)

# Jev rule. Scores are expected levels: impact and blast_radius 0..3.
IMPACT_MAX = 2.5  # human if impact >= this
BLAST_MAX = 2.0  # human if blast_radius >= this
SUM_MAX = 3.5  # human if impact + blast_radius >= this
# Pilot on 18 merged PRs (retro-log.md): P(needs_human) ranged 0.65-0.96 and did not
# separate them, so it only decides when it is near-certain. Recorded either way.
NEEDS_HUMAN_P = 0.98  # human if P(needs_human) >= this
MIN_CONFIDENCE = 0.35  # human if any answer is less confident than this

MAX_TEXT = 1500


def gh_json(*args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def newest_comment(comments, heading):
    bodies = [c["body"] for c in comments if c["body"].lstrip().startswith(heading)]
    return bodies[-1] if bodies else ""


def clip(text, limit=MAX_TEXT):
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " [...]"


def build_item(pr_number, review_text):
    pr = gh_json(
        "pr",
        "view",
        str(pr_number),
        "--repo",
        REPO,
        "--json",
        "number,title,body,files,closingIssuesReferences,comments",
    )
    issue, plan, priority = {}, "", None
    if pr["closingIssuesReferences"]:
        number = pr["closingIssuesReferences"][0]["number"]
        issue = gh_json(
            "issue", "view", str(number), "--repo", REPO, "--json", "number,title,body,comments"
        )
        plan = newest_comment(issue["comments"], "## Implementation plan")
        board = subprocess.run(
            ["python3", "-I", str(HERE / "board.py"), "show", str(number)],
            capture_output=True,
            text=True,
        )
        if board.returncode == 0:
            priority = json.loads(board.stdout).get("priority")
    review = review_text or newest_comment(pr["comments"], "## Review round")
    files = [f"{f['path']} +{f['additions']} -{f['deletions']}" for f in pr["files"]]
    item = {
        "id": pr["number"],
        "title": pr["title"],
        "issue": clip(f"{issue.get('title', '')}\n{issue.get('body', '')}"),
        "plan": clip(plan),
        "files": "\n".join(files[:60])
        + (f"\n... {len(files) - 60} more" if len(files) > 60 else ""),
        "review": clip(review),
    }
    changed = sum(f["additions"] + f["deletions"] for f in pr["files"])
    paths = [f["path"] for f in pr["files"]]
    return item, changed, paths, priority


def hard_rules(changed, paths, priority):
    reasons = []
    touched = sorted({p for p in paths for s in SENSITIVE_PATHS if p == s or p.startswith(s)})
    if touched:
        reasons.append(f"touches sensitive paths: {', '.join(touched)}")
    if changed >= MAX_CHANGED_LINES:
        reasons.append(f"{changed} changed lines (limit {MAX_CHANGED_LINES})")
    if priority in HUMAN_PRIORITIES:
        reasons.append(f"issue priority {priority}")
    return reasons


def ask_jev(item):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([item], f)
        items_path = f.name
    try:
        res = subprocess.run(
            ["python3", "-I", str(JEV), "--spec", str(SPEC), "--items", items_path],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(items_path)
    lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    if not lines or "answers" not in lines[0]:
        error = lines[0].get("error") if lines else res.stderr.strip()[-300:]
        raise RuntimeError(f"Jev failed: {error}")
    answers = lines[0]["answers"]
    expected = {"impact": "score", "blast_radius": "score", "needs_human": "noul"}
    for name, kind in expected.items():
        if answers.get(name, {}).get("type") != kind:
            raise RuntimeError(f"Jev answer {name!r} missing or not a {kind}")
    return answers


def jev_rules(answers):
    impact = answers["impact"]["value"]
    blast = answers["blast_radius"]["value"]
    p_human = answers["needs_human"]["value"]
    reasons = []
    if impact >= IMPACT_MAX:
        reasons.append(f"impact {impact:.2f} >= {IMPACT_MAX}")
    if blast >= BLAST_MAX:
        reasons.append(f"blast radius {blast:.2f} >= {BLAST_MAX}")
    if impact + blast >= SUM_MAX:
        reasons.append(f"impact + blast radius {impact + blast:.2f} >= {SUM_MAX}")
    if p_human >= NEEDS_HUMAN_P:
        reasons.append(f"P(needs human) {p_human:.2f} >= {NEEDS_HUMAN_P}")
    low = [n for n, a in answers.items() if a.get("confidence", 1) < MIN_CONFIDENCE]
    if low:
        reasons.append(f"low Jev confidence on {', '.join(low)}")
    return reasons


def risk_level(answers):
    if answers is None:
        return "high"
    blast = answers["blast_radius"]["value"]
    return "high" if blast >= BLAST_MAX else "medium" if blast >= 1.0 else "low"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("pr", type=int)
    p.add_argument(
        "--review-file", help="review summary to send instead of the newest review comment"
    )
    p.add_argument("--item-only", action="store_true", help="print the Jev item and exit")
    args = p.parse_args()

    review = Path(args.review_file).read_text() if args.review_file else ""
    item, changed, paths, priority = build_item(args.pr, review)
    if args.item_only:
        print(json.dumps(item, indent=2))
        return

    reasons = hard_rules(changed, paths, priority)
    answers = None
    try:
        answers = ask_jev(item)
        reasons += jev_rules(answers)
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        reasons.append(f"fail closed: {exc}")

    print(
        json.dumps(
            {
                "decision": "human" if reasons else "merge",
                "risk": "high" if answers is None and reasons else risk_level(answers),
                "reasons": reasons,
                "jev": {
                    n: {k: a.get(k) for k in ("value", "confidence", "probabilities")}
                    for n, a in (answers or {}).items()
                },
                "changed_lines": changed,
                "item": item,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
