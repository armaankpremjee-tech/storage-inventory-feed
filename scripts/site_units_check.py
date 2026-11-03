#!/usr/bin/env python3
"""Check that both storage websites can show their unit list, and that nobody is
scripting Monument's storefront behind them.

Writes result.json for the workflow's issue step:
  state: "ok" | "degraded" | "down"
  summary: one line
  detail: markdown bullets with the evidence
  captcha_state: "on" | "off" | "unknown"  (its own alert, label storefront-captcha-off)
  bot_leads: {active, available, summary, detail}  (its own alert, label bot-leads)
  card_payments: {active, available, summary, detail}  (its own alert, label card-payments-silent)

down     = a site's /api/monument/unit-groups is not a valid inventory after 3
           tries, or its homepage is not serving rendered unit cards.
degraded = every site is up, but the safety net is at risk: the snapshot feed
           reports a failure or has not refreshed for FEED_MAX_AGE_H hours, a
           site is serving a snapshot more than SNAPSHOT_SERVED_MAX_AGE_H hours
           old, or Monument refuses the Integration key (which the feed and the
           Netlify live path both use). GitHub runs the refresh every 2 to 7
           hours in practice, not every 5 minutes, so a younger heartbeat is
           normal and is not reported.
captcha  = the public storefront key probe: 405 CAPTCHA_REQUIRED is "on", a valid
           inventory is "off" (the storefront answers scripts again), anything
           else is "unknown" and never changes an alert.
bot_leads = read only through the Integration key: leads created at either
           facility in the last 24 hours. Active when Abandoned Cart leads come
           in a burst or in a volume no real visitor produces, or any carries a
           template signature (see lead_signatures). This is the pattern behind
           the October 2026 storefront incident, and its footprint showed days
           before the damage did. The output carries counts and signature names
           only, never a lead's data, because this repository is public.
card_payments = read only through the Integration key: the ledger of both
           facilities. Active when completed card payments have stopped (no
           completed card payment for longer than the facility's PAY_SILENCE_H)
           or when the 1st of the month autopay run produced fewer completed card
           payments than PAY_MONTH_START_FLOOR by the 7th. Both times a card
           testing bot hit the Lifetime storefront in 2026 the card processor
           blocked the merchant and every card payment failed silently for days
           (12 to 27 Oct, found on day 13). The thresholds are calibrated on the
           ledger since 20 Aug 2026 (see judge_payments) so that only those
           outages would have alerted. Hours and counts only in the output.
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
FEED_MAX_AGE_H = 12              # longest observed refresh gap 7.2 h (19 to 30 Oct 2026)
SNAPSHOT_SERVED_MAX_AGE_H = 3    # only matters while a site actually serves the snapshot
NEIGHBOR_MARKETING_SOURCE = "474ca270-9d82-11f1-91fb-cb3df9954417"  # every 2026 storefront bot lead carried it
BOT_WINDOW_H = 24                # leads read per run; also covers GitHub's 2 to 7 hour gaps
BOT_BURST_MIN = 60
BOT_BURST_COUNT = 5              # Abandoned Cart leads inside any 60 minutes
BOT_DAILY_COUNT = 8              # Abandoned Cart leads in 24 hours; real ones run about 1 a week
LEAD_TIMEOUT_S = 15
PAY_LOOKBACK_H = 24 * 12         # ledger rows read per run; longer than any silence limit below
PAY_SILENCE_H = {                # hours without a completed card payment before it alerts
    # Calibrated on every completed card payment 20 Aug to 3 Nov 2026. Cypress (211
    # units): longest normal gap 93 h, the only longer one the 15 to 19 Sep outage
    # (150 h). Lifetime (98 units, mostly autopay on the 1st): normal gaps reach 126 h
    # mid month; the 15 to 19 Sep outage was 192 h and the 12 to 27 Oct merchant
    # block 364 h. Tighter limits page every quiet week.
    "Cypress Mini Storage": 120,
    "Lifetime Climate Controlled Storage": 168,
}
PAY_MONTH_START_FLOOR = {        # completed card payments expected from the 1st 00:00 UTC autopay run
    # 1 Nov 2026: Cypress 80, Lifetime 39 (46 of 51 enrolled). A blocked merchant shows 0.
    "Cypress Mini Storage": 20,
    "Lifetime Climate Controlled Storage": 10,
}
PAY_MONTH_START_FROM_H = 15      # UTC hour on the 1st by which the 3 AM Central run and its retries have posted
PAY_MONTH_START_DAYS = 7         # the floor is judged from the 1st 15:00 UTC through the 7th
PAY_TIMEOUT_S = 25


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


def probe_captcha():
    """(state, note) from the public storefront key. 1 retry before settling on unknown."""
    for attempt in range(2):
        st, _, body = get(f"{SHOPPING}/facilities/{SITES[1][2]}/unitGroups", {"x-api-key": PUBLIC_KEY})
        if b"CAPTCHA_REQUIRED" in body:
            return "on", f"HTTP {st}, captcha on"
        if st == 200 and valid_inventory(body)[0]:
            return "off", "HTTP 200 with a valid inventory, captcha OFF"
        if attempt == 0:
            time.sleep(10)
    return "unknown", f"HTTP {st}, no verdict {body[:80].decode(errors='ignore')}"


def lead_signatures(lead):
    """Names of the template signatures a lead carries; empty for a normal lead.

    Everything but the template address applies to Abandoned Cart leads only: the
    storefront bot only ever produced those, and staff, SpareFoot or website test
    leads (Cypress, Jul and Aug 2026) must not page. Calibrated on every lead at
    both facilities since 1 Sep 2026: 0 real leads match. The names never carry
    the lead's own data.
    """
    person = lead.get("primaryPerson") or {}
    phone = re.sub(r"\D", "", person.get("phone") or "")
    name = f"{person.get('firstName') or ''} {person.get('lastName') or ''}".strip().lower()
    email = (person.get("email") or "").lower()
    found = []
    if "street 332" in (person.get("addressLine1") or "").lower():
        found.append("template address")
    if lead.get("leadSource") != "Abandoned Cart":
        return found
    if lead.get("leadMarketingSourceUuid") == NEIGHBOR_MARKETING_SOURCE:
        found.append("Neighbor.com marketing source")
    if phone and len(phone) != 10:
        # Monument cuts phones to 10 characters, so generated strings arrive with 8 or 9 digits.
        found.append(f"phone with {len(phone)} digits")
    if phone and (phone.endswith("1234567") or len(set(phone)) <= 2 or phone[3:] in ("5551234", "0000000")):
        found.append("test phone number")
    if phone.startswith("616"):
        found.append("616 phone")
    if name in ("john doe", "jane doe") or re.search(r"\btest\b", name) or re.search(r"\btest\b", email):
        found.append("test name or email")
    return found


def recent_leads(key, facility_uuid, since):
    """Leads created at a facility since the given UTC time, hydrated. Read only; raises on any doubt."""
    query = urllib.parse.urlencode({"createdAt.greaterOrEqual": since.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    st, _, body = get(f"{INTEGRATION}/facilities/{facility_uuid}/leads/uuids?{query}", {"x-api-key": key},
                      timeout=LEAD_TIMEOUT_S)
    if st != 200:
        raise RuntimeError(f"leads/uuids HTTP {st}")
    uuids = json.loads(body)
    if not isinstance(uuids, list):
        raise RuntimeError("leads/uuids did not return a list")
    leads = []
    for start in range(0, min(len(uuids), 3000), 1000):  # get_by_uuids takes at most 1,000
        st, _, body = post(f"{INTEGRATION}/facilities/{facility_uuid}/leads/get_by_uuids",
                           {"leadUuids": uuids[start:start + 1000]}, {"x-api-key": key}, timeout=LEAD_TIMEOUT_S)
        if st != 200:
            raise RuntimeError(f"leads/get_by_uuids HTTP {st}")
        chunk = json.loads(body)
        if not isinstance(chunk, list):
            raise RuntimeError("leads/get_by_uuids did not return a list")
        leads += chunk
    return leads


def _created(lead):
    try:
        return datetime.fromisoformat((lead.get("dateCreated") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def judge_leads(leads, now):
    """Pure decision for 1 facility's leads of the last BOT_WINDOW_H hours. Counts and names only."""
    since = now - timedelta(hours=BOT_WINDOW_H)
    carts = sorted(t for t in (_created(lead) for lead in leads if lead.get("leadSource") == "Abandoned Cart")
                   if t is not None and t >= since)
    burst, start = 0, 0
    for end, when in enumerate(carts):  # most Abandoned Cart leads inside any BOT_BURST_MIN minutes
        while when - carts[start] > timedelta(minutes=BOT_BURST_MIN):
            start += 1
        burst = max(burst, end - start + 1)
    flagged = {}
    for lead in leads:
        created = _created(lead)
        if created is not None and created < since:
            continue
        for sig in lead_signatures(lead):
            flagged[sig] = flagged.get(sig, 0) + 1
    reasons = []
    if burst >= BOT_BURST_COUNT:
        reasons.append(f"{burst} Abandoned Cart leads within {BOT_BURST_MIN} minutes")
    if len(carts) >= BOT_DAILY_COUNT:
        reasons.append(f"{len(carts)} Abandoned Cart leads in {BOT_WINDOW_H} hours")
    if flagged:
        reasons.append("template signatures: " + ", ".join(f"{k} x{v}" for k, v in sorted(flagged.items())))
    return {"leads": len(leads), "abandoned_cart": len(carts), "max_in_burst": burst,
            "signatures": flagged, "reasons": reasons, "active": bool(reasons)}


