#!/bin/bash
# Opens, comments on and closes the alert issues from result.json (written by
# site_units_check.py). Used unchanged by the GitHub workflow in
# armaankpremjee-tech/storage-inventory-feed and by the M4 launchd watcher.
# Needs: gh authenticated (GH_TOKEN or gh auth), GITHUB_REPOSITORY, DRY_RUN.
#
# The issues are state, not notifications (Armaan, 25 Oct 2026: "i get too many
# github notifications"): no @mention, no assignee. The M4 watcher sets
# ALERT_EMAIL_HELPER, which sends Armaan exactly 1 email per open bot-leads,
# site-units-down or storefront-captcha-off issue (and per degraded issue while
# Monument refuses the Integration key), and records it in the issue body.
set -u
now=$(date -u +%s)
field() { python3 -c "import json,sys;r=json.load(open('result.json'));v=r.get(sys.argv[1]);print('' if v is None else v)" "$1"; }
bot() { python3 -c "import json,sys;b=json.load(open('result.json')).get('bot_leads') or {};v=b.get(sys.argv[1]);print(v if isinstance(v,str) else (1 if v else 0))" "$1"; }
state=$(field state)
summary=$(field summary)
detail=$(field detail)
captcha_state=$(field captcha_state)
integration_ok=$(field integration_ok)
bot_active=$(bot active)
bot_available=$(bot available)
bot_summary=$(bot summary)
bot_detail=$(bot detail)
echo "$(date -u +%FT%TZ) state=$state $summary"
echo "$(date -u +%FT%TZ) captcha=$captcha_state bot_leads active=$bot_active available=$bot_available $bot_summary"
open_issue() { gh issue list -R "$GITHUB_REPOSITORY" --label "$1" --state open --json number --jq '.[0].number // empty'; }
down_issue=$(open_issue site-units-down)
deg_issue=$(open_issue site-units-degraded)
bot_issue=$(open_issue bot-leads)
cap_issue=$(open_issue storefront-captcha-off)
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY RUN, no issue changes. down_issue=${down_issue:-none} degraded_issue=${deg_issue:-none} bot_issue=${bot_issue:-none} captcha_issue=${cap_issue:-none}"
  exit 0
