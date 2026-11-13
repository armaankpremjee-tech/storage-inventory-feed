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


# Since 13 Nov 2026: a silence past the limit is a quiet week, not an alert, only
# when nobody has tried to pay since the last completed card payment. A rental
# record's lastPayment moves on every attempt, a decline included; the ledger has a
# PAYMENT row only for attempts that went through.
c.PAY_ATTEMPT_RETRY_S = 0


def rrow(when, uuid, rental, tender="CREDIT/DEBIT", status="COMPLETED"):
    out = row(when, uuid, tender=tender, status=status)
    out["unitRentalRecordUuid"] = rental
    return out


LAST = NOW - timedelta(hours=173)                   # the 13 Nov 2026 shape: 173 h since the last card payment
base = [rrow(LAST, "p65", "r65"), rrow(LAST - timedelta(hours=20), "p7", "r7")]
before = {"r65": LAST - timedelta(seconds=4), "r7": LAST - timedelta(hours=20, seconds=4),
          "r76": NOW - timedelta(days=32)}          # its own decline long before the last success
j = c.judge_payments(base, NOW, LT, attempts=before)
expect("173 h with nobody trying to pay is quiet, not an alert", not j["active"] and j["quiet"] and j["tried_since"] == 0)
j = c.judge_payments(base, NOW, LT, attempts=dict(before, r20=NOW - timedelta(hours=30)))
expect("1 tenant trying (a decline, no ledger row) after the limit alerts as before",
       j["active"] and "1 rental has tried to pay since" in j["reasons"][0])
paid_ach = base + [rrow(NOW - timedelta(hours=30), "a20", "r20", tender="ACH")]
j = c.judge_payments(paid_ach, NOW, LT, attempts=dict(before, r20=NOW - timedelta(hours=30, seconds=4)))
expect("an ACH payment since the last card payment still counts as an attempt (old rule)", j["active"])
# the review's masking case: declined by card, then paid by ACH; lastPayment only moves forward
j = c.judge_payments(paid_ach, NOW, LT, attempts=dict(before, r20=NOW - timedelta(hours=2), r37=NOW - timedelta(hours=1)))
expect("a decline later covered by an ACH payment cannot turn the alert quiet", j["active"] and j["tried_since"] == 2)
j = c.judge_payments(base, NOW, LT, attempts_error="rentalRecords/get_by_uuids HTTP 503")
expect("unreadable attempts give no verdict (neither alert nor quiet)",
       not j["active"] and j["unknown"] and "could not be read" in j["unknown"] and not j["quiet"])
j = c.judge_payments(base, NOW, LT)
expect("no attempt data at all keeps the old silence rule", j["active"] and "173 hours ago" in j["reasons"][0])
old = [rrow(NOW - timedelta(hours=253), "p", "r1")]
j = c.judge_payments(old, NOW, LT, attempts={"r1": NOW - timedelta(hours=253, seconds=4)})
expect("past the 252 h Lifetime backstop alerts with nobody trying", j["active"] and "252 h backstop" in j["reasons"][0])
j = c.judge_payments(old, NOW, LT, attempts_error="HTTP 503")
expect("past the backstop alerts even when attempts are unreadable", j["active"] and j["unknown"] is None)
j = c.judge_payments([rrow(NOW - timedelta(hours=181), "p", "r1")], NOW, CY, attempts={"r1": NOW - timedelta(days=9)})
expect("Cypress backstop is 180 h", j["active"] and "180 h backstop" in j["reasons"][0])
j = c.judge_payments([rrow(NOW - timedelta(hours=170), "p", "r1")], NOW, CY, attempts={"r1": NOW - timedelta(days=9)})
expect("Cypress 170 h with nobody trying is quiet", not j["active"] and j["quiet"])
day3 = FIRST + timedelta(days=2)
j = c.judge_payments([rrow(day3 - timedelta(hours=170), "p", "r1")], day3, LT, attempts={"r1": day3 - timedelta(days=9)})
expect("the month start floor still alerts in a quiet stretch", j["active"] and "floor 10" in j["reasons"][0] and j["quiet"] is None)
expect("lookback covers the longest backstop", c.PAY_LOOKBACK_H >= max(c.PAY_SILENCE_H.values()) * c.PAY_BACKSTOP_FACTOR)
# a line added to an old payment (prepaid credit applied on the 25th, a voided invoice) is not a new payment
realloc = [rrow(NOW - timedelta(hours=200), "same", "r1"), rrow(NOW - timedelta(hours=2), "same", "r1")]
j = c.judge_payments(realloc, NOW, LT)
expect("a payment is dated by its earliest line", j["hours_since_last"] == 200 and j["active"])


