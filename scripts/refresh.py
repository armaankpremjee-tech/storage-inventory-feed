#!/usr/bin/env python3
"""Refresh the inventory snapshot from Monument's shopping API.

Writes <facilityUuid>.json for each facility when the payload validates and
changed, and status.json as a heartbeat (every run when inventory changed,
otherwise at most once an hour, so the repo does not collect 288 commits a day).
Exit 1 when any facility could not be refreshed, so the workflow run shows red.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

FACILITIES = {
    "29c2b9eb-699f-11f1-989e-e99e7c079122": "Cypress Mini Storage",
    "29c2b9e2-699f-11f1-989e-e99e7c079122": "Lifetime Climate Controlled Storage",
}
BASE = "https://public-api.prd.monument.io/shopping/storagegiant/v1"
HEARTBEAT_SECONDS = 3600


def fetch(facility_uuid, key):
    req = urllib.request.Request(
        f"{BASE}/facilities/{facility_uuid}/unitGroups",
        headers={"x-api-key": key, "accept": "application/json"},
    )
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode(errors="ignore")
            last = (e.code, body)
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:  # noqa: BLE001
            last = (0, repr(e))
        time.sleep(10 * (attempt + 1))
    return last


def valid(raw):
    if not isinstance(raw, list) or not raw:
        return False
    for g in raw:
        if not isinstance(g, dict):
            return False
        if not isinstance(g.get("unitGroupUuid"), str) or not g["unitGroupUuid"].strip():
            return False
        for k in ("unitGroupWidth", "unitGroupDepth"):
            v = g.get(k)
            if not isinstance(v, (int, float)) or v <= 0:
                return False
        c = g.get("availableUnitCount")
        if not isinstance(c, int) or isinstance(c, bool) or c < 0:
            return False
    return True


def main():
    key = os.environ.get("MONUMENT_API_KEY", "").strip()
    if not key:
        print("MONUMENT_API_KEY is not set", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        status = json.load(open("status.json"))
    except Exception:  # noqa: BLE001
        status = {}
    facilities_status = status.get("facilities", {})
    failures = 0
    changed = False
    for uuid, name in FACILITIES.items():
        code, payload = fetch(uuid, key)
        if code != 200 or not valid(payload):
            failures += 1
            print(f"{name}: refresh FAILED, HTTP {code}: {str(payload)[:200]}", file=sys.stderr)
            facilities_status[uuid] = {
                **facilities_status.get(uuid, {}),
                "name": name,
                "last_error_at": now.isoformat(),
                "last_error": f"HTTP {code}: {str(payload)[:160]}",
            }
            continue
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        path = f"{uuid}.json"
        old = open(path).read() if os.path.exists(path) else None
        if body != old:
            open(path, "w").write(body)
            changed = True
            print(f"{name}: inventory changed, {len(payload)} groups written")
        else:
            print(f"{name}: unchanged, {len(payload)} groups")
        facilities_status[uuid] = {
            "name": name,
            "groups": len(payload),
            "available_units": sum(g["availableUnitCount"] for g in payload),
            "refreshed_at": now.isoformat(),
        }
    last_hb = status.get("checked_at")
    stale = True
    if last_hb:
        try:
            stale = (now - datetime.fromisoformat(last_hb)).total_seconds() >= HEARTBEAT_SECONDS
        except ValueError:
            stale = True
    if changed or stale or failures:
        status = {"checked_at": now.isoformat(), "ok": failures == 0, "facilities": facilities_status}
        open("status.json", "w").write(json.dumps(status, indent=2, sort_keys=True) + "\n")
        print("status.json written")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