fi
epoch_of() { python3 -c "import sys,datetime;s=sys.argv[1].replace('Z','+00:00');print(int(datetime.datetime.fromisoformat(s).timestamp()))" "$1" 2>/dev/null || echo 0; }
# GitHub refuses titles over 256 characters, which would leave an alert unopened.
cap() { local t="$1"; [ ${#t} -gt 250 ] && t="${t:0:247}..."; printf '%s' "$t"; }
create() {
  # $1 label, $2 title, $3 body
  gh issue create -R "$GITHUB_REPOSITORY" --title "$(cap "$2")" --label "$1" --body "$3" \
    || echo "ERROR: could not open the $1 issue" >&2
}
comment_if_due() {
  # $1 issue number, $2 hours between comments, $3 body
  last=$(gh issue view "$1" -R "$GITHUB_REPOSITORY" --json comments,createdAt --jq '((.comments | last | .createdAt) // .createdAt) // empty')
  last_epoch=0
  [ -n "$last" ] && last_epoch=$(epoch_of "$last")
  if [ $(( now - last_epoch )) -gt $(( $2 * 3600 )) ]; then
    gh issue comment "$1" -R "$GITHUB_REPOSITORY" --body "$3"
  fi
}

# Bot leads: open on the first flagged run, comment every 6 hours while it lasts,
# close only after a run that read BOTH facilities and found nothing in 24 hours.
# Counts and signature names only: this repository is public.
if [ "$bot_active" = "1" ]; then
  printf -v bbody '%s.\n\nFake leads are being posted into Monument'"'"'s storefront for our facilities, the pattern behind the October 2026 storefront incident. Treat it as same day work. Evidence from this run (counts only):\n%s\n\nThis check runs every 5 minutes from GitHub and from the M4, comments every 6 hours while it lasts, and closes itself once a run reads both facilities and finds no bot lead in 24 hours.' "$bot_summary" "$bot_detail"
  if [ -z "$bot_issue" ]; then
    create bot-leads "Bot leads: $bot_summary" "$bbody"
  else
    printf -v still 'Still arriving: %s.\n%s' "$bot_summary" "$bot_detail"
    comment_if_due "$bot_issue" 6 "$still"
  fi
elif [ -n "$bot_issue" ] && [ "$bot_available" = "1" ]; then
  # A failed read at either facility is unknown, never clear.
  printf -v clear 'Clear: no bot lead at either facility in 24 hours.\n%s' "$bot_detail"
  gh issue close "$bot_issue" -R "$GITHUB_REPOSITORY" --comment "$clear"
fi

# Storefront captcha: its own issue, so it is announced at once and never hides,
# or is hidden by, the snapshot and key alerts. Unknown changes nothing.
if [ "$captcha_state" = "off" ]; then
  printf -v cbody 'Monument'"'"'s public storefront key answers the unit list without a captcha again (it has answered 405 CAPTCHA_REQUIRED since 12 Oct 2026). The captcha is the control that stopped the October 2026 storefront bot, so check today whether Monument changed it on purpose and whether lead creation is open too; the bot-leads alert covers both facilities meanwhile.\n%s\n\nThis check runs every 5 minutes, comments once a day while it lasts, and closes itself when the probe sees the captcha again.' "$detail"
  if [ -z "$cap_issue" ]; then
    create storefront-captcha-off "Storefront captcha OFF: Monument's public key answers without a captcha" "$cbody"
  else
    comment_if_due "$cap_issue" 24 "Still off.
$detail"
  fi
elif [ "$captcha_state" = "on" ] && [ -n "$cap_issue" ]; then
  gh issue close "$cap_issue" -R "$GITHUB_REPOSITORY" --comment "Captcha back on: the public key answers 405 CAPTCHA_REQUIRED again."
fi

case "$state" in
  down)
    printf -v body '%s.\n\nCustomers who open the site see "We could not load current availability" instead of sizes and prices, so online move-ins are stopped. Evidence from this run:\n%s\n\nWhat to do: open https://lifetimeccs.com/#units and https://cypresslouisiana.com/#units yourself. If the Monument Integration key call in the diagnostics is failing, Monument changed or revoked the key. If it works but the sites fail, the Netlify deploy or function is broken (app.netlify.com, sites cypress-mini-storage and lifetime-climate-storage). If only the snapshot is failing, see this repository'"'"'s refresh workflow. This check runs every 5 minutes from GitHub and from the M4, comments every 6 hours while the sites are down, and closes itself on recovery.' "$summary" "$detail"
    if [ -z "$down_issue" ]; then
      create site-units-down "Website units DOWN: $summary" "$body"
    else
      printf -v still 'Still down: %s.\n%s' "$summary" "$detail"
      comment_if_due "$down_issue" 6 "$still"
    fi
    ;;
  degraded)
    if [ -n "$down_issue" ]; then
      gh issue close "$down_issue" -R "$GITHUB_REPOSITORY" --comment "Recovered: both sites show their unit list again ($summary)."
    fi
    printf -v body '%s.\n\nThe sites are up for customers, but the safety net is at risk: the snapshot only stays useful while this repository'"'"'s refresh workflow and the Monument Integration key keep working. Evidence:\n%s\n\nThis check runs every 5 minutes, comments once a day while it lasts, and closes itself on recovery.' "$summary" "$detail"
    if [ -z "$deg_issue" ]; then
      create site-units-degraded "Website units degraded: $summary" "$body"
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

# Delivery (the M4 only): 1 email per open alert issue, recorded in its body.
if [ -n "${ALERT_EMAIL_HELPER:-}" ]; then
  for label in bot-leads site-units-down storefront-captcha-off site-units-degraded; do
    if [ "$label" = "site-units-degraded" ] && [ "$integration_ok" != "False" ]; then
      continue  # degraded only pages while Monument refuses the Integration key
    fi
    n=$(open_issue "$label")
    [ -n "$n" ] && python3 "$ALERT_EMAIL_HELPER" --repo "$GITHUB_REPOSITORY" --label "$label" --issue "$n" --result result.json
  done
fi
exit 0
