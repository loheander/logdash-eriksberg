#!/usr/bin/env python3
"""Board helper for the GitHub Project used as the SDLC queue.

    board.py snapshot                   every board issue as JSON lines
    board.py show <issue#>              one board issue as JSON
    board.py status <issue#> <Status>   set the Status field
    board.py set <issue#> <Field> <Opt> set any single-select field (Priority, Size, Risk, ...)
    board.py add <issue#> [Status]      add an issue to the board (default Backlog)
    board.py ensure-labels              create the pipeline labels if missing

Board state is read from the issue side (issue.projectItems): `gh project item-list`
returns stale or empty results for a while after writes. Needs a gh token with the
`project` scope. Stdlib only; run with `python3 -I`.
"""

import json
import subprocess
import sys

OWNER = "loheander"
REPO = "logdash-eriksberg"
PROJECT = 1

LABELS = {
    "agent-busy": ("fbca04", "An agent is working on it; nobody else starts it"),
    "needs-replan": ("d93f0b", "Back in Backlog; the planner writes the next plan version"),
    "needs-human": ("b60205", "Blocked on a question only a human can answer"),
    "changes-requested": ("e99695", "Review findings are open; the implementer fixes them"),
    "epic": ("3e4b9e", "Split into sub-issues; not implemented itself"),
    "story": ("5319e7", "Sub-issue created by the planner from an epic"),
    "retro-pending": ("c5def5", "PR finished by the pipeline, not yet in a retrospective"),
    "retro-done": ("bfdadc", "PR covered by a retrospective"),
    "retrospective": ("0e8a16", "Changes to the SDLC pipeline made by the retrospective"),
}

FIELD_VALUE = """... on ProjectV2ItemFieldSingleSelectValue {
  name field { ... on ProjectV2SingleSelectField { name } } }"""

ISSUE_FIELDS = f"""
  number title state url
  labels(first: 30) {{ nodes {{ name }} }}
  assignees(first: 10) {{ nodes {{ login }} }}
  parent {{ number }}
  subIssues(first: 50) {{ nodes {{ number state }} }}
  closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {{
    nodes {{ number state mergedAt headRefName labels(first: 20) {{ nodes {{ name }} }} }} }}
  projectItems(first: 20) {{ nodes {{
    id project {{ number owner {{ ... on User {{ login }} ... on Organization {{ login }} }} }}
    fieldValues(first: 30) {{ nodes {{ {FIELD_VALUE} }} }} }} }}
"""


def gh(*args, check=True):
    res = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        sys.exit(f"gh {' '.join(args[:3])}...: {res.stderr.strip()}")
    return res.stdout