def bot_lead_check(key, now, read=recent_leads):
    """The tripwire across both facilities. available is true only when EVERY facility was read."""
    if not key:
        return {"active": False, "available": False, "summary": "bot lead tripwire not run (Integration key missing)",
                "detail": "", "facilities": {}, "errors": ["Integration key missing"]}
    since = now - timedelta(hours=BOT_WINDOW_H)
    facilities, notes, errors = {}, [], []
    for name, _, uuid in SITES:
        try:
            judged = judge_leads(read(key, uuid, since), now)
        except Exception as e:  # noqa: BLE001
            errors.append(name)
            notes.append(f"{name}: leads could not be read ({str(e)[:60]})")
            continue
        facilities[name] = judged
        notes.append(f"{name}: {judged['leads']} leads in {BOT_WINDOW_H} h, {judged['abandoned_cart']} Abandoned Cart, "
                     + ("; ".join(judged["reasons"]) if judged["reasons"] else "no bot pattern"))
    hit = [n for n, f in facilities.items() if f["active"]]
    if hit:
        summary = " and ".join(hit) + ": fake leads are being posted into Monument's storefront"
    elif errors:
        summary = "could not read leads at " + " and ".join(errors) + "; no bot pattern where they were read"
    else:
        summary = f"no bot leads at either facility in the last {BOT_WINDOW_H} hours"
    return {"active": bool(hit), "available": not errors, "summary": summary,
            "detail": "".join(f"\n- {n}" for n in notes), "facilities": facilities, "errors": errors}


