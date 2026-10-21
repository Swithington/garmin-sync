"""Personal bests for garmin-sync.

Writes pbs.json (current records) and appends to pb_history.csv whenever a
record is new or improves. Source: Garmin's own personal records, plus
biggest running week computed from data.json (Garmin has no weekly-run-km PR).

Env: GARMIN_EMAIL, GARMIN_PASSWORD
"""
import csv
import datetime as dt
import json
import os
from collections import defaultdict
from pathlib import Path

from garminconnect import Garmin

PBS = Path("pbs.json")
HISTORY = Path("pb_history.csv")
DATA = Path("data.json")
HIST_COLS = ["logged_on", "metric", "value", "display", "activity_id", "achieved_on", "previous_value"]
RUN_TYPES = {"running", "trail_running", "track_running", "virtual_run", "treadmill_running"}

# Garmin personal-record type ids (verify against pbs.json "unmapped" after first run)
TYPE_MAP = {
    1: ("fastest_1k", "time"),
    2: ("fastest_1mile", "time"),
    3: ("fastest_5k", "time"),
    4: ("fastest_10k", "time"),
    5: ("fastest_half_marathon", "time"),
    6: ("fastest_marathon", "time"),
    7: ("longest_run", "distance"),
}
LOWER_IS_BETTER = {"time"}


def fmt_time(s):
    s = round(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def display(kind, value):
    return fmt_time(value) if kind == "time" else f"{value / 1000:.2f} km"


def pick(rec, *names):
    for n in names:
        if rec.get(n) is not None:
            return rec[n]
    return None


def garmin_records(api):
    out, unmapped = {}, []
    for rec in api.get_personal_record() or []:
        tid = rec.get("typeId")
        value = rec.get("value")
        if tid not in TYPE_MAP or value is None:
            unmapped.append({"typeId": tid, "value": value})
            continue
        key, kind = TYPE_MAP[tid]
        when = pick(rec, "prStartTimeGmtFormatted", "actStartDateTimeInGMTFormatted", "prTypeLabelKey") or ""
        out[key] = {
            "value": value,
            "display": display(kind, value),
            "activity_id": pick(rec, "activityId"),
            "achieved_on": str(when)[:10],
            "kind": kind,
        }
    return out, unmapped


def biggest_week():
    if not DATA.exists():
        return None
    weeks = defaultdict(float)
    for day, d in json.load(DATA.open()).get("days", {}).items():
        for a in d.get("activities", []):
            if a.get("type") in RUN_TYPES and a.get("distance_km"):
                monday = dt.date.fromisoformat(day) - dt.timedelta(days=dt.date.fromisoformat(day).weekday())
                weeks[monday.isoformat()] += a["distance_km"]
    if not weeks:
        return None
    start = max(weeks, key=weeks.get)
    return {"week_start": start, "km": round(weeks[start], 1), "kind": "volume"}


def changed(old, new):
    if old is None:
        return True
    if new["kind"] in LOWER_IS_BETTER:
        return new["value"] < old["value"]
    return new["value"] > old["value"]


def main():
    api = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    api.login()

    old = json.loads(PBS.read_text()) if PBS.exists() else {}
    old_rec = old.get("records", {})
    records, unmapped = garmin_records(api)
    today = dt.date.today().isoformat()

    new_rows = []
    for key, rec in records.items():
        prev = old_rec.get(key)
        if changed(prev, rec):
            new_rows.append({
                "logged_on": today, "metric": key, "value": rec["value"],
                "display": rec["display"], "activity_id": rec["activity_id"],
                "achieved_on": rec["achieved_on"],
                "previous_value": prev["value"] if prev else "",
            })

    bw = biggest_week()
    old_bw = old.get("biggest_week")
    if bw and (old_bw is None or bw["km"] > old_bw["km"]):
        new_rows.append({
            "logged_on": today, "metric": "biggest_week_km", "value": bw["km"],
            "display": f"{bw['km']} km", "activity_id": "",
            "achieved_on": bw["week_start"],
            "previous_value": old_bw["km"] if old_bw else "",
        })
    elif old_bw:
        bw = old_bw  # never lower a stored record if data.json window moved on

    if new_rows:
        write_header = not HISTORY.exists()
        with HISTORY.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HIST_COLS)
            if write_header:
                w.writeheader()
            w.writerows(new_rows)

    # keep better stored records if Garmin ever returns a worse one
    merged = dict(old_rec)
    merged.update({k: v for k, v in records.items() if changed(old_rec.get(k), v) or k not in old_rec})
    PBS.write_text(json.dumps({
        "updated_at": today,
        "records": merged,
        "biggest_week": bw,
        "unmapped": unmapped,
    }, indent=1))
    print(f"{len(new_rows)} new PB rows; {len(merged)} Garmin records; unmapped={len(unmapped)}")


if __name__ == "__main__":
    main()
