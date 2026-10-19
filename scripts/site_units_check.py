#!/usr/bin/env python3
"""Check that both storage websites can show their unit list.

Writes result.json for the workflow's issue step:
  state: "ok" | "degraded" | "down"
  summary: one line
  detail: markdown bullets with the evidence

down     = a site's /api/monument/unit-groups is not a valid inventory after 3
           tries, or its homepage is not serving rendered unit cards.
degraded = every site is up, but the safety net is at risk: the snapshot feed
           is stale or failing, or Monument refuses the Integration key (which
           the feed and the Netlify live path both use). A site serving the
           snapshot while the feed is fresh is reported but is not an alert:
           customers see counts at most a few minutes old.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SITES = [
    ("Cypress Mini Storage", "cypresslouisiana.com", "29c2b9eb-699f-11f1-989e-e99e7c079122"),
    ("Lifetime Climate Controlled Storage", "lifetimeccs.com", "29c2b9e2-699f-11f1-989e-e99e7c079122"),
]
SHOPPING = "https://public-api.prd.monument.io/shopping/storagegiant/v1"
PUBLIC_KEY = "V6AHJGWpyQakzctySeh9NxBhw29lInd8j8541hj9"  # Monument's own storefront key, in their bundle
FEED_STATUS = "https://raw.githubusercontent.com/armaankpremjee-tech/storage-inventory-feed/main/status.json"
UA = "fleet-watchdog site-units-feed (github actions)"


def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"user-agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # noqa: BLE001
        return 0, {}, repr(e).encode()


def valid_inventory(body):
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return False, "not JSON"
    if not isinstance(data, list) or not data:
        return False, "not a non-empty array"
    for g in data:
        if not isinstance(g, dict) or not isinstance(g.get("unitGroupUuid"), str):
            return False, "group without unitGroupUuid"
        c = g.get("availableUnitCount")
        if not isinstance(c, int) or isinstance(c, bool) or c < 0:
            return False, "group without availableUnitCount"
    return True, f"{len(data)} groups, {sum(g['availableUnitCount'] for g in data)} units available"


def check_site(name, domain, uuid):
    out = {"name": name, "domain": domain, "api_ok": False, "cards_ok": False, "source": "?", "notes": []}
    for attempt in range(3):
        status, headers, body = get(f"https://{domain}/api/monument/unit-groups", {"accept": "application/json"})
        h = {k.lower(): v for k, v in headers.items()}
        out["source"] = h.get("x-inventory-source", "?")
        ok, why = (False, f"HTTP {status}") if status != 200 else valid_inventory(body)
        if ok:
            out["api_ok"] = True
            out["notes"].append(f"feed HTTP 200, {why}, source {out['source']}")
            break
        out["notes"].append(f"feed try {attempt + 1}: {why} (x-monument-status {h.get('x-monument-status', '?')}) {body[:120].decode(errors='ignore')}")
        if attempt < 2:
            time.sleep(20)
    status, headers, body = get(f"https://{domain}/", {"accept": "text/html"})
    html = body.decode(errors="ignore")
    # Server rendered unit cards carry the UnitGroupCard CSS module class on both
    # sites. (data-unit-size is NOT reliable: Lifetime's Button drops data attributes.)
    cards = html.count("UnitGroupCard-module")
    buttons = html.count("Rent Now") + html.count("Rent Outdoor Storage") + html.count("Join waitlist") + html.count("Join the waitlist")
    if status == 200 and cards > 0 and buttons > 0:
        out["cards_ok"] = True
        out["notes"].append(f"homepage HTTP 200 with {buttons} rendered unit cards")
    else:
        out["notes"].append(f"homepage HTTP {status}, rendered unit cards: {'present' if cards else 'MISSING'} ({buttons} buttons)")
    return out


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sites = [check_site(*s) for s in SITES]
    diag = []
    key = os.environ.get("MONUMENT_API_KEY", "").strip()
    probe_uuid = SITES[1][2]
    if key:
        st, _, body = get(f"{SHOPPING}/facilities/{probe_uuid}/unitGroups", {"x-api-key": key})
        diag.append(f"Monument direct with the Integration key: HTTP {st}" + ("" if st == 200 else f" {body[:100].decode(errors='ignore')}"))
        integration_ok = st == 200
    else:
        diag.append("Monument direct with the Integration key: not probed (secret missing)")
        integration_ok = None
    st, _, body = get(f"{SHOPPING}/facilities/{probe_uuid}/unitGroups", {"x-api-key": PUBLIC_KEY})
    captcha = b"CAPTCHA_REQUIRED" in body
    diag.append(f"Monument direct with the public storefront key: HTTP {st}" + (" CAPTCHA_REQUIRED (expected since 19 Oct 2026)" if captcha else ""))
    st, _, body = get(FEED_STATUS)
    feed_ok = False
    feed_note = f"snapshot feed status.json: HTTP {st}"
    if st == 200:
        try:
            s = json.loads(body)
            age = (now - datetime.fromisoformat(s["checked_at"])).total_seconds()
            feed_ok = bool(s.get("ok")) and age < 3 * 3600
            feed_note += f", ok={s.get('ok')}, heartbeat {int(age // 60)} min old"
        except Exception as e:  # noqa: BLE001
            feed_note += f", unreadable ({e!r})"
    diag.append(feed_note)

    down = [s for s in sites if not (s["api_ok"] and s["cards_ok"])]
    on_snapshot = [s for s in sites if s["api_ok"] and s["source"] == "snapshot"]
    if down:
        state = "down"
        names = " and ".join(s["name"] for s in down)
        summary = f"{names}: the unit list is NOT loading for customers"
    elif not feed_ok or integration_ok is False:
        state = "degraded"
        risk = "Monument refuses the Integration key" if integration_ok is False else "the fallback snapshot feed is stale or failing"
        if on_snapshot:
            summary = " and ".join(s["name"] for s in on_snapshot) + f": up on the fallback snapshot, and {risk}"
        else:
            summary = f"Both sites are up, but {risk}"
    else:
        state = "ok"
        if on_snapshot:
            summary = "Both sites show units; " + " and ".join(s["name"] for s in on_snapshot) + " is serving the fallback snapshot (fresh) while Monument's live call fails"
        else:
            summary = "Both sites serve live unit inventory"
    detail = ""
    for s in sites:
        detail += f"\n- **{s['name']}** ({s['domain']}): " + "; ".join(s["notes"])
    detail += "\n- Diagnostics: " + "; ".join(diag)
    result = {"state": state, "summary": summary, "detail": detail, "checked_at": now.isoformat(),
              "integration_ok": integration_ok, "captcha_on_public_key": captcha, "feed_ok": feed_ok}
    json.dump(result, open("result.json", "w"), indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
