#!/usr/bin/env python3
"""Synthetic checks for the online rentals rule in site_units_check.py (no network).

Run: python3 scripts/test_online_rentals.py
Every identity here is invented (example.com). The limit was calibrated by replaying
every lead at both facilities (20 Aug to 13 Nov 2026) every 15 minutes: only the
stall that began on 6 Nov 2026 alerted. That replay reads live data and is not part
of this file; the gaps it found (hours only) are below.
"""
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import site_units_check as c  # noqa: E402

CY, LT = "Cypress Mini Storage", "Lifetime Climate Controlled Storage"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
LIMIT = c.ONLINE_SILENCE_H
ARRIVING = "online rentals arriving"
SILENT = "No online rentals or abandoned carts at either facility"


def at(when, who, source="Website", status="CONVERTED", phone="9405550142", address="12 Main St", agent=None,
       agent_name=None):
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
    return {"leadSource": source, "leadStatus": status, "dateCreated": stamp,
            "agentUserAccountUuid": agent, "agentName": agent_name if agent_name is not None else ("Staff" if agent else None),
            "primaryPerson": {"firstName": "Person", "lastName": str(who), "email": f"p{who}@example.com",
                              "phone": phone, "addressLine1": address}}


def ago(hours, who, **kw):
    return at(NOW - timedelta(hours=hours), who, **kw)


def judge(cypress=(), lifetime=(), now=NOW):
    return c.judge_online_rentals({CY: c.classify_online_leads(list(cypress)),
                                   LT: c.classify_online_leads(list(lifetime))}, now)


def reader(rows, broken=(), seen=None):
    def read(key, uuid, since):
        name = next(n for n, _, u in c.SITES if u == uuid)
        if seen is not None:
            seen.append(since)
        if name in broken:
            raise RuntimeError("HTTP 503 " + "x" * 60 + " leak@example.com")
        return list(rows.get(name, ()))
    return read


checks = 0


def expect(name, cond):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    print("ok:", name)


# calibration: the replay of 20 Aug to 13 Nov 2026, hours only. The 10 longest gaps between consecutive self serve
# leads, both facilities together, before the stall, and the stall that began 6 Nov 02:21Z, measured 13 Nov 21:12Z.
NORMAL_GAPS_H = [176.6, 157.6, 123.3, 106.2, 102.1, 80.2, 68.8, 66.2, 65.0, 64.7]
STALL_H = 186.8
expect("the limit is the smallest whole number of hours above the longest normal gap",
       LIMIT == math.floor(max(NORMAL_GAPS_H)) + 1 == 177)
expect("the 6 Nov 2026 stall is past the limit", STALL_H > LIMIT)
expect("the lookback holds far more than the limit", c.ONLINE_LOOKBACK_H >= 2 * LIMIT)
train, when = [], NOW - timedelta(hours=sum(NORMAL_GAPS_H) + STALL_H)
for gap in NORMAL_GAPS_H:
    train.append(when)
    when += timedelta(hours=gap)
train.append(when)  # the last lead before the stall
verdicts = []
for i in range(len(train)):
    moment = train[i + 1] - timedelta(minutes=1) if i + 1 < len(train) else NOW  # the end of each silence
    seen = [at(t, n) for n, t in enumerate(train[:i + 1])]
    verdicts.append(judge(seen[0::2], seen[1::2], now=moment)["active"])
expect("replayed at the end of every silence, only the stall alerts", verdicts == [False] * len(NORMAL_GAPS_H) + [True])