def lt_quiet(key, uuid, since):
    return base if uuid == c.SITES[1][2] else payments(NOW, 10, 5)


def attempts_reader(*results):
    calls = []

    def read(key, uuid):
        calls.append(uuid)
        result = results[min(len(calls), len(results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result
    read.calls = calls
    return read


ar = attempts_reader(before)
r = c.card_payment_check("k", NOW, read=lt_quiet, read_attempts=ar)
expect("quiet Lifetime: inactive, available, summary says quiet", not r["active"] and r["available"]
       and r["summary"].startswith(LT + " is quiet") and "nobody has tried to pay since" in r["detail"])
expect("attempts are read only for the facility past its limit", ar.calls == [c.SITES[1][2]])
expect("quiet output carries no identities", "r65" not in r["detail"] and "r76" not in r["detail"] and "VISA" not in r["detail"])
r = c.card_payment_check("k", NOW, read=lt_quiet, read_attempts=attempts_reader(dict(before, r37=NOW - timedelta(hours=2))))
expect("a tenant trying at quiet Lifetime: active", r["active"] and r["summary"].startswith(LT + ": card payments have stopped"))
ar = attempts_reader(RuntimeError("HTTP 0"), RuntimeError("HTTP 503"), before)
r = c.card_payment_check("k", NOW, read=lt_quiet, read_attempts=ar)
expect("a read that fails twice then works gives the verdict", not r["active"] and r["available"] and len(ar.calls) == 3)
ar = attempts_reader(RuntimeError("HTTP 401"))
r = c.card_payment_check("k", NOW, read=lt_quiet, read_attempts=ar)
expect("attempts unreadable after 3 tries: no alert, not available (an open issue stays open, none opens)",
       not r["active"] and not r["available"] and "HTTP 401" in r["detail"] and "no verdict" in r["summary"]
       and len(ar.calls) == 3)


# payment_attempts itself, on canned HTTP answers (no network)
def fake_http(uuids_status=200, uuids=("u1", "u2"), records=None):
    records = [{"rentalRecordUuid": "u1", "lastPayment": {"lastPaymentDate": "2026-11-10T15:19:32.881Z"}},
               {"rentalRecordUuid": "u2"}] if records is None else records

    def g(url, headers=None, timeout=30):
        return uuids_status, {}, c.json.dumps(list(uuids)).encode()

    def p(url, body, headers=None, timeout=30):
        assert "expand%5BlastPayment%5D" in url and body == {"unitRentalRecordUuids": list(uuids)}
        return 200, {}, c.json.dumps(records).encode()
    return g, p


def raises(fn):
    try:
        fn()
    except RuntimeError:
        return True
    return False


saved = (c.get, c.post)
try:
    c.get, c.post = fake_http()
    got = c.payment_attempts("k", "f")
    expect("payment_attempts reads lastPaymentDate as UTC", got == {"u1": datetime(2026, 11, 10, 15, 19, 32, 881000, tzinfo=timezone.utc)})
    c.get, c.post = fake_http(records=[{"rentalRecordUuid": "u1"}, {"rentalRecordUuid": "u2"}])
    expect("no lastPayment anywhere raises (the expand stopped working)", raises(lambda: c.payment_attempts("k", "f")))
    c.get, c.post = fake_http(uuids=())
    expect("no rental records at all raises (unknown, never quiet)", raises(lambda: c.payment_attempts("k", "f")))
    c.get, c.post = fake_http(uuids_status=401)
    expect("HTTP 401 raises", raises(lambda: c.payment_attempts("k", "f")))
finally:
    c.get, c.post = saved

print(f"all {checks} checks passed")
