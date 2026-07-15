"""
Garmin Connect -> data.json
Fetches all history from Garmin Connect and writes structured JSON.
Designed to run in GitHub Actions on a nightly schedule.
Merges with existing data.json so only new dates are fetched from Garmin.
"""

import os
import json
import urllib.request
import urllib.parse
import logging
import pickle
from datetime import date, timedelta
from garminconnect import Garmin

logging.basicConfig(level=logging.WARNING)

# -- Config -------------------------------------------------------------------

GARMIN_EMAIL    = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
DAYS_TO_SYNC    = int(os.environ.get("DAYS_TO_SYNC", 365))
REFRESH_DAYS    = 7        # always re-fetch last N days to catch late syncs
OUTPUT_FILE     = "data.json"
TOKEN_DIR       = os.path.expanduser("~/.garminconnect")

WMO_MAP = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
    61: "Rain", 63: "Rain", 65: "Heavy rain",
    71: "Snow", 73: "Snow", 75: "Heavy snow",
    80: "Showers", 81: "Showers", 82: "Heavy showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}

# -- Helpers ------------------------------------------------------------------

def decimal_mins_to_mmss(value):
    try:
        if not value:
            return ""
        val = float(value)
        if val <= 0:
            return ""
        mins = int(val)
        secs = round((val - mins) * 60)
        if secs == 60:
            mins += 1
            secs = 0
        return f"{mins}:{secs:02d}"
    except (ValueError, TypeError):
        return ""

# -- Garmin Auth --------------------------------------------------------------

def get_garmin_client():
    os.makedirs(TOKEN_DIR, exist_ok=True)
    token_file  = os.path.join(TOKEN_DIR, "oauth2_token.json")
    pickle_file = os.path.join(TOKEN_DIR, "session.pkl")

    client = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)

    if os.path.exists(token_file):
        try:
            client.login(token_file)
            client.get_user_summary(date.today().isoformat())
            print("Session resumed from token cache")
            return client
        except Exception:
            print("Token expired, logging in fresh...")
            for f in [token_file, pickle_file]:
                if os.path.exists(f):
                    os.remove(f)

    if os.path.exists(pickle_file):
        try:
            with open(pickle_file, "rb") as f:
                client = pickle.load(f)
            client.get_user_summary(date.today().isoformat())
            print("Session resumed from pickle cache")
            return client
        except Exception:
            print("Pickle expired, logging in fresh...")
            if os.path.exists(pickle_file):
                os.remove(pickle_file)

    print("Logging in to Garmin Connect...")
    client = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)
    client.login()
    print("Login successful")

    try:
        tokenstore = client.garth.dumps()
        with open(token_file, "w") as f:
            f.write(tokenstore)
    except AttributeError:
        with open(pickle_file, "wb") as f:
            pickle.dump(client, f)

    return client

# -- Weather ------------------------------------------------------------------

def fetch_weather(lat, lon, date_str, hour=12):
    empty = {"temp_c": None, "humidity_pct": None, "wind_kph": None, "weather_desc": ""}
    if not lat or not lon:
        return empty
    try:
        params = urllib.parse.urlencode({
            "latitude":       round(float(lat), 4),
            "longitude":      round(float(lon), 4),
            "start_date":     date_str,
            "end_date":       date_str,
            "hourly":         "temperature_2m,relativehumidity_2m,windspeed_10m,weathercode",
            "timezone":       "auto",
            "windspeed_unit": "kmh",
        })
        url = f"https://archive-api.open-meteo.com/v1/archive?{params}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        hourly = data.get("hourly", {})
        if not hourly.get("time"):
            return empty
        idx  = min(hour, len(hourly["time"]) - 1)
        code = hourly.get("weathercode", [None])[idx]
        return {
            "temp_c":       hourly.get("temperature_2m",      [None])[idx],
            "humidity_pct": hourly.get("relativehumidity_2m", [None])[idx],
            "wind_kph":     hourly.get("windspeed_10m",       [None])[idx],
            "weather_desc": WMO_MAP.get(code, "") if code is not None else "",
        }
    except Exception as e:
        print(f"  Weather fetch failed {date_str}: {e}")
        return empty

# -- Data Fetching ------------------------------------------------------------

