"""Per-second GPS export for garmin-sync.

Writes one CSV per run to gps/ plus a small gps/index.csv.
data.json is never touched, so regular analysis stays small.
Incremental: activities already in the index are skipped, so every
scheduled run only fetches new activities. First run backfills GPS_DAYS.

Env: GARMIN_EMAIL, GARMIN_PASSWORD, GPS_DAYS (default 90), GPS_SLEEP (default 2)
"""
import csv
import datetime as dt
import os
import sys
import time
from pathlib import Path

from garminconnect import Garmin

OUT = Path("gps")
INDEX = OUT / "index.csv"
DAYS = int(os.environ.get("GPS_DAYS", "90"))
SLEEP = float(os.environ.get("GPS_SLEEP", "2"))
RUN_TYPES = {"running", "trail_running", "track_running", "virtual_run", "treadmill_running"}
INDEX_COLS = ["activity_id", "date", "name", "type", "distance_km", "duration_s", "points", "file"]
POINT_COLS = ["t_s", "lat", "lon", "elev_m", "hr", "cadence_spm", "speed_ms", "pace_s_km"]
KEYS = {
    "ts": "directTimestamp",
    "lat": "directLatitude",
    "lon": "directLongitude",
    "elev": "directElevation",
    "hr": "directHeartRate",
    "cad": "directRunCadence",
    "spd": "directSpeed",
}


def load_index():
    if not INDEX.exists():
        return {}
    with INDEX.open(newline="") as f:
        return {r["activity_id"]: r for r in csv.DictReader(f)}


def save_index(rows):
    ordered = sorted(rows.values(), key=lambda r: (r["date"], r["activity_id"]))
    with INDEX.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_COLS)
        w.writeheader()
        w.writerows(ordered)


def points_from_details(details):
    desc = {d["key"]: d["metricsIndex"] for d in details.get("metricDescriptors", [])}
    rows = details.get("activityDetailMetrics", [])
    if KEYS["lat"] not in desc or not rows:
        return []

    def get(m, name):
        i = desc.get(KEYS[name])
        return None if i is None else m[i]

    out, t0 = [], None
    for r in rows:
        m = r["metrics"]
        lat, lon = get(m, "lat"), get(m, "lon")
        if lat is None or lon is None:
            continue
        ts = get(m, "ts")
        if ts is not None and t0 is None:
            t0 = ts
        spd = get(m, "spd")
        pace = round(1000 / spd) if spd and spd > 0.5 else ""
        out.append([
            round((ts - t0) / 1000) if ts is not None else "",
            round(lat, 6), round(lon, 6),
            "" if get(m, "elev") is None else round(get(m, "elev"), 1),
            "" if get(m, "hr") is None else round(get(m, "hr")),
            "" if get(m, "cad") is None else round(get(m, "cad") * 2),  # Garmin stream is per-foot; x2 = steps/min
            "" if spd is None else round(spd, 2),
            pace,
        ])
    return out


def main():
    OUT.mkdir(exist_ok=True)
    api = Garmin(os.environ["GARMIN_EMAIL"], os.environ["GARMIN_PASSWORD"])
    api.login()

    end = dt.date.today()
    start = end - dt.timedelta(days=DAYS)
    acts = api.get_activities_by_date(start.isoformat(), end.isoformat())
    index = load_index()
    new = 0

    for a in sorted(acts, key=lambda x: x["startTimeLocal"]):
        aid = str(a["activityId"])
        atype = a.get("activityType", {}).get("typeKey", "")
        if aid in index or atype not in RUN_TYPES:
            continue
        time.sleep(SLEEP)
        try:
            details = api.get_activity_details(a["activityId"], maxchart=100000)
        except Exception as e:  # 429 or transient: stop cleanly, next run resumes
            print(f"stopping at {aid}: {e}", file=sys.stderr)
            break
        pts = points_from_details(details)
        if not pts:  # treadmill / no GPS
            continue
        date = a["startTimeLocal"][:10]
        fname = f"{date}_{aid}.csv"
        with (OUT / fname).open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(POINT_COLS)
            w.writerows(pts)
        index[aid] = {
            "activity_id": aid,
            "date": date,
            "name": a.get("activityName", ""),
            "type": atype,
            "distance_km": round(a.get("distance", 0) / 1000, 2),
            "duration_s": round(a.get("duration", 0)),
            "points": len(pts),
            "file": fname,
        }
        new += 1
        save_index(index)  # save as we go so a rate-limit stop loses nothing

    save_index(index)
    print(f"added {new} activities; index has {len(index)}")


if __name__ == "__main__":
    main()
