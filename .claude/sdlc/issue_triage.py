#!/usr/bin/env python3
"""Classify one issue's Size, Priority, Risk and type label with Jev.

    issue_triage.py <issue#> [--apply] [--item-only]

Builds one item from the issue (title, body, parent, newest plan comment), asks the
Jev decision model the questions in jev-issue-triage.json plus the human-review gate's
blast_radius question, and applies the planner's rules below. Prints one JSON object:

    {"proposed": {...}, "applied": {...}, "skipped": {field: reason},
     "unsure": [...], "jev": {...answers...}, "item": {...}}

Rules: a field is only filled when it is empty, so a human's value always wins. A
story copies its parent's Priority. Risk uses the gate's blast_radius question and
risk_level(), so planning and review grade risk the same way; the reviewer's gate
overwrites it later. The type label (bug, enhancement, documentation) is added only
if the issue has none. An answer less confident than MIN_CONFIDENCE is not applied;
it is listed in "unsure" for the planner to decide. If Jev fails, nothing is applied
and the exit status is 1. Without --apply nothing is written. Stdlib only; run with
`python3 -I`.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "jev-issue-triage.json"
GATE_SPEC = HERE / "jev-human-review.json"
REPO = "loheander/logdash-eriksberg"


def load_gate():
    spec = importlib.util.spec_from_file_location("gate", HERE / "human_review_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_gate()
JEV = gate.JEV
MIN_CONFIDENCE = gate.MIN_CONFIDENCE  # same bar as the human-review gate
MAX_TEXT = 3000  # plans are long; their Steps name the files they touch

SIZES = ["XS", "S", "M", "L", "XL"]
PRIORITIES = ["P2", "P1", "P0"]  # score levels, lowest first
KIND_LABELS = ("bug", "enhancement", "documentation")


def gh_json(*args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def board(*args, check=True):
    res = subprocess.run(
        ["python3", "-I", str(HERE / "board.py"), *args], capture_output=True, text=True
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"board.py {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout


def build_item(number):
    issue = gh_json(
        "issue", "view", str(number), "--repo", REPO, "--json", "number,title,body,comments"
    )
    show = json.loads(board("show", str(number)))
    parent = json.loads(board("show", str(show["parent"]))) if show["parent"] else None
    item = {
        "id": number,
        "title": issue["title"],
        "body": gate.clip(issue["body"], MAX_TEXT),
        "parent": parent["title"] if parent else "",
        "plan": gate.clip(
            gate.newest_comment(issue["comments"], "## Implementation plan"), MAX_TEXT
        ),
    }
    return item, show, parent


def backlog_context(number):
    """The other open board issues with their priority, for the relative judgement."""
    rows = [json.loads(line) for line in board("snapshot").splitlines() if line.strip()]
    lines = [
        f"#{r['number']} [{r['priority'] or 'unset'}] {r['title']}"
        for r in rows
        if r["state"] == "OPEN" and "epic" not in r["labels"] and r["number"] != number
    ]
    return "Open backlog, for comparing priority:\n" + "\n".join(lines)


def ask_jev(item):
    spec = json.loads(SPEC.read_text())
    spec["context"] += "\n\n" + backlog_context(item["id"])
    spec["questions"]["blast_radius"] = json.loads(GATE_SPEC.read_text())["questions"][
        "blast_radius"
    ]
    with tempfile.TemporaryDirectory() as tmp:
        spec_path, items_path = Path(tmp, "spec.json"), Path(tmp, "items.json")
        spec_path.write_text(json.dumps(spec))
        items_path.write_text(json.dumps([item]))
        res = subprocess.run(
            ["python3", "-I", str(JEV), "--spec", str(spec_path), "--items", str(items_path)],
            capture_output=True,
            text=True,
        )
    lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    if not lines or "answers" not in lines[0]:
        error = lines[0].get("error") if lines else res.stderr.strip()[-300:]
        raise RuntimeError(f"Jev failed: {error}")
    answers = lines[0]["answers"]
    expected = {"size": "score", "priority": "score", "kind": "choice", "blast_radius": "score"}
    for name, kind in expected.items():
        if answers.get(name, {}).get("type") != kind:
            raise RuntimeError(f"Jev answer {name!r} missing or not a {kind}")
    return answers


def level(answer, names):
    return names[min(len(names) - 1, max(0, round(answer["value"])))]


def decide(answers, show, parent):
    """(proposed, skipped, unsure): field -> value, field -> reason, [field, ...]."""
    proposed = {
        "Size": level(answers["size"], SIZES),
        "Priority": level(answers["priority"], PRIORITIES),
        "Risk": gate.risk_level(answers),
        "label": answers["kind"]["value"],
    }
    source = {"Size": "size", "Priority": "priority", "Risk": "blast_radius", "label": "kind"}
    current = {"Size": show["size"], "Priority": show["priority"], "Risk": show["risk"]}
    skipped, unsure = {}, []
    if parent and parent["priority"] and not show["priority"]:
        proposed["Priority"] = parent["priority"]
        source.pop("Priority")
    for field, value in list(proposed.items()):
        if field == "label":
            have = [x for x in show["labels"] if x in KIND_LABELS]
            if have:
                skipped[field] = f"already labelled {', '.join(have)}"
            elif value == "none":
                skipped[field] = "no type label fits"
        elif current[field]:
            skipped[field] = f"already {current[field]}"
        if (
            field not in skipped
            and field in source
            and answers[source[field]].get("confidence", 1) < MIN_CONFIDENCE
        ):
            skipped[field] = "low Jev confidence"
            unsure.append(field)
    return proposed, skipped, unsure


def apply(number, proposed, skipped):
    applied = {}
    for field, value in proposed.items():
        if field in skipped:
            continue
        if field == "label":
            subprocess.run(
                ["gh", "issue", "edit", str(number), "--repo", REPO, "--add-label", value],
                capture_output=True,
                check=True,
            )
        else:
            board("set", str(number), field, value)
        applied[field] = value
    return applied


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("issue", type=int)
    p.add_argument("--apply", action="store_true", help="write the fields and label")
    p.add_argument("--item-only", action="store_true", help="print the Jev item and exit")
    args = p.parse_args()

    item, show, parent = build_item(args.issue)
    if args.item_only:
        print(json.dumps(item, indent=2))
        return 0
    try:
        answers = ask_jev(item)
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc), "applied": {}, "item": item}, indent=2))
        return 1
    proposed, skipped, unsure = decide(answers, show, parent)
    applied = apply(args.issue, proposed, skipped) if args.apply else {}
    print(
        json.dumps(
            {
                "proposed": proposed,
                "applied": applied,
                "skipped": skipped,
                "unsure": unsure,
                "jev": {
                    n: {k: a.get(k) for k in ("value", "confidence", "probabilities")}
                    for n, a in answers.items()
                },
                "item": item,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
