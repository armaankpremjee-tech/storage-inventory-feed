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
           before the damage did. Since 6 Nov 2026 it also judges leads of EVERY
           source that have not converted and that no staff member keyed (see
           judge_leads): that night the bot relabelled its leads from Abandoned
           Cart to Website and the Abandoned Cart rules alone would have closed
           the alert while it kept posting.
           The output carries counts and signature names only, never a lead's
           data, because this repository is public.
card_payments = read only through the Integration key: the ledger of both
           facilities. Active when completed card payments have stopped (no
           completed card payment for longer than the facility's PAY_SILENCE_H)
           and somebody has tried to pay since, when the silence passes
           PAY_BACKSTOP_FACTOR times the limit, or when the 1st of the month
           autopay run produced fewer completed card payments than
           PAY_MONTH_START_FLOOR by the 7th. Both times a card testing bot hit the
           Lifetime storefront in 2026 the card processor blocked the merchant and
           every card payment failed silently for days (12 to 27 Oct, found on day
           13). The silence limits are calibrated on the ledger since 20 Aug 2026
           (see judge_payments). Since 13 Nov 2026 a silence with nobody trying to
           pay is a quiet week, not an alert: that day Lifetime passed 168 h only
           because no tenant had tried to pay since 6 Nov (see payment_attempts).
           Hours and counts only in the output.
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
NEIGHBOR_MARKETING_SOURCE = "474ca270-9d82-11f1-91fb-cb3df9954417"  # on every storefront bot lead until Oct 2026
BOT_WINDOW_H = 24                # leads read per run; also covers GitHub's 2 to 7 hour gaps
BOT_BURST_MIN = 60
BOT_BURST_COUNT = 5              # Abandoned Cart leads inside any 60 minutes
BOT_DAILY_COUNT = 8              # Abandoned Cart leads in 24 hours; real ones run about 1 a week
# Rules for leads of ANY source that have not converted and that no staff member
# keyed (a storefront post cannot carry agentUserAccountUuid: Monument's shopping
# API has no such field). Calibrated by replaying every lead that exists at both
# facilities (16 Jul to 6 Nov 2026) with every lead counted as unconverted, the
# worst case. Before 31 Oct 2026 the most different people inside any 60 seconds
# was 2, the most with neither a phone nor an address inside any 24 hours was 1,
# and the most such leads inside any 24 hours was 5. With these limits the replay
# pages on 3, 4 and 6 Nov at Lifetime and never else.
BOT_MACHINE_S = 60
BOT_MACHINE_COUNT = 3            # different people inside any 60 seconds
BOT_BARE_COUNT = 4               # different people with no phone and no address in 24 hours
BOT_VOLUME_COUNT = 12            # such leads in 24 hours, whoever they claim to be
LEAD_TIMEOUT_S = 15
PAY_LOOKBACK_H = 24 * 15         # ledger rows read per run; longer than any backstop below (1.5 x the 168 h limit)
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
# A silence past PAY_SILENCE_H is a quiet week, not an alert, only when NOBODY has
# tried to pay since the last completed card payment. The ledger records completed
# payments only, but every rental record's lastPayment moves on each attempt, a
# decline included (checked live 13 Nov 2026: Cypress 18d shows its 10 Nov
# insufficient funds decline, Lifetime 76 its 12 Oct decline in the Payrix block,
# Lifetime P5 its failed 1 Nov autopay, none with a ledger payment). Any attempt
# after the last completed card payment (a decline, or even an ACH payment) keeps
# the old silence alert. lastPayment only ever moves forward, so once someone has
# tried, the alert holds until a card payment completes: a decline cannot be
# hidden by the same tenant paying by ACH later. On 13 Nov 2026 Lifetime passed
# 168 h with no attempt at all since 6 Nov (all 48 autopay rentals paid, Monument's
# Failed tab empty), a quiet week that paged anyway. Declines at move in leave no
# rental record and a change in what Monument writes to lastPayment would hide
# declines, so a silence past the backstop alerts whatever the attempts show.
PAY_BACKSTOP_FACTOR = 1.5        # Lifetime 252 h, Cypress 180 h (the October 2026 block was found by hand on day 13)
PAY_ATTEMPT_TRIES = 3            # tries of the rental records read; still unreadable means no verdict this run
PAY_ATTEMPT_RETRY_S = 5


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

    Everything but the template address applies to Abandoned Cart leads only: until
    6 Nov 2026 the storefront bot only produced those (judge_leads has the rules
    for every other source), and staff, SpareFoot or website test leads (Cypress,
    Jul and Aug 2026) must not page on a name or a phone. Calibrated on every lead at
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
    """The lead's creation time in UTC, or None when it cannot be read.

    Python 3.9 and 3.10 (the Mac) only read 0, 3 or 6 fraction digits where 3.11
    and later (the GitHub runner) read any number, so the fraction is brought to
    6 digits first: both hosts must judge the same leads the same way.
    """
    text = (lead.get("dateCreated") or "").replace("Z", "+00:00")
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _identity(lead):
    """Who a lead claims to be: email and name together, lower cased; None when it gives neither.

    Two leads are the same person only when both match. A visitor who submits a
    form twice is 1 person; a script that reuses 1 mailbox under changing names,
    or 1 name under changing mailboxes, is not. Only ever compared, never printed.
    """
    person = lead.get("primaryPerson") or {}
    email = (person.get("email") or "").strip().lower()
    name = f"{person.get('firstName') or ''} {person.get('lastName') or ''}".strip().lower()
    return (email, name) if email or name else None


def _is_bare(lead):
    """A lead with neither a phone nor an address: at most a name and an email."""
    person = lead.get("primaryPerson") or {}
    return not re.sub(r"\D", "", person.get("phone") or "") and not (person.get("addressLine1") or "").strip()


def judge_leads(leads, now):
    """Pure decision for 1 facility's leads of the last BOT_WINDOW_H hours. Counts and names only.

    Two families of rules. The Abandoned Cart rules and the template signatures
    are the October 2026 footprint and count every lead they match. The other 3
    look at every lead, whatever its source, that has not converted and that no
    staff member keyed, because the bot labels its leads as it likes (6 Nov
    2026: Website, a name and an unrelated email, nothing else, 3 to 6 of them
    5 seconds apart): a machine burst (different people inside 1 minute), leads
    with no phone and no address, and plain volume. A converted lead is a paying
    tenant and a staff keyed lead is our own work; neither counts toward these
    3 rules, so a busy move in day or a batch of logged calls cannot page.
    A lead whose date cannot be read was still returned for this window by
    Monument, so it counts toward the last 2 of them: unreadable is never clear.
    """
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
    posted = []  # (created or None, who, bare): each unconverted lead in the window that no staff member keyed
    for index, lead in enumerate(leads):
        if lead.get("leadStatus") == "CONVERTED" or lead.get("agentUserAccountUuid"):
            continue
        created = _created(lead)
        if created is not None and created < since:
            continue
        posted.append((created, _identity(lead) or ("", index), _is_bare(lead)))  # a nameless lead is its own person
    timed = sorted((row for row in posted if row[0] is not None), key=lambda row: row[0])
    machine, start = 0, 0
    for end, (when, _, _) in enumerate(timed):  # most different people inside any BOT_MACHINE_S seconds
        while when - timed[start][0] > timedelta(seconds=BOT_MACHINE_S):
            start += 1
        machine = max(machine, len({who for _, who, _ in timed[start:end + 1]}))
    bare = len({who for _, who, is_bare in posted if is_bare})
    reasons = []
    if burst >= BOT_BURST_COUNT:
        reasons.append(f"{burst} Abandoned Cart leads within {BOT_BURST_MIN} minutes")
    if len(carts) >= BOT_DAILY_COUNT:
        reasons.append(f"{len(carts)} Abandoned Cart leads in {BOT_WINDOW_H} hours")
    if flagged:
        reasons.append("template signatures: " + ", ".join(f"{k} x{v}" for k, v in sorted(flagged.items())))
    if machine >= BOT_MACHINE_COUNT:
        reasons.append(f"{machine} different people within {BOT_MACHINE_S} seconds")
    if bare >= BOT_BARE_COUNT:
        reasons.append(f"{bare} different people with no phone and no address in {BOT_WINDOW_H} hours")
    volume_only = False
    if len(posted) >= BOT_VOLUME_COUNT:
        volume_only = not reasons  # many leads and nothing else odd about them: possibly real demand
        reasons.append(f"{len(posted)} unconverted leads that staff did not key in {BOT_WINDOW_H} hours")
    return {"leads": len(leads), "abandoned_cart": len(carts), "max_in_burst": burst,
            "signatures": flagged, "unconverted": len(posted), "max_in_machine_burst": machine,
            "bare": bare, "volume_only": volume_only, "reasons": reasons, "active": bool(reasons)}


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
    odd = [n for n in hit if facilities[n]["volume_only"]]  # volume alone may be real demand: say what was measured
    sure = [n for n in hit if n not in odd]
    if sure:
        summary = " and ".join(sure) + ": fake leads are being posted into Monument's storefront"
        if odd:
            summary += "; " + " and ".join(odd) + f": an unusual number of new leads in {BOT_WINDOW_H} hours"
    elif odd:
        summary = " and ".join(odd) + f": an unusual number of new leads in {BOT_WINDOW_H} hours, possibly a bot"
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


def _utc(text):
    """A Monument ISO time in UTC, or None. The fraction is padded to 6 digits as in _created (Python 3.9 on the Mac)."""
    text = (text or "").replace("Z", "+00:00")
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def completed_card_payments(rows):
    """Distinct completed card payments (paymentUuid -> UTC time) in ledger line items.

    A payment spreads over several line items (rent, fees, tax), so it is counted
    once, dated by its EARLIEST line: Monument adds lines to old payments when the
    invoice run on the 25th applies prepaid credit or staff void a paid invoice
    (seen 25 Aug and 25 Oct 2026, 9 to 26 days after the payment), and those lines
    are not a new payment. A payment older than the lookback whose later line falls
    inside it is still dated by that line; the 1st of the month floor bounds it.
    ACH and cash never count: a blocked card merchant leaves ACH working.
    """
    seen = {}
    for row in rows:
        if row.get("transactionType") != "PAYMENT" or row.get("paymentStatus") != "COMPLETED":
            continue
        if ((row.get("payment") or {}).get("tenderType") or "").upper() != "CREDIT/DEBIT":
            continue
        uuid, when = row.get("paymentUuid"), _utc(row.get("createdAt"))
        if uuid and when and (uuid not in seen or when < seen[uuid]):
            seen[uuid] = when
    return seen


def payment_attempts(key, facility_uuid):
    """Each rental record's latest payment attempt at a facility, {rentalRecordUuid: UTC time}. Read only; raises on any doubt.

    The rental record's lastPayment (expand[lastPayment]) moves on every attempt, a
    decline included, which the ledger never records. Rentals that never paid are
    left out. No rental records, or none carrying a lastPayment, means the read or
    the expand stopped working: unknown, never quiet.
    """
    st, _, body = get(f"{INTEGRATION}/facilities/{facility_uuid}/rentalRecords/uuids", {"x-api-key": key},
                      timeout=PAY_TIMEOUT_S)
    if st != 200:
        raise RuntimeError(f"rentalRecords/uuids HTTP {st}")
    uuids = json.loads(body)
    if not isinstance(uuids, list) or not uuids:
        raise RuntimeError("rentalRecords/uuids returned no rental records")
    if len(uuids) > 6000:
        raise RuntimeError(f"{len(uuids)} rental records, more than this check reads")
    query = urllib.parse.urlencode({"expand[lastPayment][]": "true"})
    attempts = {}
    for start in range(0, len(uuids), 500):
        st, _, body = post(f"{INTEGRATION}/facilities/{facility_uuid}/rentalRecords/get_by_uuids?{query}",
                           {"unitRentalRecordUuids": uuids[start:start + 500]}, {"x-api-key": key},
                           timeout=PAY_TIMEOUT_S)
        if st != 200:
            raise RuntimeError(f"rentalRecords/get_by_uuids HTTP {st}")
        chunk = json.loads(body)
        if not isinstance(chunk, list):
            raise RuntimeError("rentalRecords/get_by_uuids did not return a list")
        for record in chunk:
            when = _utc((record.get("lastPayment") or {}).get("lastPaymentDate"))
            if record.get("rentalRecordUuid") and when:
                attempts[record["rentalRecordUuid"]] = when
    if not attempts:
        raise RuntimeError("no rental record carries a lastPayment")
    return attempts


def _rentals(n):
    return f"{n} rental has" if n == 1 else f"{n} rentals have"


def judge_payments(rows, now, name, attempts=None, attempts_error=None):
    """Pure decision for 1 facility's ledger of the last PAY_LOOKBACK_H hours. Hours and counts only.

    attempts is payment_attempts() for the facility, read only while the silence is
    past the limit and inside the backstop; attempts_error says why it could not be
    read, which gives no verdict this run (neither alert nor all clear). Called
    with neither, a silence past the limit alerts on its own, the rule before 13 Nov 2026.
    """
    times = sorted(completed_card_payments(rows).values())
    last = times[-1] if times else None
    hours = round((now - last).total_seconds() / 3600, 1) if last else None
    limit = PAY_SILENCE_H[name]
    backstop = round(limit * PAY_BACKSTOP_FACTOR)
    reasons, quiet, unknown, tried = [], None, None, None
    if hours is None:
        reasons.append(f"no completed card payment in {PAY_LOOKBACK_H} hours (limit {limit} h)")
    elif hours > backstop:
        reasons.append(f"last completed card payment {hours:g} hours ago, past the {backstop} h backstop")
    elif hours > limit:
        if attempts is not None:
            tried = sum(1 for when in attempts.values() if when > last)
            if tried:
                reasons.append(f"last completed card payment {hours:g} hours ago (limit {limit} h), and "
                               f"{_rentals(tried)} tried to pay since then")
            else:
                quiet = (f"quiet: last completed card payment {hours:g} h ago (limit {limit} h), but nobody has "
                         f"tried to pay since (alerts on the first attempt, or at {backstop} h)")
        elif attempts_error:
            unknown = (f"last completed card payment {hours:g} h ago (limit {limit} h); payment attempts could not "
                       f"be read ({attempts_error}), so no verdict this run")
        else:
            reasons.append(f"last completed card payment {hours:g} hours ago (limit {limit} h)")
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    in_window = (now.day == 1 and now.hour >= PAY_MONTH_START_FROM_H) or 1 < now.day <= PAY_MONTH_START_DAYS
    since_start = sum(1 for t in times if t >= month_start) if in_window else None
    floor = PAY_MONTH_START_FLOOR[name]
    if in_window and since_start < floor:
        reasons.append(f"{since_start} completed card payments since the 1st at 00:00 UTC, floor {floor}")
    return {"card_payments_in_lookback": len(times), "hours_since_last": hours, "limit_h": limit,
            "backstop_h": backstop, "since_month_start": since_start, "month_start_floor": floor,
            "tried_since": tried, "quiet": None if reasons else quiet, "unknown": None if reasons else unknown,
            "reasons": reasons, "active": bool(reasons)}


def card_payment_check(key, now, read=recent_payments, read_attempts=payment_attempts):
    """Card payments at both facilities. available is true only when every facility got a verdict."""
    if not key:
        return {"active": False, "available": False, "summary": "card payment check not run (Integration key missing)",
                "detail": "", "facilities": {}, "errors": ["Integration key missing"]}
    since = now - timedelta(hours=PAY_LOOKBACK_H)
    facilities, notes, errors, unread = {}, [], [], []
    for name, _, uuid in SITES:
        try:
            rows = read(key, uuid, since)
            judged = judge_payments(rows, now, name)
        except Exception as e:  # noqa: BLE001
            errors.append(name)
            notes.append(f"{name}: ledger could not be read ({str(e)[:60]})")
            continue
        hours = judged["hours_since_last"]
        if hours is not None and judged["limit_h"] < hours <= judged["backstop_h"]:
            # Only a silence between the limit and the backstop needs to know whether anyone tried to pay.
            attempts, error = None, None
            for attempt in range(PAY_ATTEMPT_TRIES):
                try:
                    attempts, error = read_attempts(key, uuid), None
                    break
                except Exception as e:  # noqa: BLE001
                    error = str(e)[:60]
                    if attempt + 1 < PAY_ATTEMPT_TRIES:
                        time.sleep(PAY_ATTEMPT_RETRY_S)
            judged = judge_payments(rows, now, name, attempts=attempts, attempts_error=error)
        facilities[name] = judged
        if judged["reasons"]:
            notes.append(f"{name}: " + "; ".join(judged["reasons"]))
        elif judged["quiet"] or judged["unknown"]:
            if judged["unknown"]:
                unread.append(name)
            notes.append(f"{name}: {judged['quiet'] or judged['unknown']}")
        else:
            note = f"{name}: last completed card payment {judged['hours_since_last']:g} h ago (limit {judged['limit_h']} h)"
            if judged["since_month_start"] is not None:
                note += f", {judged['since_month_start']} since the 1st (floor {judged['month_start_floor']})"
            notes.append(note)
    hit = [n for n, f in facilities.items() if f["active"]]
    quiet = [n for n, f in facilities.items() if f["quiet"]]
    if hit:
        summary = " and ".join(hit) + ": card payments have stopped completing in Monument"
    elif errors:
        summary = "could not read the ledger at " + " and ".join(errors) + "; card payments normal where it was read"
    elif unread:
        summary = ("could not read payment attempts at " + " and ".join(unread)
                   + ", so no verdict on its card payment silence this run")
    elif quiet:
        summary = (" and ".join(quiet) + (" is" if len(quiet) == 1 else " are") + " quiet: past the card payment "
                   "silence limit, but nobody has tried to pay since, so no block is seen")
    else:
        summary = "card payments completing normally at both facilities"
    return {"active": bool(hit), "available": not errors and not unread, "summary": summary,
            "detail": "".join(f"\n- {n}" for n in notes), "facilities": facilities, "errors": errors + unread}


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