# quiet: the newest self serve lead at either facility decides
j = judge([ago(20, 1)], [])
expect("a Cypress online rental 20 h ago and nothing at Lifetime: inactive", not j["active"] and j["hours_since_last"] == 20)
j = judge([ago(300, 1)], [ago(10, 2)])
expect("judged together: Cypress quiet 300 h, Lifetime 10 h ago: inactive", not j["active"] and j["hours_since_last"] == 10)
j = judge([ago(LIMIT, 1)], [])
expect("exactly the limit: inactive", not j["active"])
j = judge([ago(LIMIT + 0.1, 1)], [ago(LIMIT + 5, 2)])
expect("just past the limit at both: active", j["active"] and j["hours_since_last"] == LIMIT + 0.1)
j = judge([ago(5, 1, source="Abandoned Cart", status="OPEN")], [])
expect("an abandoned cart counts like an online rental", not j["active"])
j = judge([ago(5, 1, source="Abandoned Cart", status="LOST")], [ago(400, 2)])
expect("a lost abandoned cart counts too", not j["active"] and j["counted"] == 2)
j = judge([], [])
expect("nothing at all in the lookback: active", j["active"] and j["hours_since_last"] is None)

# staff only leads never count, nor do other sources
staff = [ago(2, "s1", agent="staff-uuid"), ago(3, "s2", agent=None, agent_name="Staff"),
         ago(4, "s3", agent="staff-uuid", agent_name="")]
j = judge(staff + [ago(200, 9)], staff)
expect("Website leads keyed by staff do not count (agentName or agentUserAccountUuid)",
       j["active"] and j["hours_since_last"] == 200 and j["counted"] == 1)
f = c.classify_online_leads(staff)
expect("staff keyed leads are counted as staff, not as self serve", f["staff"] == 3 and not f["counted"])
others = [ago(1, "o1", source="3rd Party"), ago(2, "o2", source="Phone"), ago(3, "o3", source="Other")]
f = c.classify_online_leads(others)
expect("3rd Party, Phone and Other leads do not count", f["other"] == 3 and not f["counted"])
expect("staff and other leads alone: active", judge(staff + others, staff)["active"])

# bot leads never count as rentals arriving
signed = ago(1, "b1", source="Abandoned Cart", phone="6165550142")
f = c.classify_online_leads([signed])
expect("an abandoned cart with a bot signature does not count", f["bot"] == 1 and not f["counted"])
night = [ago(5 - i * 5 / 3600, f"n{i}", status="OPEN", phone=None, address=None) for i in range(3)]
f = c.classify_online_leads(night)
expect("the 6 Nov 2026 shape (3 people 5 seconds apart, name and email only): none counts, the first included",
       f["bot"] == 3 and not f["counted"])
first = ago(30, "f0", status="OPEN")
burst = [ago(29 - i * 5 / 3600, f"f{i + 1}", status="OPEN", phone=None, address=None) for i in range(3)]
expect("the first lead of a flood counts on its own", c.classify_online_leads([first])["counted"])
expect("and stops counting once the burst behind it arrives", not c.classify_online_leads([first] + burst)["counted"])
carts = [ago(2.5 * i + 1, f"c{i}", source="Abandoned Cart", status="OPEN") for i in range(8)]
f = c.classify_online_leads(carts)
expect("8 abandoned carts in a day (the October rule): none counts", f["bot"] == 8 and not f["counted"])
expect("so a flood alone leaves the alert active", judge(carts + night, [signed])["active"])
real, test_record = ago(30, "r1"), ago(25, "t1", source="Abandoned Cart", status="OPEN", phone="6161234567")
f = c.classify_online_leads([real, test_record])
expect("a real move in 5 h before a lone signed test record still counts (12 Oct 2026)",
       len(f["counted"]) == 1 and f["bot"] == 1)
f = c.classify_online_leads([ago(20, "r2", status="OPEN")] + carts)
expect("an open lead inside a flood's window does not count either", not f["counted"] and f["bot"] == 9)
f = c.classify_online_leads([ago(5 - 700 / 3600, "tenant")] + night + carts)
expect("a lead that converted inside a flood's window still counts (a paying tenant, the 6 Nov 2026 night)",
       len(f["counted"]) == 1 and f["bot"] == 11)
signed_tenant = ago(3, "st", source="Abandoned Cart", phone="6165550142")
expect("a converted lead with a bot signature of its own still does not count",
       c.classify_online_leads([signed_tenant])["bot"] == 1)
flood = [ago(72 - i / 30, f"x{i}", source="Abandoned Cart", status="OPEN", phone=None, address=None)
         for i in range(2000)]
