#!/usr/bin/env python3
"""Check that both storage websites can show their unit list, and that nobody is
scripting Monument's storefront behind them.

Writes result.json for the workflow's issue step:
  state: "ok" | "degraded" | "down"
  summary: one line
  detail: markdown bullets with the evidence
  bot_leads: {active, persist, summary, detail}, a separate alert (label bot-leads)

down     = a site's /api/monument/unit-groups is not a valid inventory after 3
           tries, or its homepage is not serving rendered unit cards.
degraded = every site is up, but a protection is at risk: the snapshot feed is
           stale or failing, Monument refuses the Integration key (which the
           feed and the Netlify live path both use), or Monument's storefront
           captcha is OFF. The captcha matters because on 9 to 12 Oct 2026 a
           script posted 476 fake "Abandoned Cart" leads through the public
           storefront key, Payrix flagged card testing on the Lifetime merchant
           and blocked it for 15 days, and the captcha Monument put up on 12 Oct
           is the only thing that has stopped that bot. A site serving the
           snapshot while the feed is fresh is reported but is not an alert:
           customers see counts at most a few minutes old.
bot_leads = read only through the Integration key: leads created at either
           facility in the last 2 hours. Active when 5 or more Abandoned Cart
           leads arrived in 60 minutes, or any lead carries a known bot
           signature (the Neighbor.com marketing source, a phone that is not 10
           digits, the "Street 332" template address, a test phone or name, a
           616 phone on an abandoned cart, a loctain email). The bot's lead
           footprint began 9 Oct 2026, 3 days before the Payrix block, so this
           is the earliest warning we own. The issue text carries counts and
           signature names only, never a lead's own data: the repository that
           hosts the issues is public.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SITES = [
    ("Cypress Mini Storage", "cypresslouisiana.com", "29c2b9eb-699f-11f1-989e-e99e7c079122"),
    ("Lifetime Climate Controlled Storage", "lifetimeccs.com", "29c2b9e2-699f-11f1-989e-e99e7c079122"),
]
SHOPPING = "https://public-api.prd.monument.io/shopping/storagegiant/v1"
INTEGRATION = "https://public-api.prd.monument.io/api/storagegiant/portfolios/v1"
PUBLIC_KEY = "V6AHJGWpyQakzctySeh9NxBhw29lInd8j8541hj9"  # Monument's own storefront key, in their bundle
FEED_STATUS = "https://raw.githubusercontent.com/armaankpremjee-tech/storage-inventory-feed/main/status.json"
UA = "fleet-watchdog site-units-feed (github actions)"
NEIGHBOR_MARKETING_SOURCE = "474ca270-9d82-11f1-91fb-cb3df9954417"  # every 2026 bot lead carried it
BOT_WINDOW_MIN = 120      # leads read per run; an alert persists while this window still shows the burst
BOT_BURST_MIN = 60        # 5 or more Abandoned Cart leads inside this many minutes is a burst
BOT_BURST_COUNT = 5       # Cypress logs about 1 real abandoned cart a week


def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"user-agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # noqa: BLE001
        return 0, {}, repr(e).encode()


def post(url, body, headers=None, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"user-agent": UA, "content-type": "application/json", **(headers or {})})
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


def lead_signatures(lead):
    """Names of the bot signatures a lead carries; empty for a normal lead.

    Calibrated on every Lifetime and Cypress lead since 1 Sep 2026 (59 real ones
    matched nothing; the 6 test records at Lifetime matched 616, loctain, test
    or 1234567). The names never carry the lead's own data.
    """
    person = lead.get("primaryPerson") or {}
    phone = re.sub(r"\D", "", person.get("phone") or "")
    address = (person.get("addressLine1") or "").lower()
    name = f"{person.get('firstName') or ''} {person.get('lastName') or ''}".strip().lower()
    email = (person.get("email") or "").lower()
    abandoned = lead.get("leadSource") == "Abandoned Cart"
    found = []
    if lead.get("leadMarketingSourceUuid") == NEIGHBOR_MARKETING_SOURCE:
        found.append("Neighbor.com marketing source")
    if abandoned and phone and len(phone) != 10:
        # Monument cuts phones to 10 characters, so Faker strings arrive with 8 or 9 digits.
        # Scoped to abandoned carts: a partner key (SpareFoot, Emma) may post a +1 number.
        found.append(f"phone with {len(phone)} digits")
    if "street 332" in address:
        found.append("Street 332 template address")
    if phone and (phone.endswith("1234567") or len(set(phone)) <= 2 or phone[3:] in ("5551234", "0000000")):
        found.append("test phone number")
    if abandoned and phone.startswith("616"):
        found.append("616 phone on an abandoned cart")
    if name in ("john doe", "jane doe") or re.search(r"\btest\b", name) or re.search(r"\btest\b|loctain", email):
        found.append("test name or email")
    return found


def recent_leads(key, facility_uuid, since):
    """Leads created at a facility since the given UTC time, hydrated. Read only."""
    query = urllib.parse.urlencode({"createdAt.greaterOrEqual": since.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    st, _, body = get(f"{INTEGRATION}/facilities/{facility_uuid}/leads/uuids?{query}", {"x-api-key": key})
    if st != 200:
        raise RuntimeError(f"leads/uuids HTTP {st} {body[:80].decode(errors='ignore')}")
    uuids = json.loads(body)
    if not isinstance(uuids, list):
        raise RuntimeError("leads/uuids did not return a list")
    if not uuids:
        return []
    st, _, body = post(f"{INTEGRATION}/facilities/{facility_uuid}/leads/get_by_uuids",
                       {"leadUuids": uuids[:1000]}, {"x-api-key": key})
    if st != 200:
        raise RuntimeError(f"leads/get_by_uuids HTTP {st} {body[:80].decode(errors='ignore')}")
    leads = json.loads(body)
    return leads if isinstance(leads, list) else []


def judge_leads(leads, now):
    """Pure decision for 1 facility's recent leads. Counts and signature names only."""
    burst_since = now - timedelta(minutes=BOT_BURST_MIN)
    abandoned_window, abandoned_burst, flagged = 0, 0, {}
    for lead in leads:
        created = lead.get("dateCreated") or ""
        try:
            when = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if lead.get("leadSource") == "Abandoned Cart":
            abandoned_window += 1
            if when is not None and when >= burst_since:
                abandoned_burst += 1
        for sig in lead_signatures(lead):
            flagged[sig] = flagged.get(sig, 0) + 1
    burst = abandoned_burst >= BOT_BURST_COUNT
    return {"leads": len(leads), "abandoned_cart": abandoned_window, "abandoned_cart_last_hour": abandoned_burst,
            "signatures": flagged, "active": burst or bool(flagged),
            "persist": abandoned_window >= BOT_BURST_COUNT or bool(flagged)}


