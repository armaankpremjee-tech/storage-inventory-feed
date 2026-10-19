#!/bin/bash
# Opens, comments on and closes the alert issues from result.json (written by
# site_units_check.py). Used unchanged by the GitHub workflow in
# armaankpremjee-tech/storage-inventory-feed and by the M4 launchd watcher.
# Needs: gh authenticated (GH_TOKEN or gh auth), GITHUB_REPOSITORY, MENTION, DRY_RUN.
set -u
now=$(date -u +%s)
state=$(python3 -c "import json;print(json.load(open('result.json'))['state'])")
summary=$(python3 -c "import json;print(json.load(open('result.json'))['summary'])")
detail=$(python3 -c "import json;print(json.load(open('result.json'))['detail'])")
echo "$(date -u +%FT%TZ) state=$state $summary"
down_issue=$(gh issue list -R "$GITHUB_REPOSITORY" --label site-units-down --state open --json number --jq '.[0].number // empty')
deg_issue=$(gh issue list -R "$GITHUB_REPOSITORY" --label site-units-degraded --state open --json number --jq '.[0].number // empty')
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY RUN, no issue changes. down_issue=${down_issue:-none} degraded_issue=${deg_issue:-none}"
  exit 0
fi
epoch_of() { python3 -c "import sys,datetime;s=sys.argv[1].replace('Z','+00:00');print(int(datetime.datetime.fromisoformat(s).timestamp()))" "$1" 2>/dev/null || echo 0; }
comment_if_due() {
  # $1 issue number, $2 hours between comments, $3 body
  last=$(gh issue view "$1" -R "$GITHUB_REPOSITORY" --json comments,createdAt --jq '((.comments | last | .createdAt) // .createdAt) // empty')
  last_epoch=0
  [ -n "$last" ] && last_epoch=$(epoch_of "$last")
  if [ $(( now - last_epoch )) -gt $(( $2 * 3600 )) ]; then
    gh issue comment "$1" -R "$GITHUB_REPOSITORY" --body "$3"
  fi
}
case "$state" in
  down)
    printf -v body '%s %s.\n\nCustomers who open the site see "We could not load current availability" instead of sizes and prices, so online move-ins are stopped. Evidence from this run:\n%s\n\nWhat to do: open https://lifetimeccs.com/#units and https://cypresslouisiana.com/#units yourself. If the Monument Integration key call in the diagnostics is failing, Monument changed or revoked the key. If it works but the sites fail, the Netlify deploy or function is broken (app.netlify.com, sites cypress-mini-storage and lifetime-climate-storage). If only the snapshot is failing, see this repository'"'"'s refresh workflow. This check runs every 5 minutes from GitHub and from the M4, comments every 6 hours while the sites are down, and closes itself on recovery.' "$MENTION" "$summary" "$detail"
    if [ -z "$down_issue" ]; then
      gh issue create -R "$GITHUB_REPOSITORY" --title "Website units DOWN: $summary" --label site-units-down --body "$body"
    else
      printf -v still 'Still down: %s.\n%s' "$summary" "$detail"
      comment_if_due "$down_issue" 6 "$still"
    fi
    ;;
  degraded)
    if [ -n "$down_issue" ]; then
      gh issue close "$down_issue" -R "$GITHUB_REPOSITORY" --comment "Recovered: both sites show their unit list again ($summary)."
    fi
    printf -v body '%s %s.\n\nThe sites are up for customers, nothing to do tonight, but the safety net is at risk: the snapshot only stays fresh while this repository'"'"'s refresh workflow and the Monument Integration key keep working. Evidence:\n%s\n\nThis check runs every 5 minutes, comments once a day while it lasts, and closes itself on recovery.' "$MENTION" "$summary" "$detail"
    if [ -z "$deg_issue" ]; then
      gh issue create -R "$GITHUB_REPOSITORY" --title "Website units degraded: $summary" --label site-units-degraded --body "$body"
    else
      printf -v still 'Still degraded: %s.\n%s' "$summary" "$detail"
      comment_if_due "$deg_issue" 24 "$still"
    fi
    ;;
  ok)
    printf -v rec 'Recovered: %s.\n%s' "$summary" "$detail"
    [ -n "$down_issue" ] && gh issue close "$down_issue" -R "$GITHUB_REPOSITORY" --comment "$rec"
    [ -n "$deg_issue" ] && gh issue close "$deg_issue" -R "$GITHUB_REPOSITORY" --comment "$rec"
    echo "healthy"
    ;;
esac
exit 0
