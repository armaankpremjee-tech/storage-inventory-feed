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
# The bot lead tripwire is a separate alert (label bot-leads): the sites can be fine while a
# script floods Monument with fake leads, which is what got Lifetime's merchant blocked.
bot_active=$(python3 -c "import json;b=json.load(open('result.json')).get('bot_leads') or {};print(1 if b.get('active') else 0)")
bot_persist=$(python3 -c "import json;b=json.load(open('result.json')).get('bot_leads') or {};print(1 if b.get('persist') else 0)")
bot_available=$(python3 -c "import json;b=json.load(open('result.json')).get('bot_leads') or {};print(1 if b.get('available') else 0)")
bot_summary=$(python3 -c "import json;b=json.load(open('result.json')).get('bot_leads') or {};print(b.get('summary',''))")
bot_detail=$(python3 -c "import json;b=json.load(open('result.json')).get('bot_leads') or {};print(b.get('detail',''))")
echo "$(date -u +%FT%TZ) state=$state $summary"
echo "$(date -u +%FT%TZ) bot_leads active=$bot_active persist=$bot_persist $bot_summary"
down_issue=$(gh issue list -R "$GITHUB_REPOSITORY" --label site-units-down --state open --json number --jq '.[0].number // empty')
deg_issue=$(gh issue list -R "$GITHUB_REPOSITORY" --label site-units-degraded --state open --json number --jq '.[0].number // empty')
bot_issue=$(gh issue list -R "$GITHUB_REPOSITORY" --label bot-leads --state open --json number --jq '.[0].number // empty')
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY RUN, no issue changes. down_issue=${down_issue:-none} degraded_issue=${deg_issue:-none} bot_issue=${bot_issue:-none}"
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
# Bot leads: open on the first flagged run, comment every 6 hours while leads keep
# arriving, close once the 2 hour window is clear. Counts and signature names only:
# this repository is public, so no lead's name, phone, email or address goes here.
if [ "$bot_active" = "1" ] || { [ -n "$bot_issue" ] && [ "$bot_persist" = "1" ]; }; then
  printf -v bbody '%s %s.\n\nA script is posting fake leads into Monument'"'"'s storefront for our facilities. The last time this ran unchecked (9 to 12 Oct 2026, 476 fake Abandoned Cart leads) Payrix flagged card testing on the Lifetime merchant account and blocked it for 15 days, and the captcha Monument then put on the storefront broke both websites'"'"' unit lists for a week.\n\nWhat to do today: tell Monument support on the existing storefront thread (which API key or IPs are creating the leads, block them, bulk remove the records, keep the captcha on); confirm the captcha is still on (the Diagnostics line of the site units check says so); watch both facilities'"'"' completed payments daily for 14 days; the dormant lead task must keep holding these numbers as invalid_synthetic and text nobody. Evidence from this run (counts only):\n%s\n\nThis check runs every 5 minutes from GitHub and from the M4, comments every 6 hours while the leads keep coming, and closes itself once none have arrived for 2 hours.' "$MENTION" "$bot_summary" "$bot_detail"
  if [ -z "$bot_issue" ]; then
    gh issue create -R "$GITHUB_REPOSITORY" --title "Bot leads: $bot_summary" --label bot-leads --body "$bbody"
  else
    printf -v still 'Still arriving: %s.\n%s' "$bot_summary" "$bot_detail"
    comment_if_due "$bot_issue" 6 "$still"
  fi
elif [ -n "$bot_issue" ] && [ "$bot_available" = "1" ]; then
  # Close only on a successful read: a failed leads read is unknown, never clear.
  printf -v clear 'Clear: no bot lead at either facility for 2 hours.\n%s' "$bot_detail"
  gh issue close "$bot_issue" -R "$GITHUB_REPOSITORY" --comment "$clear"
fi
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
    printf -v body '%s %s.\n\nThe sites are up for customers, but a protection is at risk. If it is the snapshot or the Integration key: the snapshot only stays fresh while this repository'"'"'s refresh workflow and the Monument Integration key keep working. If it is the storefront captcha being OFF: the public storefront key answers without a captcha again, which is how a script posted 476 fake leads on 9 to 12 Oct 2026 and got Lifetime'"'"'s merchant account blocked by Payrix for card testing; ask Monument on the existing storefront thread whether they turned it off, and watch the bot-leads alert and both facilities'"'"' payments closely. Evidence:\n%s\n\nThis check runs every 5 minutes, comments once a day while it lasts, and closes itself on recovery.' "$MENTION" "$summary" "$detail"
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