def recent_payments(key, facility_uuid, since):
    """Ledger line items created at a facility since the given UTC time. Read only; raises on any doubt."""
    query = urllib.parse.urlencode({"createdAt.greaterOrEqual": since.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
    st, _, body = get(f"{INTEGRATION}/facilities/{facility_uuid}/ledgerEntries/uuids?{query}", {"x-api-key": key},
                      timeout=PAY_TIMEOUT_S)
    if st != 200:
        raise RuntimeError(f"ledgerEntries/uuids HTTP {st}")
    uuids = json.loads(body)
    if not isinstance(uuids, list):
        raise RuntimeError("ledgerEntries/uuids did not return a list")
    rows = []
    for start in range(0, min(len(uuids), 6000), 1000):  # get_by_uuids takes at most 1,000
        st, _, body = post(f"{INTEGRATION}/facilities/{facility_uuid}/ledgerEntries/get_by_uuids",
                           {"lineItemLedgerUuids": uuids[start:start + 1000]}, {"x-api-key": key}, timeout=PAY_TIMEOUT_S)
        if st != 200:
            raise RuntimeError(f"ledgerEntries/get_by_uuids HTTP {st}")
        chunk = json.loads(body)
        if not isinstance(chunk, list):
            raise RuntimeError("ledgerEntries/get_by_uuids did not return a list")
        rows += chunk
    return rows


def completed_card_payments(rows):
    """Distinct completed card payments (paymentUuid -> UTC time) in ledger line items.

    A payment spreads over several line items (rent, fees, tax), so it is counted
    once. ACH and cash never count: a blocked card merchant leaves ACH working.
    """
    seen = {}
    for row in rows:
        if row.get("transactionType") != "PAYMENT" or row.get("paymentStatus") != "COMPLETED":
            continue
        if ((row.get("payment") or {}).get("tenderType") or "").upper() != "CREDIT/DEBIT":
            continue
        uuid = row.get("paymentUuid")
        try:
            when = datetime.fromisoformat((row.get("createdAt") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if uuid and (uuid not in seen or when > seen[uuid]):
            seen[uuid] = when
    return seen


def judge_payments(rows, now, name):
    """Pure decision for 1 facility's ledger of the last PAY_LOOKBACK_H hours. Hours and counts only."""
    times = sorted(completed_card_payments(rows).values())
    last = times[-1] if times else None
    hours = round((now - last).total_seconds() / 3600, 1) if last else None
    limit = PAY_SILENCE_H[name]
    reasons = []
    if hours is None:
        reasons.append(f"no completed card payment in {PAY_LOOKBACK_H} hours (limit {limit} h)")
    elif hours > limit:
        reasons.append(f"last completed card payment {hours:g} hours ago (limit {limit} h)")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    in_window = (now.day == 1 and now.hour >= PAY_MONTH_START_FROM_H) or 1 < now.day <= PAY_MONTH_START_DAYS
    since_start = sum(1 for t in times if t >= month_start) if in_window else None
    floor = PAY_MONTH_START_FLOOR[name]
    if in_window and since_start < floor:
        reasons.append(f"{since_start} completed card payments since the 1st at 00:00 UTC, floor {floor}")
    return {"card_payments_in_lookback": len(times), "hours_since_last": hours, "limit_h": limit,
            "since_month_start": since_start, "month_start_floor": floor,
            "reasons": reasons, "active": bool(reasons)}


def card_payment_check(key, now, read=recent_payments):
    """Card payments at both facilities. available is true only when EVERY facility was read."""
    if not key:
        return {"active": False, "available": False, "summary": "card payment check not run (Integration key missing)",
                "detail": "", "facilities": {}, "errors": ["Integration key missing"]}
    since = now - timedelta(hours=PAY_LOOKBACK_H)
    facilities, notes, errors = {}, [], []
    for name, _, uuid in SITES:
        try:
            judged = judge_payments(read(key, uuid, since), now, name)
        except Exception as e:  # noqa: BLE001
            errors.append(name)
            notes.append(f"{name}: ledger could not be read ({str(e)[:60]})")
            continue
        facilities[name] = judged
        if judged["reasons"]:
            notes.append(f"{name}: " + "; ".join(judged["reasons"]))
        else:
            note = f"{name}: last completed card payment {judged['hours_since_last']:g} h ago (limit {judged['limit_h']} h)"
            if judged["since_month_start"] is not None:
                note += f", {judged['since_month_start']} since the 1st (floor {judged['month_start_floor']})"
            notes.append(note)
    hit = [n for n, f in facilities.items() if f["active"]]
    if hit:
        summary = " and ".join(hit) + ": card payments have stopped completing in Monument"
    elif errors:
        summary = "could not read the ledger at " + " and ".join(errors) + "; card payments normal where it was read"
    else:
        summary = "card payments completing normally at both facilities"
    return {"active": bool(hit), "available": not errors, "summary": summary,
            "detail": "".join(f"\n- {n}" for n in notes), "facilities": facilities, "errors": errors}


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
    captcha_state, captcha_note = probe_captcha()
    diag.append(f"Monument direct with the public storefront key: {captcha_note}")
    st, _, body = get(FEED_STATUS)
    feed_ok, feed_age_min = False, None
    feed_note = f"snapshot feed status.json: HTTP {st}"
    if st == 200:
        try:
            s = json.loads(body)
            age = (now - datetime.fromisoformat(s["checked_at"])).total_seconds()
            feed_age_min = int(age // 60)
            feed_ok = bool(s.get("ok")) and age < FEED_MAX_AGE_H * 3600
            feed_note += f", ok={s.get('ok')}, heartbeat {feed_age_min} min old"
        except Exception as e:  # noqa: BLE001
            feed_note += f", unreadable ({e!r})"
    diag.append(feed_note)
    bots = bot_lead_check(key, now)
    diag.append("bot lead tripwire: " + bots["summary"])
    pays = card_payment_check(key, now)
    diag.append("card payments: " + pays["summary"])

    down = [s for s in sites if not (s["api_ok"] and s["cards_ok"])]
    on_snapshot = [s for s in sites if s["api_ok"] and s["source"] == "snapshot"]
    risks = []
    if integration_ok is False:
        risks.append("Monument refuses the Integration key")
    if not feed_ok:
        risks.append(f"the fallback snapshot feed is failing or has not refreshed for {FEED_MAX_AGE_H} hours")
    elif on_snapshot and feed_age_min is not None and feed_age_min > SNAPSHOT_SERVED_MAX_AGE_H * 60:
        risks.append(f"a site is serving a snapshot {feed_age_min // 60} hours old")
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
              "integration_ok": integration_ok, "captcha_on_public_key": captcha_state == "on",
              "captcha_state": captcha_state, "feed_ok": feed_ok, "feed_age_min": feed_age_min,
              "bot_leads": {k: bots[k] for k in ("active", "available", "summary", "detail")},
              "card_payments": {k: pays[k] for k in ("active", "available", "summary", "detail")}}
    json.dump(result, open("result.json", "w"), indent=2)
    # The log of a public repository: no lead counts, only the 1 line verdicts.
    printed = {k: v for k, v in result.items() if k not in ("bot_leads", "card_payments")}
    printed["bot_leads"] = {k: bots[k] for k in ("active", "available", "summary")}
    printed["card_payments"] = {k: pays[k] for k in ("active", "available", "summary")}
    print(json.dumps(printed, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