started = time.time()
f = c.classify_online_leads(flood + [ago(100, "r3", status="OPEN")])
took = time.time() - started
expect(f"a 2,000 lead flood is judged in {took:.1f} s (under 10) and the real lead 28 h before it counts",
       took < 10 and len(f["counted"]) == 1 and f["bot"] == 2000)

# unreadable is unknown, never clear: available false, so an open issue stays open and none opens
r = c.online_rentals_check("k", NOW, read=reader({CY: [ago(400, 1)]}, broken={LT}))
expect("Lifetime unreadable with Cypress silent: no alert, not available",
       not r["active"] and not r["available"] and r["summary"].startswith(f"could not read leads at {LT}")
       and "no verdict" in r["summary"])
expect("an error message is cut before it can carry data", "leak@example.com" not in r["detail"] + r["summary"])
r = c.online_rentals_check("k", NOW, read=reader({CY: [ago(1, 1)]}, broken={LT}))
expect("Lifetime unreadable with a fresh lead at Cypress: still no verdict, never clear", not r["available"])
r = c.online_rentals_check("", NOW)
expect("missing key: not available", not r["available"] and not r["active"] and "not run" in r["summary"])
r = c.online_rentals_check("k", NOW, read=reader({CY: [ago(1, 1)] * c.ONLINE_MAX_LEADS, LT: []}))
expect("3,000 leads at a facility (the read is cut there): no verdict", not r["available"] and "more than" in r["detail"])
undated = ago(1, "u1")
undated["dateCreated"] = "not a date"
r = c.online_rentals_check("k", NOW, read=reader({CY: [undated, ago(300, 2)], LT: []}))
expect("a self serve lead with an unreadable date does not count, and is reported",
       r["active"] and r["available"] and "1 without a readable date" in r["detail"])

# the wrapper: live shape, recovery, and nothing that identifies anyone
seen = []
r = c.online_rentals_check("k", NOW, read=reader({CY: [ago(310, 1)] + staff, LT: [ago(187, 2), signed]}, seen=seen))
expect("the 13 Nov 2026 shape: active, available, hours in the summary",
       r["active"] and r["available"] and r["summary"] == f"{SILENT} for 187 hours (limit {LIMIT} h)"
       and r["hours_since_last"] == 187 and r["limit_h"] == LIMIT)
expect("leads are read from the start of the lookback at both facilities",
       seen == [NOW - timedelta(hours=c.ONLINE_LOOKBACK_H)] * 2)
expect("detail: hours and counts per facility, skipped leads named by kind",
       f"- {CY}: newest self serve lead 310 h ago, 1 in 30 days (not counted: 3 keyed by staff)" in r["detail"]
       and f"- {LT}: newest self serve lead 187 h ago, 1 in 30 days (not counted: 1 flagged by the bot lead rules)"
       in r["detail"])
expect("output carries no identities", all(s not in r["detail"] + r["summary"] for s in ("example.com", "Person", "Staff",
                                                                                         "616", "9405550142")))
r = c.online_rentals_check("k", NOW, read=reader({CY: [], LT: []}))
expect("nothing in the lookback: active, says so in days", r["active"] and r["summary"].startswith(f"{SILENT} in 30 days"))
r = c.online_rentals_check("k", NOW, read=reader({CY: [ago(310, 1), ago(0.2, 3)], LT: [ago(187, 2)]}))
expect("recovery: a new online rental clears it (inactive and available, so the issue closes)",
       not r["active"] and r["available"] and r["summary"] == f"{ARRIVING}: newest self serve lead 0.2 h ago (limit {LIMIT} h)")
r = c.online_rentals_check("k", NOW, read=reader({CY: [ago(310, 1), ago(0.2, 3, agent="staff-uuid")], LT: [ago(187, 2)]}))
expect("a lead staff keyed does not clear it", r["active"])
r = c.online_rentals_check("k", NOW, read=reader({CY: [ago(310, 1)] + night, LT: [ago(187, 2)]}))
expect("a bot burst does not clear it", r["active"])

print(f"all {checks} checks passed")
