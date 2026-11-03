#!/usr/bin/env python3
"""Synthetic checks for the card payment rule in site_units_check.py (no network).

Run: python3 scripts/test_card_payments.py
The thresholds themselves were calibrated by replaying the real ledger (20 Aug to
3 Nov 2026): only the 15 to 19 Sep outage and the 12 to 27 Oct merchant block
alerted, the latter from 19 Oct 21:00 UTC. That replay reads live data and is
not part of this file.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import site_units_check as c  # noqa: E402

CY, LT = "Cypress Mini Storage", "Lifetime Climate Controlled Storage"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)  # mid month: the floor is not judged
FIRST = datetime(2026, 12, 1, 16, 0, tzinfo=timezone.utc)  # the 1st after 15:00 UTC: the floor is judged


def row(when, uuid, tender="CREDIT/DEBIT", status="COMPLETED", kind="PAYMENT"):
    return {"transactionType": kind, "paymentStatus": status, "paymentUuid": uuid,
            "createdAt": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "payment": {"tenderType": tender, "source": "VISA"} if tender else None}


def payments(now, every_h, count, **kw):
    return [row(now - timedelta(hours=every_h * i + 1), f"p{i}", **kw) for i in range(count)]


checks = 0


def expect(name, cond):
    global checks
    checks += 1
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    print("ok:", name)


# quiet facility, last card payment inside the limit: no alert
j = c.judge_payments([row(NOW - timedelta(hours=101), "p")], NOW, LT)
expect("Lifetime 101 h silence is normal", not j["active"] and j["hours_since_last"] == 101)
# past the limit: alert, with the hours in the reason
j = c.judge_payments([row(NOW - timedelta(hours=169), "p")], NOW, LT)
expect("Lifetime 169 h silence alerts", j["active"] and "169 hours ago" in j["reasons"][0])
j = c.judge_payments([row(NOW - timedelta(hours=121), "p")], NOW, CY)
expect("Cypress 121 h silence alerts", j["active"])
j = c.judge_payments([row(NOW - timedelta(hours=119), "p")], NOW, CY)
expect("Cypress 119 h silence is normal", not j["active"])
# nothing in the whole lookback
j = c.judge_payments([], NOW, LT)
expect("empty ledger alerts as no payment in the lookback", j["active"] and j["hours_since_last"] is None)
# ACH, refunds, invoices and failed reconciliations never count as a card payment
rows = [row(NOW - timedelta(hours=2), "a", tender="ACH"), row(NOW - timedelta(hours=3), "r", status="REFUNDED"),
        row(NOW - timedelta(hours=4), "i", tender=None, kind="INVOICE"),
        row(NOW - timedelta(hours=5), "f", tender="ACH", status="RECONCILIATION_FAILED"),
        row(NOW - timedelta(hours=200), "old")]
j = c.judge_payments(rows, NOW, LT)
expect("only completed card payments count", j["active"] and j["card_payments_in_lookback"] == 1)
# one payment spread over 3 line items counts once
rows = [row(NOW - timedelta(hours=1), "same")] * 3
j = c.judge_payments(rows, NOW, LT)
expect("line items of 1 payment count once", j["card_payments_in_lookback"] == 1)
# month start floor: judged from the 1st 15:00 UTC through the 7th
j = c.judge_payments(payments(FIRST, 1, 4), FIRST, LT)
expect("4 card payments on the 1st at 16:00 UTC is below the Lifetime floor", j["active"] and "floor 10" in j["reasons"][0])
j = c.judge_payments(payments(FIRST, 1, 12), FIRST, LT)
expect("12 card payments on the 1st meets the Lifetime floor", not j["active"] and j["since_month_start"] == 12)
early = FIRST.replace(hour=9)
j = c.judge_payments(payments(early, 1, 2), early, LT)
expect("the floor is not judged before 15:00 UTC on the 1st", not j["active"] and j["since_month_start"] is None)
day5 = FIRST + timedelta(days=4)
j = c.judge_payments(payments(day5, 10, 4), day5, CY)
expect("4 payments by the 5th is below the Cypress floor of 20", j["active"] and j["since_month_start"] == 4)
day9 = FIRST + timedelta(days=8)
j = c.judge_payments(payments(day9, 10, 4), day9, CY)
expect("after the 7th the floor is no longer judged", not j["active"] and j["since_month_start"] is None)


# the cross facility wrapper: a failed read is unknown, never clear
def reader(ok_names):
    def read(key, uuid, since):
        name = next(n for n, _, u in c.SITES if u == uuid)
        if name not in ok_names:
            raise RuntimeError("HTTP 503")
        return payments(NOW, 10, 5)
    return read


r = c.card_payment_check("k", NOW, read=reader({CY, LT}))
expect("both normal: available and inactive", r["available"] and not r["active"] and "normally" in r["summary"])
r = c.card_payment_check("k", NOW, read=reader({CY}))
expect("one ledger unreadable: not available", not r["available"] and not r["active"] and "could not read" in r["summary"])
r = c.card_payment_check("", NOW)
expect("missing key: not available", not r["available"] and not r["active"])


def silent(key, uuid, since):
    return [] if uuid == c.SITES[1][2] else payments(NOW, 10, 5)


r = c.card_payment_check("k", NOW, read=silent)
expect("Lifetime silent: active, named in the summary", r["active"] and r["summary"].startswith(LT) and "- Cypress" in r["detail"])
expect("output carries no identities", "VISA" not in r["detail"] and "p0" not in r["detail"])
print(f"all {checks} checks passed")