def bot_lead_check(key, now):
    """The tripwire across both facilities. Unavailable (never a false alarm) without the key."""
    if not key:
        return {"active": False, "persist": False, "available": False,
                "summary": "bot lead tripwire not run (Integration key missing)", "detail": "", "facilities": {}}
    since = now - timedelta(minutes=BOT_WINDOW_MIN)
    facilities, notes, errors = {}, [], []
    for name, _, uuid in SITES:
        try:
            judged = judge_leads(recent_leads(key, uuid, since), now)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {str(e)[:120]}")
            continue
        facilities[name] = judged
        sigs = ", ".join(f"{k} x{v}" for k, v in sorted(judged["signatures"].items())) or "none"
        notes.append(f"{name}: {judged['leads']} leads in the last {BOT_WINDOW_MIN} min, "
                     f"{judged['abandoned_cart']} Abandoned Cart ({judged['abandoned_cart_last_hour']} in the last hour), "
                     f"bot signatures: {sigs}")
    active = any(f["active"] for f in facilities.values())
    persist = any(f["persist"] for f in facilities.values())
    hit = [n for n, f in facilities.items() if f["active"] or f["persist"]]
    if active or persist:
        summary = (" and ".join(hit) + ": a script is posting fake leads into Monument's storefront "
                   "(the pattern that got Lifetime's merchant blocked for card testing on 12 Oct 2026)")
    else:
        summary = "no bot leads at either facility in the last 2 hours"
    detail = "".join(f"\n- {n}" for n in notes + [f"could not read leads: {e}" for e in errors])
    return {"active": active, "persist": persist, "available": not errors or bool(facilities),
            "summary": summary, "detail": detail, "facilities": facilities, "errors": errors}


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
    if b"CAPTCHA_REQUIRED" in body:
        captcha_state = "on"
    elif st == 200 and valid_inventory(body)[0]:
        captcha_state = "off"       # the public key answers freely again: the bot's door is open
    else:
        captcha_state = "unknown"   # network error, key revoked, or a new refusal: not a captcha verdict
    captcha = captcha_state == "on"
    diag.append(f"Monument direct with the public storefront key: HTTP {st}, captcha {captcha_state}"
                + (" (expected since 12 Oct 2026)" if captcha else "") + ("" if st in (200, 405) else f" {body[:100].decode(errors='ignore')}"))
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
    bots = bot_lead_check(key, now)
    diag.append("bot lead tripwire: " + bots["summary"])

    down = [s for s in sites if not (s["api_ok"] and s["cards_ok"])]
    on_snapshot = [s for s in sites if s["api_ok"] and s["source"] == "snapshot"]
    risks = []
    if captcha_state == "off":
        risks.append("Monument's storefront captcha is OFF: the bot that got Lifetime's merchant blocked on 12 Oct can post leads again")
    if integration_ok is False:
        risks.append("Monument refuses the Integration key")
    if not feed_ok:
        risks.append("the fallback snapshot feed is stale or failing")
    if down:
        state = "down"
        names = " and ".join(s["name"] for s in down)
        summary = f"{names}: the unit list is NOT loading for customers"
    elif risks:
        state = "degraded"
        risk = "; ".join(risks)
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
              "integration_ok": integration_ok, "captcha_on_public_key": captcha, "captcha_state": captcha_state,
              "feed_ok": feed_ok,
              "bot_leads": {k: bots[k] for k in ("active", "persist", "available", "summary", "detail", "facilities")}}
    json.dump(result, open("result.json", "w"), indent=2)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