def fetch_wellness(client, date_str):
    result = {"date": date_str}

    # Sleep
    try:
        data  = client.get_sleep_data(date_str)
        daily = data.get("dailySleepDTO", {})
        def to_hrs(v): return round((v or 0) / 3600, 2) if v is not None else None
        result["sleep"] = {
            "score":         daily.get("sleepScores", {}).get("overall", {}).get("value"),
            "duration_hrs":  to_hrs(daily.get("sleepTimeSeconds")),
            "deep_hrs":      to_hrs(daily.get("deepSleepSeconds")),
            "rem_hrs":       to_hrs(daily.get("remSleepSeconds")),
            "light_hrs":     to_hrs(daily.get("lightSleepSeconds")),
            "awake_hrs":     to_hrs(daily.get("awakeSleepSeconds")),
        }
    except Exception as e:
        print(f"  Sleep failed {date_str}: {e}")
        result["sleep"] = {}

    # HRV
    try:
        data    = client.get_hrv_data(date_str)
        summary = data.get("hrvSummary", {})
        result["hrv"] = {
            "weekly_avg":  summary.get("weeklyAvg"),
            "last_night":  summary.get("lastNight"),
            "5min_high":   summary.get("lastNight5MinHigh"),
            "status":      summary.get("status"),
        }
    except Exception as e:
        print(f"  HRV failed {date_str}: {e}")
        result["hrv"] = {}

    # Body Battery
    try:
        data = client.get_body_battery(date_str, date_str)
        if data and len(data) > 0:
            result["body_battery"] = {
                "charged": data[0].get("charged"),
                "drained": data[0].get("drained"),
            }
        else:
            result["body_battery"] = {}
    except Exception as e:
        print(f"  Body Battery failed {date_str}: {e}")
        result["body_battery"] = {}

    # Stress
    try:
        data = client.get_stress_data(date_str)
        result["avg_stress"] = data.get("overallStressLevel")
    except Exception as e:
        print(f"  Stress failed {date_str}: {e}")
        result["avg_stress"] = None

    # Resting HR
    try:
        data = client.get_rhr_day(date_str)
        metrics = data.get("allMetrics", {}).get("metricsMap", {})
        rhr_list = metrics.get("WELLNESS_RESTING_HEART_RATE", [])
        result["resting_hr"] = rhr_list[0].get("value") if rhr_list else None
    except Exception as e:
        print(f"  Resting HR failed {date_str}: {e}")
        result["resting_hr"] = None

    return result

def fetch_hr_zones(client, activity_id):
    zones = {}
    try:
        data = client.get_activity_hr_in_timezones(activity_id)
        items = data if isinstance(data, list) else data.get("heartRateZones", data.get("zones", []))
        for z in items:
            n = z.get("zoneNumber")
            s = z.get("secsInZone", 0) or 0
            if n and 1 <= n <= 5:
                zones[f"zone_{n}_mins"] = round(s / 60, 1)
    except Exception:
        pass
    for i in range(1, 6):
        zones.setdefault(f"zone_{i}_mins", None)
    return zones

def fetch_hr_drift(detail):
    try:
        splits = detail.get("splitSummaries", [])
        run_splits = [s for s in splits if s.get("splitType") in ("INTERVAL_ACTIVE", "RWD_RUN")]
        if not run_splits:
            run_splits = splits
        hr_vals = [s.get("averageHR") or s.get("averageHr") for s in run_splits
                   if s.get("averageHR") or s.get("averageHr")]
        if len(hr_vals) >= 2:
            return round(((hr_vals[-1] - hr_vals[0]) / hr_vals[0]) * 100, 1)
    except Exception:
        pass
    return None

