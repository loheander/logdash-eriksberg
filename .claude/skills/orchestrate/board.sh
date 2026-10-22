#!/bin/sh
# Board helper for the GitHub Project used as the agent queue.
#   board.sh list <Status>            stories (label `story`) in that status: number<TAB>title<TAB>labels
#   board.sh status <issue#> <Status> set a story's status
#   board.sh add <issue#> [Status]    add an issue to the board (default Backlog)
#   board.sh fields                   show the Status field and its options
# Needs a gh token with the `project` scope.
set -eu

OWNER=loheander
REPO=loheander/logdash-eriksberg
PROJECT=${BOARD_PROJECT:-1}

# Board items are read from the issue side: `gh project item-list` intermittently returns
# no items for a while after writes to the board, while issue.projectItems stays accurate.
ON_BOARD="select(.project.number == $PROJECT and .project.owner.login == \"$OWNER\")"
ITEM_FIELDS='id project { number owner { ... on User { login } ... on Organization { login } } }
  fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }'

item_id() {
  gh api graphql -f query="{ repository(owner: \"${REPO%/*}\", name: \"${REPO#*/}\") {
      issue(number: $1) { projectItems(first: 20) { nodes { $ITEM_FIELDS } } } } }" \
    --jq ".data.repository.issue.projectItems.nodes[] | $ON_BOARD | .id"
}

# Stories (label `story`, open or closed) whose board status is $1.
list_status() {
  gh api graphql --paginate -f query="query(\$endCursor: String) {
      repository(owner: \"${REPO%/*}\", name: \"${REPO#*/}\") {
        issues(first: 100, after: \$endCursor, labels: [\"story\"]) {
          pageInfo { hasNextPage endCursor }
          nodes { number title labels(first: 20) { nodes { name } }
            projectItems(first: 20) { nodes { $ITEM_FIELDS } } } } } }" \
    --jq ".data.repository.issues.nodes[]
      | select([.projectItems.nodes[] | $ON_BOARD | .fieldValueByName.name] | index(\"$1\"))
      | \"\(.number)\t\(.title)\t\([.labels.nodes[].name] | join(\",\"))\""
}

project_id() {
  gh project view "$PROJECT" --owner "$OWNER" --format json --jq .id
}

status_field() {
  gh project field-list "$PROJECT" --owner "$OWNER" --format json \
    --jq '.fields[] | select(.name == "Status")' "$@"
}

set_status() {
  item=$1
  field=$(gh project field-list "$PROJECT" --owner "$OWNER" --format json \
    --jq '.fields[] | select(.name == "Status") | .id')
  option=$(gh project field-list "$PROJECT" --owner "$OWNER" --format json \
    --jq ".fields[] | select(.name == \"Status\") | .options[] | select(.name == \"$2\") | .id")
  [ -n "$option" ] || { echo "unknown status: $2" >&2; exit 1; }
  gh project item-edit --id "$item" --project-id "$(project_id)" \
    --field-id "$field" --single-select-option-id "$option" >/dev/null
  echo "$3 -> $2"
}

case "${1:-}" in
  list)
    list_status "$2"
    ;;
  status)
    item=$(item_id "$2")
    [ -n "$item" ] || { echo "issue #$2 is not on the board" >&2; exit 1; }
    set_status "$item" "$3" "#$2"
    ;;
  add)
    url=$(gh issue view "$2" --repo "$REPO" --json url --jq .url)
    item=$(gh project item-add "$PROJECT" --owner "$OWNER" --url "$url" --format json --jq .id)
    set_status "$item" "${3:-Backlog}" "#$2"
    ;;
  fields)
    status_field
    ;;
  *)
    sed -n '2,7p' "$0" >&2
    exit 2
    ;;
esac