def graphql(query, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    return json.loads(gh(*args))["data"]


def board_item(issue):
    for item in issue["projectItems"]["nodes"]:
        project = item["project"]
        if project["number"] == PROJECT and project["owner"]["login"] == OWNER:
            return item
    return None


def summarise(issue):
    """The fields agents need, or None if the issue is not on the board."""
    item = board_item(issue)
    if item is None:
        return None
    fields = {
        v["field"]["name"]: v["name"] for v in item["fieldValues"]["nodes"] if v and "field" in v
    }
    return {
        "number": issue["number"],
        "title": issue["title"],
        "state": issue["state"],
        "status": fields.get("Status"),
        "priority": fields.get("Priority"),
        "size": fields.get("Size"),
        "risk": fields.get("Risk"),
        "labels": [n["name"] for n in issue["labels"]["nodes"]],
        "assignees": [n["login"] for n in issue["assignees"]["nodes"]],
        "parent": (issue["parent"] or {}).get("number"),
        "sub_issues": [
            {"number": s["number"], "state": s["state"]} for s in issue["subIssues"]["nodes"]
        ],
        "prs": [
            {
                "number": p["number"],
                "state": p["state"],
                "merged": p["mergedAt"] is not None,
                "branch": p["headRefName"],
                "labels": [n["name"] for n in p["labels"]["nodes"]],
            }
            for p in issue["closedByPullRequestsReferences"]["nodes"]
        ],
        "item_id": item["id"],
    }


def all_issues():
    query = f"""query($cursor: String) {{ repository(owner: "{OWNER}", name: "{REPO}") {{
      issues(first: 50, after: $cursor, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
        pageInfo {{ hasNextPage endCursor }} nodes {{ {ISSUE_FIELDS} }} }} }} }}"""
    cursor = ""
    while True:
        page = graphql(query, cursor=cursor) if cursor else graphql(query)
        issues = page["repository"]["issues"]
        yield from issues["nodes"]
        if not issues["pageInfo"]["hasNextPage"]:
            return
        cursor = issues["pageInfo"]["endCursor"]


def one_issue(number):
    query = f"""query($n: Int!) {{ repository(owner: "{OWNER}", name: "{REPO}") {{
      issue(number: $n) {{ {ISSUE_FIELDS} }} }} }}"""
    issue = graphql(query, n=int(number))["repository"]["issue"]
    if issue is None:
        sys.exit(f"no issue #{number}")
    return issue


def project_meta():
    project_id = gh(
        "project", "view", str(PROJECT), "--owner", OWNER, "--format", "json", "--jq", ".id"
    ).strip()
    fields = json.loads(
        gh("project", "field-list", str(PROJECT), "--owner", OWNER, "--format", "json")
    )["fields"]
    return project_id, {f["name"]: f for f in fields}


def set_field(item_id, field_name, option_name, label):
    project_id, fields = project_meta()
    field = fields.get(field_name)
    if field is None or "options" not in field:
        sys.exit(f"no single-select field {field_name!r}")
    options = {o["name"].lower(): o["id"] for o in field["options"]}
    option = options.get(option_name.lower())
    if option is None:
        sys.exit(
            f"{field_name} has no option {option_name!r}; options: "
            + ", ".join(o["name"] for o in field["options"])
        )
    gh(
        "project",
        "item-edit",
        "--id",
        item_id,
        "--project-id",
        project_id,
        "--field-id",
        field["id"],
        "--single-select-option-id",
        option,
    )
    print(f"{label}: {field_name} -> {option_name}")


def require_item(number):
    summary = summarise(one_issue(number))
    if summary is None:
        sys.exit(f"issue #{number} is not on the board")
    return summary


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "snapshot":
        for issue in all_issues():
            summary = summarise(issue)
            if summary is not None:
                print(json.dumps(summary))
    elif cmd == "show" and len(args) == 1:
        print(json.dumps(require_item(args[0]), indent=2))
    elif cmd == "status" and len(args) == 2:
        set_field(require_item(args[0])["item_id"], "Status", args[1], f"#{args[0]}")
    elif cmd == "set" and len(args) == 3:
        set_field(require_item(args[0])["item_id"], args[1], args[2], f"#{args[0]}")
    elif cmd == "add" and len(args) in (1, 2):
        url = gh(
            "issue", "view", args[0], "--repo", f"{OWNER}/{REPO}", "--json", "url", "--jq", ".url"
        ).strip()
        item_id = gh(
            "project",
            "item-add",
            str(PROJECT),
            "--owner",
            OWNER,
            "--url",
            url,
            "--format",
            "json",
            "--jq",
            ".id",
        ).strip()
        set_field(item_id, "Status", args[1] if len(args) == 2 else "Backlog", f"#{args[0]}")
    elif cmd == "ensure-labels":
        existing = set(
            json.loads(
                gh(
                    "label",
                    "list",
                    "--repo",
                    f"{OWNER}/{REPO}",
                    "--limit",
                    "200",
                    "--json",
                    "name",
                    "--jq",
                    "[.[].name]",
                )
            )
        )
        for name, (color, description) in LABELS.items():
            if name not in existing:
                gh(
                    "label",
                    "create",
                    name,
                    "--repo",
                    f"{OWNER}/{REPO}",
                    "--color",
                    color,
                    "--description",
                    description,
                )
                print(f"created label {name}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
