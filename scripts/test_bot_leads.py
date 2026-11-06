#!/usr/bin/env python3
"""Synthetic checks for the bot lead rules in site_units_check.py (no network).

Run: python3 scripts/test_bot_leads.py
Every identity here is invented (example.com). The thresholds were calibrated by
replaying every lead that exists at both facilities (16 Jul to 6 Nov 2026): the
rules for unconverted leads of any source fire only on the bot's own nights.
That replay reads live data and is not part of this file.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import site_units_check as c  # noqa: E402

CY, LT = "Cypress Mini Storage", "Lifetime Climate Controlled Storage"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
HOUR = 3600
MACHINE = "different people within 60 seconds"
BARE = "different people with no phone and no address in 24 hours"
VOLUME = "unconverted leads that staff did not key in 24 hours"


def lead(seconds_ago, who, source="Website", status="OPEN", phone="9405550142", address="12 Main St",
         email="", first="Person", agent=None):
    when = NOW - timedelta(seconds=seconds_ago)
    row = {"leadSource": source, "leadStatus": status,
           "dateCreated": when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z",
           "primaryPerson": {"firstName": first, "lastName": str(who),
                             "email": f"p{who}@example.com" if email == "" else email,
                             "phone": phone, "addressLine1": address}}
    if agent:
        row["agentUserAccountUuid"] = agent
    return row


def bare(seconds_ago, who, **kw):
    return lead(seconds_ago, who, phone=None, address=None, **kw)


def has(judged, text):
    return any(text in reason for reason in judged["reasons"])


checks = 0


def expect(name, cond):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    print("ok:", name)


# nothing, and ordinary days, never page
expect("no leads: inactive", not c.judge_leads([], NOW)["active"])
j = c.judge_leads([lead(2 * HOUR, 1), lead(9 * HOUR, 2, source="3rd Party"), lead(20 * HOUR, 3, source="Other")], NOW)
expect("3 ordinary leads in a day: inactive", not j["active"] and j["unconverted"] == 3)

# the October 2026 footprint is unchanged
j = c.judge_leads([lead(600 * i, i, source="Abandoned Cart") for i in range(5)], NOW)
expect("5 Abandoned Cart leads within 60 minutes: active", j["reasons"] == ["5 Abandoned Cart leads within 60 minutes"])
j = c.judge_leads([lead(1000 * i, i, source="Abandoned Cart") for i in range(4)], NOW)
expect("4 Abandoned Cart leads within 60 minutes: inactive", not j["active"])
j = c.judge_leads([lead(2.5 * HOUR * i, i, source="Abandoned Cart") for i in range(8)], NOW)
expect("8 Abandoned Cart leads in 24 hours: active", j["reasons"] == ["8 Abandoned Cart leads in 24 hours"])
tmpl = lead(HOUR, 1)
tmpl["primaryPerson"]["addressLine1"] = "Street 332,"
expect("template address pages on any source", c.judge_leads([tmpl], NOW)["signatures"] == {"template address": 1})
named = lead(HOUR, 1, source="Abandoned Cart", first="test")
expect("test name on an Abandoned Cart lead: active", c.judge_leads([named], NOW)["active"])
named = lead(HOUR, 1, source="Website", first="test")
expect("test name on a Website lead alone: inactive (staff and site tests must not page)",
       not c.judge_leads([named], NOW)["active"])

# machine burst: different people inside 60 seconds, any source
j = c.judge_leads([lead(100 + 6 * i, i) for i in range(3)], NOW)
expect("3 different people within 12 seconds: active, on the machine rule alone",
       j["reasons"] == [f"3 {MACHINE}"] and j["max_in_machine_burst"] == 3)
j = c.judge_leads([lead(100 + 6 * i, "same") for i in range(3)], NOW)
expect("the same person submitting 3 times: 1 person, inactive", not j["active"] and j["max_in_machine_burst"] == 1)
j = c.judge_leads([lead(100 + 6 * i, i, email="shared@example.com") for i in range(3)], NOW)
expect("1 mailbox under 3 names within 12 seconds: 3 people, active", has(j, MACHINE) and j["max_in_machine_burst"] == 3)
j = c.judge_leads([lead(100 + 6 * i, "same", email=f"m{i}@example.com") for i in range(3)], NOW)
expect("1 name under 3 mailboxes within 12 seconds: 3 people, active", has(j, MACHINE))
j = c.judge_leads([lead(100 + 6 * i, i, email=None) for i in range(3)], NOW)
expect("no email at all: the name alone tells 3 people apart", has(j, MACHINE))
j = c.judge_leads([lead(100, "Smith", email="A@Example.com"), lead(106, "SMITH", email="a@example.com", first="PERSON"),
                   lead(112, "smith", email=" a@example.com ")], NOW)
expect("case and spaces do not make a new person", j["max_in_machine_burst"] == 1 and not j["active"])
blank = [lead(100 + 6 * i, "", email=None, first="") for i in range(3)]
expect("leads with no name and no email are each their own person", has(c.judge_leads(blank, NOW), MACHINE))
j = c.judge_leads([lead(100 + 6 * i, i, status="CONVERTED") for i in range(3)], NOW)
expect("3 move ins within 12 seconds: inactive (converted never counts)", not j["active"] and j["unconverted"] == 0)
j = c.judge_leads([lead(100 + 6 * i, i, agent="staff-uuid") for i in range(3)], NOW)
expect("3 leads keyed by staff within 12 seconds: inactive", not j["active"] and j["unconverted"] == 0)
j = c.judge_leads([lead(100, 1), lead(130, 2)], NOW)
expect("2 different people within 30 seconds: inactive", not j["active"])
j = c.judge_leads([lead(300, 1), lead(260, 2), lead(210, 3)], NOW)
expect("3 people 40 and 50 seconds apart: no 60 second window holds all 3", not j["active"] and j["max_in_machine_burst"] == 2)
j = c.judge_leads([lead(300, 1), lead(270, 2), lead(240, 3)], NOW)
expect("3 people inside exactly 60 seconds: active", has(j, MACHINE))

# leads with no phone and no address
j = c.judge_leads([bare(5 * HOUR * i + 60, i) for i in range(4)], NOW)
expect("4 different people with no phone and no address in a day: active, on that rule alone", j["reasons"] == [f"4 {BARE}"])
j = c.judge_leads([bare(5 * HOUR * i + 60, i) for i in range(3)], NOW)
expect("3 such leads: inactive", not j["active"] and j["bare"] == 3)
j = c.judge_leads([lead(5 * HOUR * i + 60, i, address=None) for i in range(4)], NOW)
expect("a phone and no address is not bare: inactive", not j["active"] and j["bare"] == 0)
j = c.judge_leads([lead(5 * HOUR * i + 60, i, phone="() -", address=None) for i in range(4)], NOW)
expect("a phone made of punctuation is no phone: bare, active", j["bare"] == 4 and has(j, BARE))
j = c.judge_leads([lead(5 * HOUR * i + 60, i, phone=None, address="   ") for i in range(4)], NOW)
expect("an address made of spaces is no address: bare, active", j["bare"] == 4 and has(j, BARE))
j = c.judge_leads([bare(5 * HOUR * i + 60, "same") for i in range(4)], NOW)
expect("1 person leaving 4 bare leads: inactive", not j["active"] and j["bare"] == 1)
j = c.judge_leads([bare(5 * HOUR * i + 60, i, status="CONVERTED") for i in range(4)], NOW)
expect("converted bare leads never count", not j["active"] and j["bare"] == 0)
j = c.judge_leads([bare(5 * HOUR * i + 60, i, agent="staff-uuid") for i in range(4)], NOW)
expect("bare leads keyed by staff never count", not j["active"] and j["bare"] == 0)

# plain volume, any mix of sources
mix = ["Website", "3rd Party", "Other", "Phone", "Abandoned Cart"]
j = c.judge_leads([lead(1.9 * HOUR * i + 60, i, source=mix[i % 5]) for i in range(12)], NOW)
expect("12 unconverted leads in a day: active, on the volume rule alone", j["reasons"] == [f"12 {VOLUME}"] and j["volume_only"])
j = c.judge_leads([lead(1.9 * HOUR * i + 60, i, source=mix[i % 5]) for i in range(11)], NOW)
expect("11 unconverted leads in a day: inactive", not j["active"] and j["unconverted"] == 11)
busy = [lead(1.5 * HOUR * i + 60, i, status="CONVERTED" if i < 5 else "OPEN") for i in range(16)]
j = c.judge_leads(busy, NOW)
expect("16 leads of which 5 moved in: 11 unconverted, inactive", not j["active"] and j["unconverted"] == 11)
calls = [lead(1.5 * HOUR * i + 60, i, source="Phone", agent="staff-uuid") for i in range(15)]
j = c.judge_leads(calls, NOW)
expect("15 calls logged by staff in a day: inactive", not j["active"] and j["unconverted"] == 0 and j["leads"] == 15)
same = [lead(1.9 * HOUR * i + 60, "same") for i in range(12)]
j = c.judge_leads(same, NOW)
expect("volume counts leads, not people: 1 identity 12 times is active", j["reasons"] == [f"12 {VOLUME}"])

# the window: older leads are ignored by every rule
j = c.judge_leads([bare(25 * HOUR + 6 * i, i) for i in range(12)], NOW)
expect("12 bare leads 25 hours old: inactive", not j["active"] and j["unconverted"] == 0)

# dates: any fraction length reads the same on every Python, and unreadable is never clear
shapes = ["2026-11-15T11:00:00Z", "2026-11-15T11:00:00.0Z", "2026-11-15T11:00:00.00Z", "2026-11-15T11:00:00.000Z",
          "2026-11-15T11:00:00.0000000Z", "2026-11-15T11:00:00.000000+00:00", "2026-11-15T11:00:00"]
expect("7 timestamp shapes read as the same UTC instant",
       {c._created({"dateCreated": s}) for s in shapes} == {NOW - timedelta(hours=1)})
expect("a 2 digit fraction keeps its value", c._created({"dateCreated": "2026-11-15T11:00:00.12Z"}).microsecond == 120000)
expect("no date and nonsense read as None", c._created({}) is None and c._created({"dateCreated": "not a date"}) is None)
undated = []
for i in range(12):
    row = bare(HOUR, i)
    row["dateCreated"] = "not a date"
    undated.append(row)
j = c.judge_leads(undated, NOW)
expect("12 bare leads with unreadable dates count toward bare and volume: active",
       j["active"] and j["bare"] == 12 and j["unconverted"] == 12 and j["max_in_machine_burst"] == 0 and not j["volume_only"])
expect("3 leads with unreadable dates: inactive", not c.judge_leads(undated[:3], NOW)["active"])

# the 6 Nov 2026 shape: Website source, name and email only, clusters 5 seconds apart, 1 real move in between
night = [bare(6 * HOUR, "single"), lead(6 * HOUR - 700, "tenant", status="CONVERTED")]
clusters = []
for n, size in enumerate((5, 6, 6, 3)):
    clusters.append([bare(5 * HOUR - 4000 * n - 5.5 * i, f"c{n}n{i}") for i in range(size)])
    night += clusters[-1]
j = c.judge_leads(night, NOW)
expect("the 6 Nov 2026 shape: active on all 3 new rules, 0 Abandoned Cart",
       j["abandoned_cart"] == 0 and j["max_in_machine_burst"] == 6 and j["bare"] == 21 and j["unconverted"] == 21
       and j["reasons"] == [f"6 {MACHINE}", f"21 {BARE}", f"21 {VOLUME}"] and not j["volume_only"])
j = c.judge_leads(clusters[0][:3], NOW)
expect("it pages on the first 3 leads of the first cluster, on the machine rule", j["reasons"] == [f"3 {MACHINE}"])
moved_in = [dict(x, leadStatus="CONVERTED") for x in clusters[0][:3]]
expect("the same 3 leads as move ins: inactive", not c.judge_leads(moved_in, NOW)["active"])


# the cross facility wrapper: a failed read is unknown, never clear; output carries no identities
def reader(ok_names, rows, cypress_rows=()):
    def read(key, uuid, since):
        name = next(n for n, _, u in c.SITES if u == uuid)
        if name not in ok_names:
            raise RuntimeError("HTTP 503 " + "x" * 60 + " leak@example.com")
        return rows if name == LT else list(cypress_rows)
    return read


FAKE = "fake leads are being posted into Monument's storefront"
r = c.bot_lead_check("k", NOW, read=reader({CY, LT}, night))
expect("Lifetime flagged, Cypress clean: active and available",
       r["active"] and r["available"] and r["summary"] == f"{LT}: {FAKE}" and "- Cypress Mini Storage: 0 leads" in r["detail"])
expect("output carries no identities", "example.com" not in r["detail"] + r["summary"] and "Person" not in r["detail"]
       and "c0n0" not in r["detail"])
r = c.bot_lead_check("k", NOW, read=reader({CY}, night))
expect("Lifetime unreadable: not available, never clear", not r["available"] and not r["active"])
expect("an error message is cut before it can carry data", "leak@example.com" not in r["detail"] + r["summary"])
r = c.bot_lead_check("k", NOW, read=reader({CY, LT}, []))
expect("both clean: inactive and available", not r["active"] and r["available"] and "no bot leads" in r["summary"])
many = [lead(1.9 * HOUR * i + 60, i, source=mix[i % 4]) for i in range(12)]
r = c.bot_lead_check("k", NOW, read=reader({CY, LT}, [], cypress_rows=many))
expect("volume alone is stated as a count, not as fake leads",
       r["active"] and r["summary"] == f"{CY}: an unusual number of new leads in 24 hours, possibly a bot" and FAKE not in r["summary"])
r = c.bot_lead_check("k", NOW, read=reader({CY, LT}, night, cypress_rows=many))
expect("a sure hit and a volume hit are named separately",
       r["summary"] == f"{LT}: {FAKE}; {CY}: an unusual number of new leads in 24 hours")
print(f"all {checks} checks passed")
