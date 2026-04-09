#!/usr/bin/env python3
"""Compare daily TSS between Garmin data (Python engine) and show per-activity breakdown
around a target date to help diagnose TSB differences with Strava/JS version."""

import json
from collections import defaultdict
from pathlib import Path
from datetime import date, timedelta, datetime

DATA_DIR = Path(__file__).parent / "garmin_data"

RUN_TYPES = ("running", "trail_running", "treadmill_running")
BIKE_TYPES = ("road_biking", "mountain_biking", "cycling", "virtual_ride", "indoor_cycling", "gravel_cycling")
SWIM_TYPES = ("lap_swimming", "open_water_swimming")

def calc_tss(a, lthr_run, lthr_bike, ftp):
    avg_hr = a.get("averageHR", 0)
    dur = a.get("duration", 0)
    np_val = a.get("normPower", 0) or 0
    atype = a.get("activityType", {}).get("typeKey", "")
    if dur <= 0:
        return 0, "no_dur"
    if atype in BIKE_TYPES and ftp and ftp > 0 and np_val > 0:
        intensity = np_val / ftp
        return round((dur / 3600) * intensity * intensity * 100), "power"
    if avg_hr <= 0:
        return 0, "no_hr"
    lthr = lthr_run if atype in RUN_TYPES else lthr_bike if atype in BIKE_TYPES else lthr_run
    if lthr <= 0:
        return 0, "no_lthr"
    return round((dur / 3600) * (avg_hr / lthr) ** 3.5 * 100), "hr"

# Load Garmin data with same overrides
with open(DATA_DIR / "activities.json") as f:
    acts = json.load(f)

ftp = 300
lthr_run = 177
lthr_bike = 172

# Build daily TSS
daily = defaultdict(list)
for a in acts:
    if not a.get("duration"):
        continue
    d = a["startTimeLocal"][:10]
    tss, method = calc_tss(a, lthr_run, lthr_bike, ftp)
    atype = a.get("activityType", {}).get("typeKey", "")
    daily[d].append({
        "name": a.get("activityName", "?"),
        "type": atype,
        "dur": a.get("duration", 0),
        "avgHR": a.get("averageHR", 0),
        "np": a.get("normPower", 0) or 0,
        "tss": tss,
        "method": method,
    })

# Build TSB
all_dates = sorted(daily.keys())
start = datetime.strptime(all_dates[0], "%Y-%m-%d").date()
end = date.today()

ctl, atl = 0.0, 0.0
tsb_data = {}
current = start
while current <= end:
    d = current.isoformat()
    day_tss = sum(a["tss"] for a in daily.get(d, []))
    ctl += (day_tss - ctl) / 42
    atl += (day_tss - atl) / 7
    tsb_data[d] = {"tss": day_tss, "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(ctl - atl, 1)}
    current += timedelta(days=1)

# Print March 18-31 detail
print("=" * 100)
print(f"GARMIN/PYTHON TSS BREAKDOWN  |  FTP={ftp}  LTHR_Run={lthr_run}  LTHR_Bike={lthr_bike}")
print("=" * 100)
print()

for d_offset in range(-14, 1):
    d = (date(2026, 3, 25) + timedelta(days=d_offset)).isoformat()
    tsb_row = tsb_data.get(d, {})
    day_acts = daily.get(d, [])
    day_tss = sum(a["tss"] for a in day_acts)
    print(f"--- {d} --- TSS={day_tss}  CTL={tsb_row.get('ctl','-')}  ATL={tsb_row.get('atl','-')}  TSB={tsb_row.get('tsb','-')}")
    for a in day_acts:
        print(f"    {a['name']:40s}  type={a['type']:20s}  dur={a['dur']:6.0f}s  avgHR={a['avgHR']:3.0f}  NP={a['np']:3.0f}  TSS={a['tss']:3d} ({a['method']})")
    if not day_acts:
        print("    (rest day)")
    print()

# Also print the effective values from the Python engine for comparison
print("\n" + "=" * 100)
print("COPY THE JS OUTPUT BELOW FOR COMPARISON")
print("=" * 100)
print("""
In your browser console, run:

const acts = await dbGetAll();
const ov = JSON.parse(localStorage.getItem('trainingOverrides') || '{}');
const D2 = computeAll(acts, ov);
const mar = D2.tsb.filter(r => r.date >= '2026-03-11' && r.date <= '2026-03-25');
mar.forEach(r => console.log(r.date, 'TSS='+r.tss, 'CTL='+r.ctl, 'ATL='+r.atl, 'TSB='+r.tsb));
""")