def fetch_activities(client, date_str):
    activities = []
    try:
        raw = client.get_activities_by_date(date_str, date_str)
        for a in raw:
            activity_id = a.get("activityId")
            distance_m  = a.get("distance", 0) or 0
            duration_s  = a.get("duration", 0) or 0
            avg_speed   = a.get("averageSpeed", 0) or 0
            type_key    = a.get("activityType", {}).get("typeKey", "")
            is_run      = "run" in type_key.lower()

            pace_decimal = round(1 / (avg_speed * 60 / 1000), 2) if (is_run and avg_speed > 0) else None

            detail   = {}
            hr_zones = {f"zone_{i}_mins": None for i in range(1, 6)}
            weather  = {"temp_c": None, "humidity_pct": None, "wind_kph": None, "weather_desc": ""}

            if activity_id:
                try:
                    detail = client.get_activity(activity_id)
                except Exception:
                    pass
                hr_zones = fetch_hr_zones(client, activity_id)

            calories = detail.get("calories") or detail.get("activeKilocalories") or a.get("calories")

            summary  = detail.get("summaryDTO", {})
            lat      = summary.get("startLatitude") or a.get("startLatitude")
            lon      = summary.get("startLongitude") or a.get("startLongitude")
            start_dt = summary.get("startTimeLocal", "")
            try:
                act_hour = int(start_dt[11:13]) if len(start_dt) >= 13 else 12
            except (ValueError, IndexError):
                act_hour = 12

            weather = fetch_weather(lat, lon, date_str, act_hour)

            # Splits
            splits = []
            if activity_id:
                try:
                    split_data = client.get_activity_splits(activity_id)
                    items = split_data if isinstance(split_data, list) else (
                        split_data.get("lapDTOs") or split_data.get("splits") or [])
                    for i, lap in enumerate(items, start=1):
                        d_m = lap.get("distance", 0) or 0
                        d_s = lap.get("duration", 0) or lap.get("elapsedDuration", 0) or 0
                        spd = lap.get("averageSpeed", 0) or 0
                        splits.append({
                            "split_number":   i,
                            "split_type":     lap.get("lapTrigger", lap.get("splitType", "lap")),
                            "distance_km":    round(d_m / 1000, 3) if d_m else None,
                            "duration":       decimal_mins_to_mmss(round(d_s / 60, 2)) if d_s else None,
                            "pace_min_km":    decimal_mins_to_mmss(round(1 / (spd * 60 / 1000), 2)) if spd > 0 else None,
                            "avg_hr":         lap.get("averageHR") or lap.get("averageHr"),
                            "max_hr":         lap.get("maxHR") or lap.get("maxHr"),
                            "avg_cadence":    lap.get("averageRunCadence") or lap.get("averageCadence"),
                            "elevation_gain": lap.get("elevationGain"),
                        })
                except Exception as e:
                    print(f"    Splits failed {activity_id}: {e}")

            activities.append({
                "activity_id":    activity_id,
                "name":           a.get("activityName", ""),
                "type":           type_key,
                "distance_km":    round(distance_m / 1000, 2) if distance_m else None,
                "duration":       decimal_mins_to_mmss(round(duration_s / 60, 2)) if duration_s else None,
                "avg_hr":         a.get("averageHR"),
                "max_hr":         a.get("maxHR"),
                "avg_pace_min_km": decimal_mins_to_mmss(pace_decimal),
                "elevation_gain_m": a.get("elevationGain"),
                "training_effect":  a.get("aerobicTrainingEffect"),
                "vo2max":           a.get("vO2MaxValue"),
                "calories":         calories,
                "hr_drift_pct":     fetch_hr_drift(detail),
                "hr_zones":         hr_zones,
                "weather":          weather,
                "splits":           splits,
            })
    except Exception as e:
        print(f"  Activities failed {date_str}: {e}")
    return activities

# -- Main ---------------------------------------------------------------------

def load_existing():
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"Loaded existing data.json with {len(data.get('days', {}))} days")
            return data
        except Exception as e:
            print(f"Could not load existing data.json: {e}")
    return {"meta": {}, "days": {}}

def save(data):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"Saved data.json ({len(data['days'])} days)")

def main():
    print("\n=== Garmin -> data.json ===\n")

    existing = load_existing()
    days     = existing.get("days", {})

    today         = date.today()
    refresh_cutoff = (today - timedelta(days=REFRESH_DAYS - 1)).isoformat()
    all_dates     = [(today - timedelta(days=i)).isoformat() for i in range(DAYS_TO_SYNC - 1, -1, -1)]
    dates_to_fetch = [d for d in all_dates if d not in days or d >= refresh_cutoff]

    if not dates_to_fetch:
        print("Nothing new to fetch")
        save(existing)
        return

    print(f"Fetching {len(dates_to_fetch)} date(s)...\n")
    client = get_garmin_client()

    for date_str in dates_to_fetch:
        print(f"  {date_str}...")
        wellness   = fetch_wellness(client, date_str)
        activities = fetch_activities(client, date_str)
        days[date_str] = {
            "date":       date_str,
            "wellness":   wellness,
            "activities": activities,
        }

    existing["days"] = dict(sorted(days.items()))
    existing["meta"] = {
        "updated_at":  today.isoformat(),
        "total_days":  len(existing["days"]),
        "first_date":  min(existing["days"].keys()),
        "last_date":   max(existing["days"].keys()),
    }

    save(existing)
    print("\nDone.\n")

if __name__ == "__main__":
    main()
