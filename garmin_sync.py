#!/usr/bin/env python3
"""
Garmin Connect workout sync — pulls activity history and saves to JSON
for Claude to read and build training protocols.

Usage:
    python3 garmin_sync.py              # sync last 90 days
    python3 garmin_sync.py --days 180   # sync last 180 days
    python3 garmin_sync.py --summary    # print summary after sync
    python3 garmin_sync.py --sleep      # also pull last 30 days of sleep
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / "garmin_data"
DATA_DIR.mkdir(exist_ok=True)
TOKEN_DIR = DATA_DIR / ".tokens"


def login():
    from garminconnect import Garmin

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    if not email or not password:
        print("ERROR: Set GARMIN_EMAIL and GARMIN_PASSWORD in garmin/.env")
        sys.exit(1)

    api = Garmin(email, password)

    # Reuse saved tokens if available (avoids re-login every run)
    if TOKEN_DIR.exists():
        try:
            api.login(tokenstore=str(TOKEN_DIR))
            print("Logged in via saved tokens.")
            return api
        except Exception:
            print("Saved tokens expired, re-authenticating...")

    api.login()
    TOKEN_DIR.mkdir(exist_ok=True)
    api.garth.dump(str(TOKEN_DIR))
    print("Logged in and tokens saved.")
    return api


def fetch_activities(api, days: int) -> list:
    end = date.today()
    start = end - timedelta(days=days)
    print(f"Fetching activities {start} → {end}...")
    activities = api.get_activities_by_date(str(start), str(end))
    print(f"  Found {len(activities)} activities.")
    return activities


def fetch_max_metrics(api) -> dict:
    """VO2max, lactate threshold, etc."""
    try:
        return api.get_max_metrics(date.today().isoformat()) or {}
    except Exception:
        return {}


def fetch_training_status(api) -> dict:
    try:
        return api.get_training_status(date.today().isoformat()) or {}
    except Exception:
        return {}


def fetch_hrv(api) -> dict:
    try:
        return api.get_hrv_data(date.today().isoformat()) or {}
    except Exception:
        return {}


def fetch_sleep(api, days: int = 30) -> list:
    records = []
    cap = min(days, 30)  # cap to avoid rate limits
    for i in range(cap):
        d = (date.today() - timedelta(days=i)).isoformat()
        try:
            sleep = api.get_sleep_data(d)
            if sleep:
                records.append(sleep)
        except Exception:
            pass
    return records


def clean_activity(a: dict) -> dict:
    """Keep fields most useful for training analysis."""
    keep = [
        "activityId", "activityName", "activityType",
        "startTimeLocal", "duration", "movingDuration", "distance",
        "averageHR", "maxHR", "calories",
        "averageSpeed", "maxSpeed",
        "averageRunningCadenceInStepsPerMinute",
        "averagePower", "normalizedPower",
        "avgPower", "normPower", "maxPower", "max20MinPower",
        "aerobicTrainingEffect", "anaerobicTrainingEffect",
        "vO2MaxValue", "elevationGain", "elevationLoss",
        "steps", "description",
    ]
    result = {k: a.get(k) for k in keep if a.get(k) is not None}
    # Preserve maxAvgPower_X fields for power curve
    for k, v in a.items():
        if k.startswith("maxAvgPower_") and v is not None:
            result[k] = v
    return result


def save(filename: str, data) -> None:
    path = DATA_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Saved → garmin_data/{filename}")


def print_summary(activities: list):
    from collections import defaultdict

    by_type: dict = defaultdict(list)
    for a in activities:
        atype = (a.get("activityType") or {}).get("typeKey", "unknown")
        by_type[atype].append(a)

    print("\n--- Activity Summary ---")
    for atype, acts in sorted(by_type.items(), key=lambda x: -len(x[1])):
        total_hrs = sum((a.get("duration") or 0) for a in acts) / 3600
        total_km = sum((a.get("distance") or 0) for a in acts) / 1000
        print(f"  {atype:35s} {len(acts):3d} sessions  "
              f"{total_hrs:6.1f} hrs  {total_km:7.1f} km")
    print(f"\n  Total: {len(activities)} activities")


def main():
    parser = argparse.ArgumentParser(description="Sync Garmin workout history")
    parser.add_argument("--days", type=int, default=90,
                        help="Days of history to pull (default: 90)")
    parser.add_argument("--summary", action="store_true",
                        help="Print activity summary after sync")
    parser.add_argument("--sleep", action="store_true",
                        help="Also pull sleep data (last 30 days)")
    args = parser.parse_args()

    api = login()

    # Activities
    raw_activities = fetch_activities(api, args.days)
    activities = [clean_activity(a) for a in raw_activities]
    save("activities.json", activities)

    # VO2max / fitness metrics
    metrics = fetch_max_metrics(api)
    if metrics:
        save("max_metrics.json", metrics)

    # Training load / status
    status = fetch_training_status(api)
    if status:
        save("training_status.json", status)

    # HRV
    hrv = fetch_hrv(api)
    if hrv:
        save("hrv.json", hrv)

    # Sleep (optional flag)
    if args.sleep:
        sleep = fetch_sleep(api, args.days)
        if sleep:
            save("sleep.json", sleep)
        else:
            print("  No sleep data found.")

    if args.summary:
        print_summary(raw_activities)

    print("\nDone. Ask Claude to read garmin_data/ and build your training protocol.")


if __name__ == "__main__":
    main()
