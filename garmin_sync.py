#!/usr/bin/env python3
"""
Garmin Connect workout sync — pulls activity history and saves to JSON
for Claude to read and build training protocols.

Usage:
    python3 garmin_sync.py              # incremental sync (new activities only)
    python3 garmin_sync.py --full       # full re-sync (5 years)
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
ACTIVITIES_FILE = DATA_DIR / "activities.json"
FIRST_SYNC_DAYS = 5 * 365  # 5 years for first sync
INCREMENTAL_OVERLAP = 3     # re-fetch last 3 days to catch edits/late syncs


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


def load_existing() -> list:
    """Load existing activities from disk."""
    if ACTIVITIES_FILE.exists():
        try:
            with open(ACTIVITIES_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return []


def get_last_activity_date(activities: list) -> str | None:
    """Find the most recent activity date in existing data."""
    if not activities:
        return None
    latest = None
    for a in activities:
        d = a.get("startTimeLocal", "")[:10]
        if d and (latest is None or d > latest):
            latest = d
    return latest


def fetch_activities(api, start_date: date, end_date: date) -> list:
    print(f"Fetching activities {start_date} → {end_date}...")
    activities = api.get_activities_by_date(str(start_date), str(end_date))
    print(f"  Found {len(activities)} activities.")
    return activities


def merge_activities(existing: list, new: list) -> list:
    """Merge new activities into existing, deduplicating by activityId.
    New activities take precedence (in case data was updated on Garmin)."""
    by_id = {}
    for a in existing:
        aid = a.get("activityId")
        if aid:
            by_id[aid] = a
    for a in new:
        aid = a.get("activityId")
        if aid:
            by_id[aid] = a  # overwrite with newer version
    merged = list(by_id.values())
    merged.sort(key=lambda a: a.get("startTimeLocal", ""), reverse=True)
    return merged


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
    parser.add_argument("--days", type=int, default=None,
                        help="Days of history to pull")
    parser.add_argument("--full", action="store_true",
                        help="Full re-sync (5 years of history)")
    parser.add_argument("--summary", action="store_true",
                        help="Print activity summary after sync")
    parser.add_argument("--sleep", action="store_true",
                        help="Also pull sleep data (last 30 days)")
    args = parser.parse_args()

    api = login()

    # Load existing activities
    existing = load_existing()
    last_date = get_last_activity_date(existing)

    # Determine sync range
    end = date.today()
    if args.full:
        start = end - timedelta(days=FIRST_SYNC_DAYS)
        print(f"Full sync: pulling {FIRST_SYNC_DAYS // 365} years of history...")
    elif args.days:
        start = end - timedelta(days=args.days)
    elif last_date:
        # Incremental: from last activity minus overlap to today
        start = date.fromisoformat(last_date) - timedelta(days=INCREMENTAL_OVERLAP)
        print(f"Incremental sync: last activity was {last_date}, fetching from {start}...")
    else:
        # First sync ever — pull 5 years
        start = end - timedelta(days=FIRST_SYNC_DAYS)
        print(f"First sync: pulling {FIRST_SYNC_DAYS // 365} years of history...")

    # Fetch and merge
    raw_activities = fetch_activities(api, start, end)
    new_cleaned = [clean_activity(a) for a in raw_activities]

    if existing and not args.full:
        activities = merge_activities(existing, new_cleaned)
        new_count = len(activities) - len(existing)
        print(f"  Merged: {new_count} new, {len(activities)} total")
    else:
        activities = new_cleaned
        activities.sort(key=lambda a: a.get("startTimeLocal", ""), reverse=True)
        print(f"  Total: {len(activities)} activities")

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
        sleep = fetch_sleep(api, args.days or 30)
        if sleep:
            save("sleep.json", sleep)
        else:
            print("  No sleep data found.")

    if args.summary:
        print_summary(raw_activities)

    print("\nDone. Run `python3 fitness_analysis.py --open` to view your dashboard.")


if __name__ == "__main__":
    main()
