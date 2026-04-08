#!/usr/bin/env python3
"""
Garmin fitness analysis — processes activity data and generates
a comprehensive training dashboard.

Usage:
    python3 fitness_analysis.py          # generate dashboard
    python3 fitness_analysis.py --open   # generate and open in browser
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta, datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "garmin_data"
OUTPUT_DIR = DATA_DIR
OVERRIDES_FILE = DATA_DIR / "user_overrides.json"

KM_TO_MI = 0.621371
M_TO_YD = 1.09361


def load_activities():
    with open(DATA_DIR / "activities.json") as f:
        return json.load(f)


def load_overrides():
    if OVERRIDES_FILE.exists():
        with open(OVERRIDES_FILE) as f:
            return json.load(f)
    return {}


# ── Auto-estimation functions ─────────────────────────────────────────

def estimate_max_hr(acts):
    max_hrs = {}
    for a in acts:
        sport = a.get("activityType", {}).get("typeKey", "")
        hr = a.get("maxHR", 0)
        if hr > max_hrs.get(sport, 0):
            max_hrs[sport] = hr
    run_max = max(max_hrs.get("running", 0), max_hrs.get("trail_running", 0))
    bike_max = max(max_hrs.get("road_biking", 0), max_hrs.get("mountain_biking", 0),
                   max_hrs.get("cycling", 0), max_hrs.get("virtual_ride", 0),
                   max_hrs.get("indoor_cycling", 0))
    swim_max = max_hrs.get("lap_swimming", 0)
    return {"run": int(run_max), "bike": int(bike_max), "swim": int(swim_max)}


def estimate_lthr_run(acts, max_hr):
    candidates = []
    for a in acts:
        atype = a.get("activityType", {}).get("typeKey", "")
        if atype not in ("running", "trail_running"):
            continue
        avg_hr = a.get("averageHR", 0)
        dur = a.get("duration", 0)
        if avg_hr <= 0 or dur < 1200:
            continue
        if dur > 3600 and avg_hr > max_hr * 0.85:
            candidates.append(avg_hr + 3)
        elif dur >= 1200 and avg_hr > max_hr * 0.83:
            bump = 3 if dur > 2400 else 2
            candidates.append(avg_hr + bump)
    if not candidates:
        return int(max_hr * 0.89)
    candidates.sort(reverse=True)
    top = candidates[:min(5, len(candidates))]
    return int(sum(top) / len(top))


def estimate_lthr_bike(acts, max_hr):
    candidates = []
    bike_types = ("road_biking", "mountain_biking", "cycling",
                  "virtual_ride", "indoor_cycling", "gravel_cycling")
    for a in acts:
        atype = a.get("activityType", {}).get("typeKey", "")
        if atype not in bike_types:
            continue
        avg_hr = a.get("averageHR", 0)
        dur = a.get("duration", 0)
        np_val = a.get("normPower", 0) or 0
        if avg_hr <= 0 or dur < 1200:
            continue
        if dur <= 5400 and avg_hr > max_hr * 0.80:
            bump = 4 if dur > 2400 else 2
            candidates.append(avg_hr + bump)
        elif np_val > 220 and avg_hr > max_hr * 0.75:
            candidates.append(avg_hr + 5)
    if not candidates:
        return int(max_hr * 0.87)
    candidates.sort(reverse=True)
    top = candidates[:min(5, len(candidates))]
    return int(sum(top) / len(top))


def estimate_ftp(acts):
    bike_types = ("road_biking", "mountain_biking", "cycling",
                  "virtual_ride", "indoor_cycling", "gravel_cycling")
    best = 0
    for a in acts:
        if a.get("activityType", {}).get("typeKey", "") not in bike_types:
            continue
        p20 = a.get("max20MinPower", 0) or 0
        if p20 > best:
            best = p20
    return int(best * 0.95) if best > 0 else None


def estimate_threshold_pace(acts):
    """Returns threshold pace in sec/mi."""
    candidates = []
    for a in acts:
        if a.get("activityType", {}).get("typeKey", "") != "running":
            continue
        speed = a.get("averageSpeed", 0)
        dur = a.get("duration", 0)
        avg_hr = a.get("averageHR", 0)
        if speed <= 0 or dur < 1200 or not avg_hr or avg_hr <= 155:
            continue
        candidates.append(1609.344 / speed)
    if not candidates:
        return 530
    candidates.sort()
    top = candidates[:min(5, len(candidates))]
    return int(sum(top) / len(top))


def estimate_css(acts):
    """Returns CSS in sec/100yd."""
    swims = []
    for a in acts:
        if a.get("activityType", {}).get("typeKey") != "lap_swimming":
            continue
        dist_m = a.get("distance", 0)
        avg_speed = a.get("averageSpeed", 0)
        if dist_m > 0 and avg_speed > 0:
            dur = dist_m / avg_speed  # moving duration
        else:
            dur = a.get("duration", 0)
        if dist_m > 0 and dur > 0:
            dist_yd = dist_m * M_TO_YD
            swims.append((dur / dist_yd) * 100)
    if not swims:
        return 110
    swims.sort()
    idx = max(0, len(swims) // 5)
    return int(swims[idx])


# ── Activity classification ──────────────────────────────────────────

RUN_TYPES = ("running", "trail_running", "treadmill_running")
BIKE_TYPES = ("road_biking", "mountain_biking", "cycling",
              "virtual_ride", "indoor_cycling", "gravel_cycling")
SWIM_TYPES = ("lap_swimming", "open_water_swimming")


def sport_category(atype):
    if atype in RUN_TYPES:
        return "run"
    if atype in BIKE_TYPES:
        return "bike"
    if atype in SWIM_TYPES:
        return "swim"
    return "other"


# ── TSS calculation ──────────────────────────────────────────────────

def calc_tss(activity, lthr_run, lthr_bike, ftp):
    avg_hr = activity.get("averageHR", 0)
    dur = activity.get("duration", 0)
    np_val = activity.get("normPower", 0) or 0
    atype = activity.get("activityType", {}).get("typeKey", "")
    if dur <= 0:
        return 0
    # Power TSS for cycling
    if atype in BIKE_TYPES and ftp and ftp > 0 and np_val > 0:
        intensity = np_val / ftp
        return round((dur / 3600) * intensity * intensity * 100)
    # hrTSS fallback
    if avg_hr <= 0:
        return 0
    lthr = lthr_run if atype in RUN_TYPES else lthr_bike if atype in BIKE_TYPES else lthr_run
    if lthr <= 0:
        return 0
    return round((dur / 3600) * (avg_hr / lthr) ** 3.5 * 100)


# ── TSB model ────────────────────────────────────────────────────────

def build_tsb(acts, lthr_run, lthr_bike, ftp, lookahead=5):
    daily_tss = defaultdict(float)
    for a in acts:
        if not a.get("duration"):
            continue
        d = a["startTimeLocal"][:10]
        daily_tss[d] += calc_tss(a, lthr_run, lthr_bike, ftp)

    all_dates = sorted(daily_tss.keys())
    if not all_dates:
        return []

    start = datetime.strptime(all_dates[0], "%Y-%m-%d").date()
    end = date.today() + timedelta(days=lookahead)

    ctl, atl = 0.0, 0.0
    result = []
    current = start
    while current <= end:
        d = current.isoformat()
        tss = daily_tss.get(d, 0)
        ctl += (tss - ctl) / 42
        atl += (tss - atl) / 7
        result.append({
            "date": d,
            "tss": round(tss, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1),
            "projected": current > date.today(),
        })
        current += timedelta(days=1)
    return result


# ── Weekly volume ────────────────────────────────────────────────────

def weekly_volume(acts):
    weeks = defaultdict(lambda: {"run": 0, "bike": 0, "swim": 0, "other": 0})
    for a in acts:
        dur = a.get("duration", 0)
        if not dur:
            continue
        d = datetime.strptime(a["startTimeLocal"][:10], "%Y-%m-%d").date()
        wk = d.isocalendar()
        key = f"{wk[0]}-W{wk[1]:02d}"
        cat = sport_category(a.get("activityType", {}).get("typeKey", ""))
        weeks[key][cat] += dur / 3600
    # Round values
    for wk in weeks.values():
        for k in wk:
            wk[k] = round(wk[k], 1)
    return dict(sorted(weeks.items()))


# ── Power curve ──────────────────────────────────────────────────────

def build_power_curve(acts):
    """Best power at each standard duration across all rides."""
    # Requested durations: 1s, 5s, 15s, 60s, 2m, 5m, 10m, 20m, 30m, 1h, 2h, 3h, 4h, 5h
    # Map to Garmin field names (seconds)
    durations = [1, 5, 15, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 10800, 14400, 18000]
    labels = ["1s", "5s", "15s", "1min", "2min", "5min", "10min", "20min",
              "30min", "1hr", "2hr", "3hr", "4hr", "5hr"]
    best = {}
    for dur in durations:
        field = f"maxAvgPower_{dur}"
        vals = [a.get(field, 0) or 0 for a in acts
                if a.get("activityType", {}).get("typeKey", "") in BIKE_TYPES]
        best[str(dur)] = round(max(vals)) if vals and max(vals) > 0 else None
    best["_labels"] = labels
    best["_durations"] = durations
    return best


# ── Enriched activity lists per sport ────────────────────────────────

def enrich_runs(acts, lthr_run, lthr_bike, ftp):
    runs = []
    for a in acts:
        atype = a.get("activityType", {}).get("typeKey", "")
        if atype not in RUN_TYPES:
            continue
        speed = a.get("averageSpeed", 0)
        dist_m = a.get("distance", 0)
        runs.append({
            "date": a["startTimeLocal"][:10],
            "name": a.get("activityName", ""),
            "type": atype,
            "duration": round(a.get("duration", 0) / 60, 1),
            "distance_mi": round(dist_m / 1609.344, 2) if dist_m else 0,
            "pace_mi": round(1609.344 / speed) if speed > 0 else None,
            "avgHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"),
            "cadence": a.get("averageRunningCadenceInStepsPerMinute"),
            "tss": calc_tss(a, lthr_run, lthr_bike, ftp),
            "elevGain": a.get("elevationGain"),
            "vo2max": a.get("vO2MaxValue"),
        })
    runs.sort(key=lambda x: x["date"], reverse=True)
    return runs


def enrich_rides(acts, lthr_run, lthr_bike, ftp):
    rides = []
    for a in acts:
        atype = a.get("activityType", {}).get("typeKey", "")
        if atype not in BIKE_TYPES:
            continue
        dist_m = a.get("distance", 0)
        rides.append({
            "date": a["startTimeLocal"][:10],
            "name": a.get("activityName", ""),
            "type": atype,
            "duration": round(a.get("duration", 0) / 60, 1),
            "distance_mi": round(dist_m / 1609.344, 2) if dist_m else 0,
            "avgPower": a.get("avgPower"),
            "normPower": round(a["normPower"], 1) if a.get("normPower") else None,
            "maxPower": a.get("maxPower"),
            "max20Min": round(a["max20MinPower"], 1) if a.get("max20MinPower") else None,
            "avgHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"),
            "tss": calc_tss(a, lthr_run, lthr_bike, ftp),
            "elevGain": a.get("elevationGain"),
            "vo2max": a.get("vO2MaxValue"),
        })
    rides.sort(key=lambda x: x["date"], reverse=True)
    return rides


def enrich_swims(acts, lthr_run, lthr_bike, ftp):
    swims = []
    for a in acts:
        if a.get("activityType", {}).get("typeKey") not in SWIM_TYPES:
            continue
        dist_m = a.get("distance", 0)
        avg_speed = a.get("averageSpeed", 0)
        if dist_m > 0 and avg_speed > 0:
            dur = dist_m / avg_speed  # moving duration from moving speed
        else:
            dur = a.get("duration", 0)
        dist_yd = dist_m * M_TO_YD if dist_m else 0
        pace = round((dur / dist_yd) * 100) if dist_yd > 0 else None
        swims.append({
            "date": a["startTimeLocal"][:10],
            "name": a.get("activityName", ""),
            "duration": round(dur / 60, 1),
            "distance_yd": round(dist_yd),
            "pace_100yd": pace,
            "avgHR": a.get("averageHR"),
            "maxHR": a.get("maxHR"),
            "tss": calc_tss(a, lthr_run, lthr_bike, ftp),
        })
    swims.sort(key=lambda x: x["date"], reverse=True)
    return swims


# ── VO2max trends ────────────────────────────────────────────────────

def vo2max_trends(acts):
    run, bike = [], []
    for a in acts:
        v = a.get("vO2MaxValue")
        if not v:
            continue
        atype = a.get("activityType", {}).get("typeKey", "")
        entry = {"date": a["startTimeLocal"][:10], "value": v}
        if atype in RUN_TYPES:
            run.append(entry)
        elif atype in BIKE_TYPES:
            bike.append(entry)
    return {"run": sorted(run, key=lambda x: x["date"]),
            "bike": sorted(bike, key=lambda x: x["date"])}


# ── Main computation ─────────────────────────────────────────────────

def compute_all():
    acts = load_activities()
    overrides = load_overrides()
    max_hrs = estimate_max_hr(acts)

    # Auto-calculated values
    auto = {
        "ftp": estimate_ftp(acts),
        "lthr_run": estimate_lthr_run(acts, max_hrs["run"]),
        "lthr_bike": estimate_lthr_bike(acts, max_hrs["bike"]),
        "threshold_pace_mi": estimate_threshold_pace(acts),
        "css_100yd": estimate_css(acts),
        "max_hr_run": max_hrs["run"],
        "max_hr_bike": max_hrs["bike"],
        "max_hr_swim": max_hrs["swim"],
        "weight_lb": 210,
    }

    # Effective values = overrides merged over auto
    effective = {**auto}
    effective.update({k: v for k, v in overrides.items() if v is not None})

    # TSB
    tsb = build_tsb(acts, effective["lthr_run"], effective["lthr_bike"],
                    effective.get("ftp"), lookahead=14)

    # Garmin status
    garmin_status = {}
    try:
        with open(DATA_DIR / "training_status.json") as f:
            ts = json.load(f)
        tlb = list(ts.get("mostRecentTrainingLoadBalance", {})
                   .get("metricsTrainingLoadBalanceDTOMap", {}).values())
        tsd = list(ts.get("mostRecentTrainingStatus", {})
                   .get("latestTrainingStatusData", {}).values())
        if tlb:
            garmin_status["load_low"] = int(tlb[0].get("monthlyLoadAerobicLow", 0))
            garmin_status["load_high"] = int(tlb[0].get("monthlyLoadAerobicHigh", 0))
            garmin_status["load_anaerobic"] = int(tlb[0].get("monthlyLoadAnaerobic", 0))
        if tsd:
            garmin_status["training_status"] = tsd[0].get("trainingStatusFeedbackPhrase", "")
            atl_dto = tsd[0].get("acuteTrainingLoadDTO", {})
            garmin_status["acwr"] = atl_dto.get("acwrPercent")
            garmin_status["acwr_status"] = atl_dto.get("acwrStatus")
    except Exception:
        pass

    # HRV
    hrv = {}
    try:
        with open(DATA_DIR / "hrv.json") as f:
            h = json.load(f)
        summary = h.get("hrvSummary", {})
        hrv = {
            "weeklyAvg": summary.get("weeklyAvg"),
            "lastNightAvg": summary.get("lastNightAvg"),
            "status": summary.get("status"),
            "baseline": summary.get("baseline"),
        }
    except Exception:
        pass

    return {
        "generated": date.today().isoformat(),
        "auto": auto,
        "overrides": overrides,
        "effective": effective,
        "tsb": tsb,
        "weekly_volume": weekly_volume(acts),
        "power_curve": build_power_curve(acts),
        "runs": enrich_runs(acts, effective["lthr_run"], effective["lthr_bike"], effective.get("ftp")),
        "rides": enrich_rides(acts, effective["lthr_run"], effective["lthr_bike"], effective.get("ftp")),
        "swims": enrich_swims(acts, effective["lthr_run"], effective["lthr_bike"], effective.get("ftp")),
        "vo2max": vo2max_trends(acts),
        "garmin_status": garmin_status,
        "hrv": hrv,
    }


# ── HTML generation ──────────────────────────────────────────────────

def generate_html(data):
    data_json = json.dumps(data, default=str)
    return HTML_TEMPLATE.replace("__DATA_JSON__", data_json)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TrainingHub</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root, [data-theme="dark"] {
  --bg: #111318;
  --bg-sidebar: #16191f;
  --bg-card: #1a1e26;
  --bg-hover: #1f2430;
  --bg-input: #111318;
  --border: #2a2f3a;
  --border-active: #3d4455;
  --text: #dce0e8;
  --text-dim: #959dad;
  --text-faint: #5c6370;
  --blue: #5ba0f7;
  --blue-soft: rgba(91,160,247,0.15);
  --green: #50c878;
  --green-soft: rgba(80,200,120,0.15);
  --red: #ef6461;
  --red-soft: rgba(239,100,97,0.15);
  --purple: #bb9af7;
  --purple-soft: rgba(187,154,247,0.15);
  --yellow: #e0c067;
  --yellow-soft: rgba(224,192,103,0.15);
  --orange: #e6945a;
  --chart-grid: #1e2733;
  --chart-tick: #5c6370;
  /* Sport colors */
  --color-run: var(--green);
  --color-bike: var(--blue);
  --color-swim: var(--purple);
  --color-other: #8891a0;
  --color-form: #8891a0;
  /* Radius scale */
  --radius: 12px;
  --radius-sm: 8px;
  --radius-xs: 4px;
  /* Spacing scale */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-7: 28px;
  --space-8: 32px;
  --space-9: 36px;
  /* Font scale */
  --text-xs: 10px;
  --text-sm: 11px;
  --text-base: 13px;
  --text-md: 14px;
  --text-lg: 16px;
  --text-xl: 18px;
  --text-2xl: 22px;
  --text-3xl: 28px;
  /* Chart */
  --chart-height: 300px;
  --chart-height-sm: 240px;
  --chart-line-width: 2;
  --chart-line-width-bold: 2.5;
  --chart-point-radius: 4;
  --chart-tension: 0.3;
  --chart-tension-tight: 0.2;
  /* Layout */
  --sidebar-w: 240px;
  --sidebar-collapsed: 60px;
  --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --mono: 'SF Mono', 'Fira Code', 'Cascadia Code', Menlo, monospace;
}
[data-theme="light"] {
  --bg: #f5f6f8;
  --bg-sidebar: #ffffff;
  --bg-card: #ffffff;
  --bg-hover: #f0f1f4;
  --bg-input: #f5f6f8;
  --border: #e0e3e8;
  --border-active: #c8cdd5;
  --text: #1a1d24;
  --text-dim: #5a6172;
  --text-faint: #8891a0;
  --blue: #2b7de9;
  --blue-soft: rgba(43,125,233,0.1);
  --green: #1a9d48;
  --green-soft: rgba(26,157,72,0.1);
  --red: #dc3d3d;
  --red-soft: rgba(220,61,61,0.1);
  --purple: #8b5cf6;
  --purple-soft: rgba(139,92,246,0.1);
  --yellow: #b8960c;
  --yellow-soft: rgba(184,150,12,0.1);
  --orange: #c97218;
  --chart-grid: #e8ebef;
  --chart-tick: #8891a0;
  --color-run: var(--green);
  --color-bike: var(--blue);
  --color-swim: var(--purple);
  --color-other: #8891a0;
  --color-form: #8891a0;
}

/* ── Reset ── */
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
html { -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
body { font-family: var(--font); background: var(--bg); color: var(--text); line-height: 1.55; min-height: 100vh; font-size: 14px; }

/* ── Sidebar ── */
.sidebar {
  position: fixed; top: 0; left: 0; bottom: 0; width: var(--sidebar-w);
  background: var(--bg-sidebar); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; z-index: 300;
  transition: width 0.25s cubic-bezier(0.4,0,0.2,1);
  overflow: hidden;
}
.sidebar.collapsed { width: var(--sidebar-collapsed); }
.sidebar-header {
  display: flex; align-items: center; gap: 10px; padding: 18px 16px;
  border-bottom: 1px solid var(--border); min-height: 60px; flex-shrink: 0;
}
.theme-toggle {
  position: fixed; top: 16px; right: 20px; z-index: 400;
  width: 36px; height: 36px; border-radius: 50%;
  background: var(--bg-card); border: 1px solid var(--border);
  color: var(--text-dim); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: all 0.2s; font-size: 16px;
}
.theme-toggle:hover { background: var(--bg-hover); color: var(--text); border-color: var(--border-active); }
.sidebar-header svg { width: 24px; height: 24px; color: var(--blue); flex-shrink: 0; }
.sidebar-header h1 { font-size: 16px; font-weight: 700; white-space: nowrap; }
.sidebar-header h1 span { color: var(--blue); }
.sidebar.collapsed .sidebar-header h1 { display: none; }
.sidebar-toggle {
  position: fixed; top: 18px; left: calc(var(--sidebar-w) - 12px); width: 24px; height: 24px;
  background: var(--bg-sidebar); border: 1px solid var(--border); border-radius: 50%;
  color: var(--text-dim); cursor: pointer; display: flex; align-items: center; justify-content: center;
  font-size: 14px; line-height: 1; z-index: 310; transition: all 0.25s cubic-bezier(0.4,0,0.2,1);
}
.sidebar.collapsed ~ .sidebar-toggle { left: calc(var(--sidebar-collapsed) - 12px); }
.sidebar-toggle:hover { background: var(--bg-hover); color: var(--text); }

.sidebar-nav { flex: 1; padding: 12px 8px; overflow-y: auto; }
.nav-group { margin-bottom: 8px; }
.nav-group-label {
  font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 1.2px;
  color: var(--text-faint); padding: 8px 12px 6px; white-space: nowrap;
}
.sidebar.collapsed .nav-group-label { text-align: center; font-size: 0; }
.sidebar.collapsed .nav-group-label::after { content: '\2022'; font-size: 10px; }
.nav-item {
  display: flex; align-items: center; gap: 10px; padding: 9px 12px;
  border-radius: var(--radius-sm); cursor: pointer; color: var(--text-dim);
  font-size: 13px; font-weight: 500; transition: all 0.15s; white-space: nowrap;
  border: none; background: none; width: 100%; text-align: left; font-family: var(--font);
}
.nav-item:hover { background: var(--bg-hover); color: var(--text); }
.nav-item.active { background: var(--blue-soft); color: var(--blue); }
.nav-item svg { width: 18px; height: 18px; flex-shrink: 0; opacity: 0.7; }
.nav-item.active svg { opacity: 1; }
.sidebar.collapsed .nav-item span { display: none; }
.sidebar.collapsed .nav-item { justify-content: center; padding: 9px; }

.nav-item.future { opacity: 0.35; cursor: default; }
.nav-item.future:hover { background: none; color: var(--text-dim); }

.sidebar-footer {
  padding: 12px 16px; border-top: 1px solid var(--border); flex-shrink: 0;
}
.sidebar-footer .sync-info {
  font-size: 11px; color: var(--text-faint); white-space: nowrap;
}
.sidebar.collapsed .sidebar-footer .sync-info { display: none; }

/* ── Main Content ── */
.main {
  margin-left: var(--sidebar-w); min-height: 100vh;
  transition: margin-left 0.25s cubic-bezier(0.4,0,0.2,1);
}
.sidebar.collapsed ~ .main { margin-left: var(--sidebar-collapsed); }

.page { display: none; padding: 32px 36px 200px; max-width: 1360px; }
.page.active { display: block; animation: fadeIn 0.15s ease; }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

/* ── Page Header ── */
.page-title { font-size: 22px; font-weight: 700; margin-bottom: 4px; letter-spacing: -0.3px; }
.page-desc { font-size: 13px; color: var(--text-dim); max-width: 640px; line-height: 1.6; margin-bottom: 28px; }

/* ── Grid / Stack ── */
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 20px; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
.stack { display: flex; flex-direction: column; gap: 20px; }
.row { display: flex; gap: 20px; }
.gap-md { gap: 20px; }
.mb-lg { margin-bottom: 28px; }
.mb-md { margin-bottom: 20px; }

/* ── Cards ── */
.card {
  background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 24px;
}
.card-label {
  font-size: 14px; font-weight: 600; color: var(--text-dim); margin-bottom: 18px;
}
.card-title-row {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 18px;
}
.card-title-row h3 { font-size: 14px; font-weight: 600; color: var(--text-dim); }
.badge {
  font-size: 11px; font-weight: 500; padding: 3px 10px; border-radius: 12px;
  background: var(--blue-soft); color: var(--blue);
}
.range-toggle { display: flex; gap: 2px; background: var(--bg); border-radius: 8px; padding: 2px; border: 1px solid var(--border); }
.range-btn {
  font-size: 11px; font-weight: 500; padding: 3px 10px; border-radius: 6px;
  background: none; border: none; color: var(--text-dim); cursor: pointer; transition: all 0.15s;
}
.range-btn:hover { color: var(--text); }
.range-btn.active { background: var(--blue-soft); color: var(--blue); }

/* ── Form Summary (dashboard hero) ── */
.form-hero {
  display: flex; align-items: center; gap: 28px; padding: 8px 0;
}
.form-ring {
  width: 120px; height: 120px; flex-shrink: 0; position: relative;
}
.form-ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.form-ring circle { fill: none; stroke-width: 6; }
.form-ring .bg { stroke: var(--border); }
.form-ring .fg { stroke-linecap: round; transition: stroke-dashoffset 0.6s ease, stroke 0.3s ease; }
.form-ring .center {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.form-ring .center .number { font-size: 28px; font-weight: 700; font-family: var(--mono); line-height: 1; }
.form-ring .center .unit { font-size: 10px; color: var(--text-faint); margin-top: 2px; }
.form-summary h3 { font-size: 18px; font-weight: 700; margin-bottom: 4px; }
.form-summary p { font-size: 13px; color: var(--text-dim); line-height: 1.5; }

/* ── Stat Boxes ── */
.stats-row {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px;
}
.stat {
  padding: 14px 16px; background: var(--bg); border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}
.stat .value {
  font-size: 22px; font-weight: 700; font-family: var(--mono);
  letter-spacing: -0.5px; line-height: 1.2;
}
.stat .value.blue { color: var(--blue); }
.stat .value.green { color: var(--green); }
.stat .value.red { color: var(--red); }
.stat .value.purple { color: var(--purple); }
.stat .value.yellow { color: var(--yellow); }
.stat .label { font-size: 11px; color: var(--text-faint); margin-top: 4px; font-weight: 500; }

/* ── Status Pill ── */
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; font-weight: 600; padding: 5px 14px; border-radius: 20px;
}
.pill.productive { background: var(--green-soft); color: var(--green); }
.pill.maintaining { background: var(--blue-soft); color: var(--blue); }
.pill.detraining { background: var(--yellow-soft); color: var(--yellow); }
.pill.overreaching { background: var(--red-soft); color: var(--red); }
.pill.recovery { background: var(--purple-soft); color: var(--purple); }
.pill.unproductive { background: var(--red-soft); color: var(--red); }
.pill.default { background: rgba(255,255,255,0.05); color: var(--text-dim); }

/* ── Progress Bar ── */
.progress-track { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; }
.progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }

/* ── Tables ── */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
thead { position: sticky; top: 0; z-index: 1; }
th {
  text-align: left; padding: 10px 12px; color: var(--text-faint); font-weight: 600;
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border); background: var(--bg-card);
}
td { padding: 9px 12px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
tr:hover td { background: var(--bg-hover); }
.tbl-wrap { max-height: 520px; overflow-y: auto; }

/* ── Charts ── */
.chart-wrap { position: relative; height: var(--chart-height); }
.chart-wrap-sm { position: relative; height: var(--chart-height-sm); }
.chart-note { font-size: 11px; color: var(--text-faint); margin-top: 10px; line-height: 1.5; }

/* ── Form Legend ── */
.form-legend { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; justify-content: center; }
.form-legend span {
  font-size: 11px; font-weight: 500; padding: 4px 12px; border-radius: 6px;
}

/* ── Settings ── */
.setting-row {
  display: grid; grid-template-columns: 220px 140px 120px auto; align-items: center; gap: 16px;
  padding: 16px 0; border-bottom: 1px solid var(--border);
}
.setting-row:last-of-type { border-bottom: none; }
.setting-row .setting-label { font-size: 13px; font-weight: 500; }
.setting-row .setting-desc { font-size: 11px; color: var(--text-faint); margin-top: 2px; }
.setting-row .auto-val { font-size: 12px; color: var(--text-faint); font-family: var(--mono); }
.setting-row input[type="number"] {
  background: var(--bg-input); border: 1px solid var(--border); color: var(--text);
  padding: 8px 12px; border-radius: var(--radius-sm); width: 100%; font-size: 13px;
  font-family: var(--mono); transition: border-color 0.2s;
}
.setting-row input:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px var(--blue-soft); }
/* ── Insights Card ── */
.insights-header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.insights-header h3 { font-size: 14px; font-weight: 600; color: var(--text-dim); }
.insights-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.insight {
  padding: 14px 16px; background: var(--bg); border-radius: var(--radius-sm);
  border: 1px solid var(--border); border-left: 3px solid var(--blue);
}
.insight.green { border-left-color: var(--green); }
.insight.yellow { border-left-color: var(--yellow); }
.insight.red { border-left-color: var(--red); }
.insight.purple { border-left-color: var(--purple); }
.insight.blue { border-left-color: var(--blue); }
.insight-title {
  font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
  color: var(--text-faint); margin-bottom: 6px;
}
.insight-body { font-size: 13px; color: var(--text); line-height: 1.5; }
.insight-body strong { font-weight: 600; }

.learn-content { max-width: 800px; }
.learn-card { margin-bottom: 20px; }
.learn-card p { font-size: 14px; line-height: 1.7; color: var(--text); margin: 0 0 12px; }
.learn-card ul { font-size: 14px; line-height: 1.7; color: var(--text); margin: 0 0 12px; padding-left: 20px; }
.learn-card li { margin-bottom: 4px; }
.learn-sub { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border); }
.learn-sub h4 { font-size: 14px; font-weight: 600; margin: 0 0 8px; color: var(--text); }
.learn-formula {
  font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px;
  background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-xs);
  padding: 10px 16px; margin: 10px 0 14px; color: var(--blue); font-weight: 500;
}
.learn-table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 10px 0 14px; }
.learn-table th {
  text-align: left; padding: 8px 12px; border-bottom: 2px solid var(--border);
  font-weight: 600; color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
}
.learn-table td { padding: 7px 12px; border-bottom: 1px solid var(--border); color: var(--text); }
.learn-table tbody tr:last-child td { border-bottom: none; }

.evt-form {
  display: grid; grid-template-columns: 140px 110px 1fr 150px auto; gap: 10px; align-items: end;
  margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
}
.evt-form label {
  display: block; font-size: 11px; font-weight: 500; color: var(--text-faint); margin-bottom: 4px;
}
.evt-form input, .evt-form select {
  -webkit-appearance: none; -moz-appearance: none; appearance: none;
  width: 100%; height: 36px; background: var(--bg); border: 1px solid var(--border-active); color: var(--text);
  padding: 0 12px; border-radius: var(--radius-sm); font-size: 13px; font-family: var(--font);
  transition: border-color 0.2s; box-sizing: border-box;
}
.evt-form select {
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='%23888'%3E%3Cpath d='M7 10l5 5 5-5z'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 8px center; padding-right: 28px;
}
.evt-form input:focus, .evt-form select:focus {
  outline: none; border-color: var(--blue); box-shadow: 0 0 0 2px var(--blue-soft);
}
.btn {
  background: none; border: 1px solid var(--border); color: var(--text-dim);
  padding: 7px 14px; border-radius: var(--radius-sm); cursor: pointer; font-size: 12px;
  font-weight: 500; font-family: var(--font); transition: all 0.15s; white-space: nowrap;
}
.btn:hover { color: var(--text); border-color: var(--border-active); background: var(--bg-hover); }
.btn:disabled { opacity: 0.25; cursor: default; }
.btn-primary { background: var(--blue); border-color: var(--blue); color: #fff; }
.btn-primary:hover { background: #6db3ff; border-color: #6db3ff; }

/* ── Zone Tables ── */
.zone-card table td:first-child { font-weight: 500; }
.zone-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 8px; vertical-align: middle;
}

/* ── Quick Log Bar ── */
.quick-log {
  margin-top: 32px; padding: 24px; background: var(--bg-card); border: 1px solid var(--border);
  border-radius: var(--radius);
}
.quick-log .card-label { margin-bottom: 8px; }
.quick-log p { font-size: 13px; color: var(--text-dim); margin-bottom: 16px; line-height: 1.5; }
.quick-log-row {
  display: flex; gap: 10px; align-items: stretch;
}
.quick-log-input {
  flex: 1; background: var(--bg-input); border: 1px solid var(--border); color: var(--text-faint);
  padding: 12px 16px; border-radius: var(--radius-sm); font-size: 14px; font-family: var(--font);
  cursor: default;
}
.quick-log-input::placeholder { color: var(--text-faint); }
.quick-log-btn {
  display: flex; align-items: center; gap: 6px; padding: 0 20px;
  background: var(--bg-hover); border: 1px solid var(--border); border-radius: var(--radius-sm);
  color: var(--text-faint); font-size: 13px; font-weight: 500; font-family: var(--font);
  cursor: default; white-space: nowrap;
}
.quick-log-btn svg { width: 16px; height: 16px; }
.coming-soon-tag {
  display: inline-block; font-size: 10px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--text-faint); background: var(--bg-hover);
  padding: 3px 8px; border-radius: 4px; margin-left: 8px; vertical-align: middle;
}

/* ── Responsive ── */
@media (max-width: 900px) {
  .sidebar { width: var(--sidebar-collapsed); }
  .sidebar .nav-item span, .sidebar .nav-group-label, .sidebar-header h1, .sidebar-footer .sync-info { display: none; }
  .sidebar .nav-item { justify-content: center; padding: 9px; }
  .main { margin-left: var(--sidebar-collapsed); }
  .page { padding: 20px; }
  .stats-row { grid-template-columns: repeat(2, 1fr); }
  .grid { grid-template-columns: 1fr; }
  .grid-3 { grid-template-columns: 1fr; }
  .setting-row { grid-template-columns: 1fr; gap: 8px; }
  .form-hero { flex-direction: column; text-align: center; }
}
</style>
</head>
<body>

<button class="theme-toggle" id="theme-toggle" title="Toggle light/dark mode">
  <svg id="theme-icon-moon" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3c-4.97 0-9 4.03-9 9s4.03 9 9 9 9-4.03 9-9c0-.46-.04-.92-.1-1.36-.98 1.37-2.58 2.26-4.4 2.26-2.98 0-5.4-2.42-5.4-5.4 0-1.81.89-3.42 2.26-4.4-.44-.06-.9-.1-1.36-.1z"/></svg>
  <svg id="theme-icon-sun" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="display:none"><path d="M6.76 4.84l-1.8-1.79-1.41 1.41 1.79 1.79 1.42-1.41zM4 10.5H1v2h3v-2zm9-9.95h-2V3.5h2V.55zm7.45 3.91l-1.41-1.41-1.79 1.79 1.41 1.41 1.79-1.79zm-3.21 13.7l1.79 1.8 1.41-1.41-1.8-1.79-1.4 1.4zM20 10.5v2h3v-2h-3zm-8-5c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm-1 16.95h2V19.5h-2v2.95zm-7.45-3.91l1.41 1.41 1.79-1.8-1.41-1.41-1.79 1.8z"/></svg>
</button>

<!-- ═══════════ SIDEBAR ═══════════ -->
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
    </svg>
    <h1>Training<span>Hub</span></h1>
  </div>
  <nav class="sidebar-nav">
    <div class="nav-group">
      <div class="nav-group-label">Analytics</div>
      <button class="nav-item active" data-tab="dashboard">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"/></svg>
        <span>Dashboard</span>
      </button>
      <button class="nav-item" data-tab="running">
        <svg viewBox="-1 -1 26 26" fill="currentColor"><path d="M13.49 5.48c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm-3.6 13.9l1-4.4 2.1 2v6h2v-7.5l-2.1-2 .6-3c1.3 1.5 3.3 2.5 5.5 2.5v-2c-1.9 0-3.5-1-4.3-2.4l-1-1.6c-.4-.6-1-1-1.7-1-.3 0-.5.1-.8.1l-5.2 2.2v4.7h2v-3.4l1.8-.7-1.6 8.1-4.9-1-.4 2 7 1.4z"/></svg>
        <span>Running</span>
      </button>
      <button class="nav-item" data-tab="cycling">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M15.5 5.5c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zM5 12c-2.8 0-5 2.2-5 5s2.2 5 5 5 5-2.2 5-5-2.2-5-5-5zm0 8.5c-1.9 0-3.5-1.6-3.5-3.5s1.6-3.5 3.5-3.5 3.5 1.6 3.5 3.5-1.6 3.5-3.5 3.5zm5.8-10l2.4-2.4.8.8c1.3 1.3 3 2.1 5 2.1v-2c-1.4 0-2.5-.6-3.4-1.4L13.4 6c-.4-.4-1-.6-1.6-.6s-1.1.2-1.4.6L7.8 8.4c-.4.4-.6.9-.6 1.4 0 .6.2 1.1.6 1.4L11 14v5h2v-6.2l-2.2-2.3zM19 12c-2.8 0-5 2.2-5 5s2.2 5 5 5 5-2.2 5-5-2.2-5-5-5zm0 8.5c-1.9 0-3.5-1.6-3.5-3.5s1.6-3.5 3.5-3.5 3.5 1.6 3.5 3.5-1.6 3.5-3.5 3.5z"/></svg>
        <span>Cycling</span>
      </button>
      <button class="nav-item" data-tab="swimming">
        <svg viewBox="-60 -1020 1080 1080" fill="currentColor"><path d="M80-120v-80q38 0 57-20t75-20q56 0 77 20t57 20q36 0 57-20t77-20q56 0 77 20t57 20q36 0 57-20t77-20q56 0 75 20t57 20v80q-59 0-77.5-20T748-160q-36 0-57 20t-77 20q-56 0-77-20t-57-20q-36 0-57 20t-77 20q-56 0-77-20t-57-20q-36 0-54.5 20T80-120Zm0-180v-80q38 0 57-20t75-20q56 0 77.5 20t56.5 20q36 0 57-20t77-20q56 0 77 20t57 20q36 0 57-20t77-20q56 0 75 20t57 20v80q-59 0-77.5-20T748-340q-36 0-55.5 20T614-300q-57 0-77.5-20T480-340q-38 0-56.5 20T346-300q-59 0-78.5-20T212-340q-36 0-54.5 20T80-300Zm196-204 133-133-40-40q-33-33-70-48t-91-15v-100q75 0 124 16.5t96 63.5l256 256q-17 11-33 17.5t-37 6.5q-36 0-57-20t-77-20q-56 0-77 20t-57 20q-21 0-37-6.5T276-504Zm463-306.5q29 29.5 29 70.5 0 42-29 71t-71 29q-42 0-71-29t-29-71q0-41 29-70.5t71-29.5q42 0 71 29.5Z"/></svg>
        <span>Swimming</span>
      </button>
      <button class="nav-item" data-tab="zones">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM9 17H7v-7h2v7zm4 0h-2V7h2v10zm4 0h-2v-4h2v4z"/></svg>
        <span>Zones</span>
      </button>
      <button class="nav-item" data-tab="learn">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M5 13.18v4L12 21l7-3.82v-4L12 17l-7-3.82zM12 3L1 9l11 6 9-4.91V17h2V9L12 3z"/></svg>
        <span>Learn</span>
      </button>
      <button class="nav-item" data-tab="settings">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/></svg>
        <span>Settings</span>
      </button>
    </div>

    <div class="nav-group">
      <div class="nav-group-label">Coming Soon</div>
      <button class="nav-item future" disabled>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
        <span>Training Plans</span>
      </button>
      <button class="nav-item future" disabled>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
        <span>AI Coach</span>
      </button>
      <button class="nav-item future" disabled>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 8h1a4 4 0 010 8h-1M2 8h16v9a4 4 0 01-4 4H6a4 4 0 01-4-4V8z"/><path d="M6 1v3m4-3v3m4-3v3"/></svg>
        <span>Nutrition</span>
      </button>
    </div>
  </nav>

  <div class="sidebar-footer">
    <div class="sync-info" id="sync-info"></div>
  </div>
</aside>
<button class="sidebar-toggle" id="sidebar-toggle" title="Toggle sidebar">&lsaquo;</button>

<!-- ═══════════ MAIN ═══════════ -->
<div class="main">

<!-- ──── DASHBOARD ──── -->
<div id="dashboard" class="page active">
  <h1 class="page-title">Dashboard</h1>
  <p class="page-desc">A snapshot of your current training state, load trends, and weekly volume.</p>

  <div class="grid mb-lg">
    <div class="card" id="form-card" style="display:flex;flex-direction:column;"></div>
    <div class="card" id="key-stats-card" style="display:flex;flex-direction:column;"></div>
  </div>

  <div class="card mb-lg" id="insights-card"></div>

  <div class="stack">
    <div class="card">
      <div class="card-title-row">
        <h3>Fitness &amp; Fatigue</h3>
        <div class="range-toggle" id="range-toggle">
          <button class="range-btn" data-range="12w">12 weeks</button>
          <button class="range-btn" data-range="6m">6 months</button>
          <button class="range-btn active" data-range="12m">12 months</button>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="tsbChart"></canvas></div>
      <p class="chart-note">CTL (blue) = 42-day fitness trend &middot; ATL (purple) = 7-day fatigue trend &middot; Dashed lines = projected with complete rest</p>
    </div>

    <div class="card">
      <div class="card-title-row"><h3>Form</h3></div>
      <div class="chart-wrap-sm"><canvas id="formChart"></canvas></div>
      <div class="form-legend">
        <span style="background:var(--yellow-soft);color:var(--text-dim);">Transition (20+)</span>
        <span style="background:var(--blue-soft);color:var(--text-dim);">Fresh (5 to 20)</span>
        <span style="background:rgba(255,255,255,0.04);color:var(--text-dim);">Grey Zone</span>
        <span style="background:var(--green-soft);color:var(--text-dim);">Optimal (-10 to -30)</span>
        <span style="background:var(--red-soft);color:var(--text-dim);">High Risk (&lt;-30)</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title-row"><h3>Weekly Volume</h3></div>
      <div class="chart-wrap-sm"><canvas id="volumeChart"></canvas></div>
    </div>
  </div>

  <div class="quick-log">
    <div class="card-label">Quick Log <span class="coming-soon-tag">Coming Soon</span></div>
    <p>Describe a workout in plain text or upload a .fit file. The AI will parse it into a structured activity automatically.</p>
    <div class="quick-log-row">
      <input class="quick-log-input" type="text" placeholder="e.g. &quot;45 min easy run, avg HR 145, 5.2 miles&quot;" disabled>
      <div class="quick-log-btn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        Upload .fit
      </div>
    </div>
  </div>
</div>

<!-- ──── RUNNING ──── -->
<div id="running" class="page">
  <h1 class="page-title">Running</h1>
  <p class="page-desc">Heart rate zones use Joe Friel's 7-zone model based on your LTHR. Paces and thresholds are calculated from your best recent efforts.</p>

  <div class="stats-row mb-lg" id="run-stats"></div>

  <div class="grid mb-lg">
    <div class="card">
      <div class="card-title-row"><h3>Pace Trend</h3></div>
      <div class="chart-wrap-sm"><canvas id="paceTrendChart"></canvas></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title-row"><h3>Recent Runs</h3><span class="badge" id="run-count-badge"></span></div>
    <div class="tbl-wrap" id="run-table"></div>
  </div>
</div>

<!-- ──── CYCLING ──── -->
<div id="cycling" class="page">
  <h1 class="page-title">Cycling</h1>
  <p class="page-desc">Power zones follow Coggan's model based on FTP. The power curve shows your best wattage at each duration across all rides.</p>

  <div class="stats-row mb-lg" id="bike-stats"></div>

  <div class="grid mb-lg">
    <div class="card">
      <div class="card-title-row"><h3>Power Curve</h3><span class="badge">All-time best</span></div>
      <div class="chart-wrap-sm"><canvas id="powerCurveChart"></canvas></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title-row"><h3>Recent Rides</h3><span class="badge" id="ride-count-badge"></span></div>
    <div class="tbl-wrap" id="ride-table"></div>
  </div>
</div>

<!-- ──── SWIMMING ──── -->
<div id="swimming" class="page">
  <h1 class="page-title">Swimming</h1>
  <p class="page-desc">Pace zones based on your Critical Swim Speed (CSS) — the fastest pace you can hold continuously, roughly your threshold.</p>

  <div class="stats-row mb-lg" id="swim-stats"></div>

  <div class="grid mb-lg">
    <div class="card">
      <div class="card-title-row"><h3>Pace Trend</h3></div>
      <div class="chart-wrap-sm"><canvas id="swimTrendChart"></canvas></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title-row"><h3>Recent Swims</h3><span class="badge" id="swim-count-badge"></span></div>
    <div class="tbl-wrap" id="swim-table"></div>
  </div>
</div>

<!-- ──── ZONES ──── -->
<div id="zones" class="page">
  <h1 class="page-title">Training Zones</h1>
  <p class="page-desc">All zones recalculate instantly when you change thresholds in Settings.</p>
  <div class="grid" id="zone-tables"></div>
</div>

<!-- ──── LEARN ──── -->
<div id="learn" class="page">
  <h1 class="page-title">Learn</h1>
  <p class="page-desc">Understand the science behind your training data. These concepts power everything you see on the dashboard, sport pages, and zone tables.</p>

  <div class="learn-content">

    <div class="card learn-card">
      <div class="card-label">Fitness, Fatigue & Form (TSB Model)</div>
      <p>TSS (Training Stress Score) quantifies how hard a workout was in a single number. A score of 100 represents one hour at your threshold intensity — the hardest effort you could sustain for about an hour. An easy 30-minute jog might score 25; an all-out 90-minute race could score 200+.</p>
      <p>The Performance Management Chart (PMC) is the core of the dashboard. It tracks three metrics derived from your daily TSS — together, they tell the story of your training.</p>

      <div class="learn-sub">
        <h4>CTL — Chronic Training Load (Fitness)</h4>
        <p>A rolling 42-day exponentially weighted average of your daily TSS. Think of it as your <strong>training bank account</strong> — it grows slowly as you train consistently, and decays slowly when you stop.</p>
        <ul>
          <li>Takes weeks to build — there are no shortcuts</li>
          <li>Higher CTL = greater capacity to handle training load</li>
          <li>A CTL of 50 means you've been averaging roughly 50 TSS/day over the past ~6 weeks</li>
          <li>Rate of change matters: building CTL faster than ~5-7 points/week increases injury risk</li>
        </ul>
      </div>

      <div class="learn-sub">
        <h4>ATL — Acute Training Load (Fatigue)</h4>
        <p>A rolling 7-day exponentially weighted average of your daily TSS. This reacts fast — it spikes after hard days and drops after rest days. It represents the <strong>fatigue you're carrying right now</strong>.</p>
        <ul>
          <li>Rises quickly after hard training blocks</li>
          <li>Drops quickly with rest (that's why tapers work)</li>
          <li>ATL much higher than CTL means you're reaching beyond your current fitness — unsustainable long-term</li>
        </ul>
      </div>

      <div class="learn-sub">
        <h4>TSB — Training Stress Balance (Form)</h4>
        <div class="learn-formula">TSB = CTL (yesterday) − ATL (yesterday)</div>
        <p>The balance between your fitness and fatigue. This is the number in the <strong>Form ring</strong> on the dashboard — it tells you how ready you are to perform <em>today</em>.</p>
        <table class="learn-table">
          <thead><tr><th>TSB</th><th>Zone</th><th>What It Means</th></tr></thead>
          <tbody>
            <tr><td style="color:var(--yellow)">&gt;25</td><td>Transition</td><td>Very rested — possible detraining if prolonged. Great for race day after a taper.</td></tr>
            <tr><td style="color:var(--blue)">5 to 25</td><td>Fresh</td><td>Well-recovered and ready to perform. Ideal for racing or hard quality sessions.</td></tr>
            <tr><td style="color:var(--text-dim)">−10 to 5</td><td>Grey Zone</td><td>Balanced — neutral readiness. Normal day-to-day training state.</td></tr>
            <tr><td style="color:var(--green)">−30 to −10</td><td>Optimal</td><td>Productive overreach. Fatigued but adapting — this is where fitness gains happen.</td></tr>
            <tr><td style="color:var(--red)">&lt;−30</td><td>High Risk</td><td>Deep fatigue. Risk of illness, injury, or overtraining. Back off and recover.</td></tr>
          </tbody>
        </table>
        <p>The key insight: <strong>you can't be both fit and fresh at the same time.</strong> Building fitness requires fatigue (negative TSB). Racing well requires freshness (positive TSB). Periodization — alternating build and recovery phases — is how you manage this tradeoff.</p>
      </div>

      <div class="learn-sub">
        <h4>Projected Values</h4>
        <p>The dashed lines on the dashboard project your fitness, fatigue, and form 7 days forward assuming no additional training. This helps you see where your numbers are heading if you rest, and plan accordingly.</p>
      </div>
    </div>

    <div class="card learn-card">
      <div class="card-label">Training Stress Score (TSS) — How It's Calculated</div>
      <p>TSS is calculated differently for each sport, but the principle is the same: <strong>how hard did you go, relative to your threshold, and for how long?</strong></p>

      <div class="learn-sub">
        <h4>Cycling — Power-Based TSS</h4>
        <p>Cycling TSS uses power meter data and your FTP (Functional Threshold Power):</p>
        <div class="learn-formula">TSS = (duration × NP × IF) / (FTP × 3600) × 100</div>
        <ul>
          <li><strong>NP (Normalized Power)</strong> — a weighted average that accounts for the extra cost of intensity spikes. Riding 200W steady and alternating 100W/300W produce the same average, but the variable effort is harder on your body — NP captures that.</li>
          <li><strong>IF (Intensity Factor)</strong> — NP ÷ FTP. An IF of 1.0 means you rode at threshold. Above 1.0 is a race-level effort.</li>
          <li><strong>FTP</strong> — Functional Threshold Power. The highest power you could sustain for roughly one hour. Your current setting: shown in Cycling and Settings.</li>
        </ul>
      </div>

      <div class="learn-sub">
        <h4>Running — Heart Rate TSS (hrTSS)</h4>
        <p>Without a running power meter, TrainingHub estimates stress from heart rate data and your LTHR (Lactate Threshold Heart Rate):</p>
        <div class="learn-formula">hrTSS = (duration × HR_avg × IF) / (LTHR × 3600) × 100</div>
        <ul>
          <li><strong>LTHR</strong> — Lactate Threshold Heart Rate. The heart rate at your sustainable threshold effort (~60 min race pace). Your running and cycling LTHRs are different because of body position and muscle recruitment.</li>
          <li><strong>IF</strong> — average HR ÷ LTHR. An easy Zone 2 run might have an IF of 0.75; a tempo run around 0.90.</li>
        </ul>
        <p>Heart rate-based TSS is less precise than power-based TSS because HR responds slowly to effort changes and is affected by heat, caffeine, fatigue, and drift. But over time, it's reliable enough for trend analysis.</p>
      </div>

      <div class="learn-sub">
        <h4>Swimming — Pace-Based TSS (sTSS)</h4>
        <p>Swimming TSS uses pace relative to your CSS (Critical Swim Speed) — the swimming equivalent of threshold:</p>
        <div class="learn-formula">sTSS = (duration × IF²) / 3600 × 100</div>
        <ul>
          <li><strong>CSS (Critical Swim Speed)</strong> — the pace you could hold for about 30 minutes of continuous swimming. Calculated from your recent best efforts.</li>
          <li><strong>IF</strong> — your pace relative to CSS, where faster = higher IF (unlike running, where speed and pace are inversely related, the IF calculation handles this).</li>
        </ul>
      </div>

      <div class="learn-sub">
        <h4>What TSS Numbers Mean</h4>
        <table class="learn-table">
          <thead><tr><th>TSS</th><th>Effort</th><th>Recovery</th></tr></thead>
          <tbody>
            <tr><td>&lt;150</td><td>Low–moderate</td><td>Recovered by next day</td></tr>
            <tr><td>150–300</td><td>Moderate–hard</td><td>Some residual fatigue next day</td></tr>
            <tr><td>300–450</td><td>Very hard</td><td>Fatigue lingers 2–3 days</td></tr>
            <tr><td>&gt;450</td><td>Epic</td><td>May need 5+ days to fully recover</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card learn-card">
      <div class="card-label">Heart Rate Variability (HRV)</div>
      <p>HRV measures the variation in time between consecutive heartbeats, reported in milliseconds. It's controlled by your autonomic nervous system — the same system that manages stress, recovery, digestion, and sleep.</p>

      <div class="learn-sub">
        <h4>Why It Matters for Training</h4>
        <p>A higher HRV generally indicates a well-recovered, adaptable nervous system. A lower HRV can signal accumulated fatigue, stress, poor sleep, illness onset, or insufficient recovery.</p>
        <ul>
          <li><strong>Within baseline</strong> — your body is handling current training load well. Train as planned.</li>
          <li><strong>Above baseline</strong> — your body is well-recovered. Good day to push harder.</li>
          <li><strong>Below baseline</strong> — something is stressing your system. Consider reducing intensity, prioritizing sleep, or taking a rest day.</li>
        </ul>
      </div>

      <div class="learn-sub">
        <h4>The Baseline Range</h4>
        <p>HRV is highly individual — a "good" number for one person could be low for another. That's why TrainingHub shows your value relative to your <strong>personal baseline range</strong>, calculated from your Garmin data over time. The baseline adapts as your fitness changes.</p>
        <p>Don't overreact to a single day. HRV is noisy — one low reading after a hard workout is normal. Watch for <strong>trends over 3-5 days</strong>. Consistently below baseline is a real signal worth acting on.</p>
      </div>
    </div>

    <div class="card learn-card">
      <div class="card-label">Training Zones</div>
      <p>Zones divide your effort range into distinct intensity bands, each targeting different physiological systems. Training in the right zone ensures the workout achieves its intended purpose.</p>

      <div class="learn-sub">
        <h4>Heart Rate Zones (Friel 7-Zone Model)</h4>
        <p>Used for running and cycling. Based on your LTHR — the heart rate at your lactate threshold. Each zone is a percentage range of LTHR:</p>
        <table class="learn-table">
          <thead><tr><th>Zone</th><th>Name</th><th>% LTHR</th><th>Purpose</th></tr></thead>
          <tbody>
            <tr><td>1</td><td>Recovery</td><td>&lt;85%</td><td>Active recovery, warmup/cooldown</td></tr>
            <tr><td>2</td><td>Aerobic</td><td>85–89%</td><td>Base endurance, fat burning, aerobic development</td></tr>
            <tr><td>3</td><td>Tempo</td><td>90–94%</td><td>Moderate endurance, "comfortably hard"</td></tr>
            <tr><td>4</td><td>Sub-Threshold</td><td>95–99%</td><td>Sustained hard effort, lactate clearance</td></tr>
            <tr><td>5a</td><td>Super-Threshold</td><td>100–102%</td><td>Threshold work, time trial intensity</td></tr>
            <tr><td>5b</td><td>Aerobic Capacity</td><td>103–106%</td><td>VO2max intervals, high-intensity development</td></tr>
            <tr><td>5c</td><td>Anaerobic</td><td>&gt;106%</td><td>Short, maximal efforts, sprint power</td></tr>
          </tbody>
        </table>
        <p>Your running LTHR and cycling LTHR are different because of body position and muscle recruitment. Always use sport-specific thresholds.</p>
      </div>

      <div class="learn-sub">
        <h4>Power Zones (Coggan Model)</h4>
        <p>Used for cycling with a power meter. Based on your FTP — Functional Threshold Power:</p>
        <table class="learn-table">
          <thead><tr><th>Zone</th><th>Name</th><th>% FTP</th><th>Purpose</th></tr></thead>
          <tbody>
            <tr><td>1</td><td>Active Recovery</td><td>&lt;55%</td><td>Easy spinning, recovery</td></tr>
            <tr><td>2</td><td>Endurance</td><td>56–75%</td><td>Long rides, aerobic base</td></tr>
            <tr><td>3</td><td>Tempo</td><td>76–90%</td><td>Sustained effort, "sweet spot" training</td></tr>
            <tr><td>4</td><td>Threshold</td><td>91–105%</td><td>FTP intervals, race pace</td></tr>
            <tr><td>5</td><td>VO2max</td><td>106–120%</td><td>High-intensity intervals (3-8 min)</td></tr>
            <tr><td>6</td><td>Anaerobic</td><td>121–150%</td><td>Short power (30s-2min)</td></tr>
            <tr><td>7</td><td>Neuromuscular</td><td>&gt;150%</td><td>Sprints, max power (&lt;30s)</td></tr>
          </tbody>
        </table>
      </div>

      <div class="learn-sub">
        <h4>Swimming Pace Zones (CSS-Based)</h4>
        <p>Based on your Critical Swim Speed — the fastest pace you can hold for roughly 30 minutes. CSS zones help you train the right energy systems in the pool, from easy recovery laps to race-pace intervals.</p>
      </div>
    </div>

    <div class="card learn-card">
      <div class="card-label">Power Curve</div>
      <p>The power curve (on the Cycling page) plots your <strong>best power output at every duration</strong> across all rides in the selected time range. Each point answers: "What's the hardest I've ever gone for X seconds/minutes/hours?"</p>
      <ul>
        <li><strong>Left side (5s–30s)</strong> — neuromuscular power and sprint ability</li>
        <li><strong>Middle (1min–8min)</strong> — anaerobic capacity and VO2max power</li>
        <li><strong>Right side (20min–60min+)</strong> — threshold and endurance power (where FTP lives)</li>
      </ul>
      <p>The shape of your curve reveals your strengths. A steep left side with a big drop-off means you're a sprinter. A flat curve that holds up over time means you're a diesel — strong at sustained efforts. As your training progresses, watch the curve shift upward across all durations.</p>
    </div>

    <div class="card learn-card">
      <div class="card-label">Key Thresholds</div>

      <div class="learn-sub">
        <h4>FTP — Functional Threshold Power</h4>
        <p>The highest power (in watts) you can sustain for approximately one hour. It's the anchor for all cycling power zones and cycling TSS. TrainingHub estimates your FTP from your best 20-minute power efforts (multiplied by 0.95). You can override it in Settings if you've done a formal FTP test.</p>
      </div>

      <div class="learn-sub">
        <h4>LTHR — Lactate Threshold Heart Rate</h4>
        <p>The heart rate at your lactate threshold — the point where lactate accumulates faster than your body can clear it. Above this intensity, you "go anaerobic" and fatigue accumulates rapidly. TrainingHub calculates separate running and cycling LTHRs from your workout data.</p>
      </div>

      <div class="learn-sub">
        <h4>CSS — Critical Swim Speed</h4>
        <p>The swimming equivalent of threshold. Estimated from your best efforts at different distances. Swim at CSS pace and you can sustain it for an extended period; go above it and you'll fade quickly. All swim pace zones are based on this number.</p>
      </div>

      <div class="learn-sub">
        <h4>W/kg — Watts Per Kilogram</h4>
        <p>FTP divided by body weight. This normalizes cycling power across riders of different sizes. A 60kg rider at 240W and an 80kg rider at 320W both produce 4.0 W/kg — and they'll climb at similar speeds. W/kg is the best single metric for comparing cycling fitness.</p>
      </div>
    </div>

  </div>
</div>

<!-- ──── SETTINGS ──── -->
<div id="settings" class="page">
  <h1 class="page-title">Settings</h1>
  <p class="page-desc">Auto-calculated from your workout data. Override any value and zones + charts update instantly. Saved in your browser.</p>

  <div class="card" style="max-width:780px">
    <div class="card-label">Training Parameters</div>
    <div id="settings-form"></div>
    <div style="display:flex;gap:10px;margin-top:20px;padding-top:16px;border-top:1px solid var(--border);">
      <button id="save-overrides-btn" class="btn btn-primary">Export Overrides</button>
      <button id="reset-all-btn" class="btn">Reset All to Auto</button>
    </div>
    <p class="chart-note" style="margin-top:10px">Export downloads a JSON file to place in garmin_data/ so overrides persist across data refreshes.</p>
  </div>

  <div class="card" style="max-width:780px;margin-top:20px">
    <div class="card-label">Life Events</div>
    <p class="chart-note" style="margin-bottom:16px">Add events that show as markers on the Fitness &amp; Fatigue chart — races, injuries, illness, surgery, travel, etc.</p>
    <div class="evt-form">
      <div><label>Date</label><input type="date" id="evt-date"></div>
      <div><label>Type</label>
        <select id="evt-type">
          <option value="race">Race</option><option value="injury">Injury</option><option value="illness">Illness</option>
          <option value="surgery">Surgery</option><option value="travel">Travel</option><option value="other">Other</option>
        </select></div>
      <div><label>Label</label><input type="text" id="evt-label" placeholder="e.g. Marathon, Knee surgery..."></div>
      <div><label>Notes (optional)</label><input type="text" id="evt-notes" placeholder="Details..."></div>
      <button id="evt-add-btn" class="btn btn-primary" style="padding:8px 16px;white-space:nowrap;">Add Event</button>
    </div>
    <div id="evt-table"></div>
  </div>
</div>

</div><!-- /.main -->

<script>
// ═══════════════════════════════════════
//  DATA
// ═══════════════════════════════════════
const D = __DATA_JSON__;

// ═══════════════════════════════════════
//  SETTINGS / OVERRIDES
// ═══════════════════════════════════════
const SETTINGS_KEYS = [
  { key: 'ftp', label: 'FTP', unit: 'W', desc: 'Functional Threshold Power' },
  { key: 'lthr_run', label: 'LTHR Run', unit: 'bpm', desc: 'Lactate threshold heart rate (running)' },
  { key: 'lthr_bike', label: 'LTHR Bike', unit: 'bpm', desc: 'Lactate threshold heart rate (cycling)' },
  { key: 'threshold_pace_mi', label: 'Threshold Pace', unit: '/mi', format: fmtPace, desc: 'Running threshold pace per mile' },
  { key: 'css_100yd', label: 'CSS', unit: '/100yd', format: fmtPace, desc: 'Critical Swim Speed per 100 yards' },
  { key: 'max_hr_run', label: 'Max HR Run', unit: 'bpm', desc: 'Maximum heart rate observed (running)' },
  { key: 'max_hr_bike', label: 'Max HR Bike', unit: 'bpm', desc: 'Maximum heart rate observed (cycling)' },
  { key: 'weight_lb', label: 'Weight', unit: 'lb', desc: 'Body weight (for W/kg calculation)' },
];

function loadSettings() {
  const stored = JSON.parse(localStorage.getItem('trainingOverrides') || '{}');
  const eff = { ...D.auto };
  Object.entries(D.overrides || {}).forEach(([k, v]) => { if (v != null) eff[k] = v; });
  Object.entries(stored).forEach(([k, v]) => { if (v != null) eff[k] = v; });
  return eff;
}
function saveSettings(o) { localStorage.setItem('trainingOverrides', JSON.stringify(o)); }
function getOverrides() { return JSON.parse(localStorage.getItem('trainingOverrides') || '{}'); }

// ═══════════════════════════════════════
//  LIFE EVENTS
// ═══════════════════════════════════════
const EVENT_TYPES = {
  race:    { label:'Race',    color:'--green',  icon:'\u{1F3C1}' },
  injury:  { label:'Injury',  color:'--red',    icon:'\u{1F915}' },
  illness: { label:'Illness', color:'--yellow', icon:'\u{1F912}' },
  surgery: { label:'Surgery', color:'--red',    icon:'\u{1F3E5}' },
  travel:  { label:'Travel',  color:'--purple', icon:'\u{2708}'  },
  other:   { label:'Other',   color:'--blue',   icon:'\u{1F4CC}' },
};
function getLifeEvents() { return JSON.parse(localStorage.getItem('lifeEvents') || '[]'); }
function saveLifeEvents(events) { localStorage.setItem('lifeEvents', JSON.stringify(events)); }

// ═══════════════════════════════════════
//  FORMATTING HELPERS
// ═══════════════════════════════════════
function fmtPace(s) {
  if (!s || s <= 0) return '\u2013';
  return `${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
}
function fmtDur(mins) {
  if (!mins) return '\u2013';
  const h = Math.floor(mins/60), m = Math.round(mins%60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}
function fmtDate(d) {
  if (!d) return '\u2013';
  const dt = new Date(d+'T00:00:00');
  return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}
function fmtDateShort(d) {
  if (!d) return '\u2013';
  const dt = new Date(d+'T00:00:00');
  return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}
function weekToDate(wk) {
  const [yr, wNum] = wk.split('-W').map(Number);
  const jan4 = new Date(yr, 0, 4);
  const dow = jan4.getDay() || 7;
  const mon = new Date(jan4);
  mon.setDate(jan4.getDate() - dow + 1 + (wNum-1)*7);
  return mon.toISOString().slice(0,10);
}

function humanizeStatus(raw) {
  if (!raw) return { text: '\u2013', cls: 'default' };
  const s = raw.replace(/_\d+$/, '').replace(/_/g,' ').toLowerCase();
  const cap = s.charAt(0).toUpperCase() + s.slice(1);
  const m = { productive:'productive', maintaining:'maintaining', detraining:'detraining',
    overreaching:'overreaching', recovery:'recovery', unproductive:'unproductive', peaking:'productive' };
  return { text: cap, cls: m[s] || 'default' };
}

function describeForm(tsb) {
  if (tsb > 20) return { zone: 'Transition', color: 'var(--yellow)',
    text: "You\u2019re well-rested and have shed most fatigue. Great time for a race or time trial.",
    action: "If you\u2019re not tapering for an event, start building load back up to avoid detraining." };
  if (tsb > 5) return { zone: 'Fresh', color: 'var(--blue)',
    text: "Good balance of fitness and freshness. You\u2019re primed to perform well.",
    action: "Maintain current load, or push a hard session while you have the legs for it." };
  if (tsb >= -10) return { zone: 'Grey Zone', color: 'var(--text-dim)',
    text: "Neutral \u2014 not particularly fresh or fatigued. You\u2019re absorbing recent training.",
    action: "A harder session will push you into the optimal build zone. An easy day moves you toward fresh." };
  if (tsb >= -30) return { zone: 'Optimal', color: 'var(--green)',
    text: "Carrying productive training fatigue. This is the sweet spot where fitness adapts and grows.",
    action: "Keep training consistently. Monitor how you feel \u2014 if motivation or sleep drops, take a recovery day." };
  return { zone: 'High Risk', color: 'var(--red)',
    text: "Accumulated fatigue is high. Performance and immune function may be compromised.",
    action: "Prioritize recovery: easy sessions, extra sleep, nutrition. A rest day or two will bring you back to the optimal zone." };
}

// ═══════════════════════════════════════
//  ZONES
// ═══════════════════════════════════════
function getZColors() { return [css('--blue'),css('--green'),css('--yellow'),css('--orange'),css('--red'),css('--purple'),'#ff6b9d']; }

function hrZones(lthr) {
  return [
    { name:'Z1 Recovery', lo:0, hi:Math.round(lthr*0.81) },
    { name:'Z2 Aerobic', lo:Math.round(lthr*0.81), hi:Math.round(lthr*0.89) },
    { name:'Z3 Tempo', lo:Math.round(lthr*0.90), hi:Math.round(lthr*0.93) },
    { name:'Z4 Sub-Threshold', lo:Math.round(lthr*0.94), hi:Math.round(lthr*0.99) },
    { name:'Z5a Threshold', lo:lthr, hi:Math.round(lthr*1.02) },
    { name:'Z5b VO2max', lo:Math.round(lthr*1.03), hi:Math.round(lthr*1.06) },
    { name:'Z5c Anaerobic', lo:Math.round(lthr*1.06), hi:null },
  ];
}
function pwrZones(ftp) {
  return [
    { name:'Z1 Active Recovery', lo:0, hi:Math.round(ftp*0.55) },
    { name:'Z2 Endurance', lo:Math.round(ftp*0.56), hi:Math.round(ftp*0.75) },
    { name:'Z3 Tempo', lo:Math.round(ftp*0.76), hi:Math.round(ftp*0.90) },
    { name:'Z4 Threshold', lo:Math.round(ftp*0.91), hi:Math.round(ftp*1.05) },
    { name:'Z5 VO2max', lo:Math.round(ftp*1.06), hi:Math.round(ftp*1.20) },
    { name:'Z6 Anaerobic', lo:Math.round(ftp*1.21), hi:Math.round(ftp*1.50) },
    { name:'Z7 Neuromuscular', lo:Math.round(ftp*1.50), hi:null },
  ];
}
function paceZones(tp) {
  return [
    { name:'Z1 Recovery', lo:fmtPace(tp*1.29), hi:fmtPace(tp*1.50) },
    { name:'Z2 Aerobic', lo:fmtPace(tp*1.14), hi:fmtPace(tp*1.29) },
    { name:'Z3 Tempo', lo:fmtPace(tp*1.06), hi:fmtPace(tp*1.13) },
    { name:'Z4 Sub-Threshold', lo:fmtPace(tp*1.01), hi:fmtPace(tp*1.05) },
    { name:'Z5a Threshold', lo:fmtPace(tp*0.97), hi:fmtPace(tp*1.00) },
    { name:'Z5b VO2max', lo:fmtPace(tp*0.90), hi:fmtPace(tp*0.96) },
    { name:'Z5c Anaerobic', lo:fmtPace(tp*0.80), hi:fmtPace(tp*0.90) },
  ];
}
function cssZones(css) {
  return [
    { name:'Z1 Recovery', lo:fmtPace(css*1.20), hi:fmtPace(css*1.40) },
    { name:'Z2 Endurance', lo:fmtPace(css*1.08), hi:fmtPace(css*1.19) },
    { name:'Z3 Tempo/CSS', lo:fmtPace(css*0.98), hi:fmtPace(css*1.07) },
    { name:'Z4 Threshold', lo:fmtPace(css*0.92), hi:fmtPace(css*0.97) },
    { name:'Z5 VO2max', lo:fmtPace(css*0.85), hi:fmtPace(css*0.91) },
    { name:'Z6 Sprint', lo:fmtPace(css*0.70), hi:fmtPace(css*0.84) },
  ];
}

function zoneTableHTML(title, zones, unit) {
  const zc = getZColors();
  let h = `<div class="card zone-card"><div class="card-label">${title}</div><table><tr><th>Zone</th><th>Range</th></tr>`;
  zones.forEach((z,i) => {
    const hi = z.hi != null ? z.hi : 'max';
    h += `<tr><td><span class="zone-dot" style="background:${zc[i%7]}"></span>${z.name}</td><td>${z.lo} \u2013 ${hi} ${unit}</td></tr>`;
  });
  return h + '</table></div>';
}

// ═══════════════════════════════════════
//  CHART CONFIG & DESIGN SYSTEM
// ═══════════════════════════════════════
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.plugins.legend.labels.usePointStyle = true;
Chart.defaults.plugins.legend.labels.pointStyleWidth = 19;
Chart.defaults.plugins.legend.labels.boxHeight = 2;
Chart.defaults.plugins.legend.labels.pointStyle = 'line';
Chart.defaults.elements.point.pointStyle = 'circle';

// Resolve CSS vars for chart colors (Chart.js can't read CSS vars directly)
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
function GC() { return css('--chart-grid'); }
function TC() { return css('--chart-tick'); }
function LC() { return css('--text-dim'); }

// ── Color helpers (resolve CSS vars) ──
function sportColor(name) { return css('--color-' + name); }
function hexToRgba(hex, opacity) {
  const c = hex.trim();
  const r = parseInt(c.slice(1,3),16), g = parseInt(c.slice(3,5),16), b = parseInt(c.slice(5,7),16);
  return `rgba(${r},${g},${b},${opacity || 0.06})`;
}

// ── Chart dataset helpers ──
// Continuous line (hidden points, hover dots) — for dashboard time-series
function lineStyle(colorVar, opts) {
  const c = css(colorVar);
  return {
    borderColor: c, borderWidth: 2.5, fill: false, pointRadius: 0,
    pointHoverRadius: 4, pointHoverBackgroundColor: c,
    pointHoverBorderColor: css('--bg-card'), pointHoverBorderWidth: 2,
    tension: 0.3, ...opts
  };
}
// Projected (dashed) variant
function projStyle(colorVar, opts) {
  return lineStyle(colorVar, { borderWidth: 2, borderDash: [6,4], ...opts });
}
// Scatter line with visible dots — for sport-specific trend charts
function dotStyle(colorVar, opts) {
  const c = css(colorVar);
  return {
    borderColor: c, borderWidth: 2, fill: true, backgroundColor: hexToRgba(c, 0.06),
    pointRadius: 4, pointStyle: 'circle', pointBackgroundColor: c,
    pointBorderColor: css('--bg-card'), pointBorderWidth: 2, tension: 0.2, ...opts
  };
}
// Standard legend config (with dashed line support for projected datasets)
function legendLabels(opts) {
  const base = {
    color: LC(), padding: 16, usePointStyle: true, pointStyleWidth: 19, boxHeight: 2,
    generateLabels(chart) {
      return chart.data.datasets.map((ds, i) => ({
        text: ds.label, datasetIndex: i, pointStyle: 'line',
        fontColor: LC(),
        strokeStyle: ds.borderColor || ds.backgroundColor, fillStyle: 'transparent',
        lineDash: ds.borderDash ? [4,3] : [], lineWidth: 2,
        hidden: !chart.isDatasetVisible(i)
      }));
    }
  };
  if (opts && opts.generateLabels) {
    const customGen = opts.generateLabels;
    base.generateLabels = function(chart) {
      return customGen(chart).map(item => ({ fontColor: LC(), ...item }));
    };
    delete opts.generateLabels;
  }
  return { ...base, ...opts };
}
// Standard chart options base
function chartOpts(opts) {
  const titleCb = { title(items) {
    if (!items.length) return '';
    // Use the max parsed.x across all tooltip items — projected datasets
    // have the correct future timestamps, actual datasets may map to old indices
    const ts = Math.max(...items.map(i => i.parsed?.x || 0));
    if (ts > 1e11) return new Date(ts).toLocaleDateString('en-US', {month:'short',day:'numeric',year:'numeric'});
    return items[0].label || '';
  } };
  const base = { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false } };
  const merged = { ...base, ...opts };
  // Ensure tooltip title callback is always present
  if (!merged.plugins) merged.plugins = {};
  if (!merged.plugins.tooltip) merged.plugins.tooltip = {};
  if (!merged.plugins.tooltip.callbacks) merged.plugins.tooltip.callbacks = {};
  merged.plugins.tooltip.callbacks = { ...titleCb, ...merged.plugins.tooltip.callbacks };
  return merged;
}

// ═══════════════════════════════════════
//  THEME
// ═══════════════════════════════════════
function initTheme() {
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeIcon(saved);
}
function updateThemeIcon(theme) {
  document.getElementById('theme-icon-moon').style.display = theme === 'dark' ? 'block' : 'none';
  document.getElementById('theme-icon-sun').style.display = theme === 'light' ? 'block' : 'none';
}
document.getElementById('theme-toggle').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  updateThemeIcon(next);
  renderAll(); // re-render charts with new colors
});
initTheme();

// ═══════════════════════════════════════
//  SIDEBAR + NAV
// ═══════════════════════════════════════
const sidebar = document.getElementById('sidebar');
const sidebarToggle = document.getElementById('sidebar-toggle');
sidebarToggle.addEventListener('click', () => {
  sidebar.classList.toggle('collapsed');
  sidebarToggle.textContent = sidebar.classList.contains('collapsed') ? '\u203a' : '\u2039';
});

document.querySelectorAll('.nav-item:not(.future)').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});

// ═══════════════════════════════════════
//  RENDER
// ═══════════════════════════════════════
let charts = {};

function renderAll() {
  const S = loadSettings();
  Chart.defaults.color = LC();
  document.getElementById('sync-info').textContent = `Synced ${fmtDate(D.generated)}`;
  renderDashboard(S);
  renderRunning(S);
  renderCycling(S);
  renderSwimming(S);
  renderZones(S);
  renderSettings(S);
}

// ─── Dashboard ───
function renderDashboard(S) {
  const today = D.tsb.filter(d => !d.projected).slice(-1)[0] || {};
  const projAll = D.tsb.filter(d => d.projected);
  const proj = projAll.length >= 7 ? projAll[6] : projAll.slice(-1)[0] || {};
  const tsb = today.tsb || 0;
  const form = describeForm(tsb);

  // Form ring: map TSB from [-50, 50] to [0, 100] for the ring
  const ringPct = Math.max(0, Math.min(100, ((tsb + 50) / 100) * 100));
  const circumference = 2 * Math.PI * 42;
  const dashoffset = circumference - (ringPct / 100) * circumference;

  document.getElementById('form-card').innerHTML = `
    <div class="card-label">Your Form Today</div>
    <div class="form-hero" style="flex:1;display:flex;align-items:center;justify-content:center;">
      <div class="form-ring">
        <svg viewBox="0 0 100 100">
          <circle class="bg" cx="50" cy="50" r="42"/>
          <circle class="fg" cx="50" cy="50" r="42"
            stroke="${form.color}" stroke-dasharray="${circumference}" stroke-dashoffset="${dashoffset}"/>
        </svg>
        <div class="center">
          <div class="number" style="color:${form.color}">${tsb}</div>
          <div class="unit">TSB</div>
        </div>
      </div>
      <div class="form-summary">
        <h3 style="color:${form.color}">${form.zone}</h3>
        <p>${form.text}</p>
        <p style="margin-top:6px;color:var(--text);font-weight:500;font-size:12px">${form.action}</p>
      </div>
    </div>
    <div style="margin-top:auto;padding-top:14px;border-top:1px solid var(--border);display:flex;gap:4px;align-items:center;font-size:11px;justify-content:space-between;">
        <span style="flex:1;text-align:center;padding:3px 4px;border-radius:4px;background:var(--red-soft);color:var(--red);${tsb<-30?'outline:1.5px solid var(--red)':''}">High Risk &lt;\u221230</span>
        <span style="flex:1;text-align:center;padding:3px 4px;border-radius:4px;background:var(--green-soft);color:var(--green);${tsb>=-30&&tsb<-10?'outline:1.5px solid var(--green)':''}">Optimal \u221230 to \u221210</span>
        <span style="flex:1;text-align:center;padding:3px 4px;border-radius:4px;background:rgba(255,255,255,0.04);color:var(--text-faint);${tsb>=-10&&tsb<=5?'outline:1.5px solid var(--text-faint)':''}">Grey \u221210 to 5</span>
        <span style="flex:1;text-align:center;padding:3px 4px;border-radius:4px;background:var(--blue-soft);color:var(--blue);${tsb>5&&tsb<=20?'outline:1.5px solid var(--blue)':''}">Fresh 5 to 20</span>
        <span style="flex:1;text-align:center;padding:3px 4px;border-radius:4px;background:var(--yellow-soft);color:var(--yellow);${tsb>20?'outline:1.5px solid var(--yellow)':''}">Transition 20+</span>
    </div>
  `;

  // Key stats card (top-right)
  const projForm = describeForm(proj.tsb || 0);
  const hrv = D.hrv || {};
  const bl = hrv.baseline || {};
  const hrvVal = hrv.lastNightAvg;
  const hrvWeek = hrv.weeklyAvg;
  const blLow = bl.balancedLow || 0;
  const blHigh = bl.balancedUpper || 0;
  // HRV status: compare last night to baseline range
  let hrvColor = 'var(--text)';
  let hrvLabel = '';
  let hrvTip = '';
  if (hrvVal && blLow && blHigh) {
    if (hrvVal >= blLow && hrvVal <= blHigh) {
      hrvColor = 'var(--green)'; hrvLabel = 'Within baseline';
    } else {
      hrvColor = 'var(--red)'; hrvLabel = hrvVal > blHigh ? 'Above baseline' : 'Below baseline';
    }
  }
  // HRV bar position: map value within range [blLow-15, blHigh+15]
  const hrvMin = Math.max(0, blLow - 15);
  const hrvMax = blHigh + 15;
  const hrvPct = hrvVal ? Math.max(0, Math.min(100, ((hrvVal - hrvMin) / (hrvMax - hrvMin)) * 100)) : 0;
  const blLowPct = ((blLow - hrvMin) / (hrvMax - hrvMin)) * 100;
  const blHighPct = ((blHigh - hrvMin) / (hrvMax - hrvMin)) * 100;

  document.getElementById('key-stats-card').innerHTML = `
    <div class="card-label">Key Metrics</div>
    <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px;flex:1;align-content:center;">
      <div style="padding:14px 16px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid var(--border);display:flex;flex-direction:column;justify-content:center;">
        <div style="font-size:22px;font-weight:700;font-family:var(--mono);color:var(--blue)">${today.ctl||'\u2013'}</div>
        <div style="font-size:11px;color:var(--text-faint);margin-top:4px">Fitness (CTL)</div>
      </div>
      <div style="padding:14px 16px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid var(--border);display:flex;flex-direction:column;justify-content:center;">
        <div style="font-size:22px;font-weight:700;font-family:var(--mono);color:var(--purple)">${today.atl||'\u2013'}</div>
        <div style="font-size:11px;color:var(--text-faint);margin-top:4px">Fatigue (ATL)</div>
      </div>
      <div style="padding:14px 16px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid var(--border);">
        <div style="display:flex;align-items:baseline;gap:6px;">
          <span style="font-size:22px;font-weight:700;font-family:var(--mono);color:${hrvColor}">${hrvVal||'\u2013'}</span>
          <span style="font-size:11px;color:var(--text-faint)">ms</span>
          <span style="font-size:11px;color:var(--text-faint);margin-left:auto">${hrvWeek?'7d: '+hrvWeek:''}</span>
        </div>
        ${blLow ? `<div style="position:relative;height:6px;border-radius:3px;background:var(--border);overflow:visible;margin:8px 0 2px;">
          <div style="position:absolute;left:${blLowPct}%;width:${blHighPct-blLowPct}%;height:100%;border-radius:3px;background:var(--green-soft);"></div>
          <div style="position:absolute;left:${hrvPct}%;top:-2px;width:4px;height:10px;border-radius:2px;background:${hrvColor};transform:translateX(-2px);"></div>
        </div>` : ''}
        <div style="font-size:11px;color:var(--text-faint);margin-top:4px">HRV <span style="color:${hrvColor}">${hrvLabel||''}</span></div>
      </div>
      <div style="padding:14px 16px;background:var(--bg);border-radius:var(--radius-sm);border:1px solid var(--border);display:flex;flex-direction:column;justify-content:center;">
        <div style="display:flex;align-items:baseline;gap:8px;">
          <span style="font-size:22px;font-weight:700;font-family:var(--mono);color:${projForm.color}">${proj.tsb||'\u2013'}</span>
          <span style="font-size:12px;font-weight:600;color:${projForm.color}">${projForm.zone}</span>
        </div>
        <div style="font-size:11px;color:var(--text-faint);margin-top:4px">Projected Form (7d)</div>
      </div>
    </div>
  `;

  // ── Time range (shared by all 3 dashboard charts) ──
  // lookback = historical range, lookahead = future projection space
  const rangeSpec = {
    '12w': { back: 7*12, ahead: 7 },
    '6m':  { back: 183,  ahead: 14 },
    '12m': { back: 365,  ahead: 14 },
  };
  let dashRange = localStorage.getItem('dashRange') || '12m';

  function getXBounds() {
    const spec = rangeSpec[dashRange];
    const mn = new Date(); mn.setDate(mn.getDate() - spec.back);
    const mx = new Date(); mx.setDate(mx.getDate() + spec.ahead);
    return { xMin: mn.toISOString().slice(0,10), xMax: mx.toISOString().slice(0,10) };
  }

  function getVolWeeks() {
    return Object.keys(D.weekly_volume);
  }

  // Wire up range toggle buttons
  document.querySelectorAll('.range-btn').forEach(btn => {
    if (btn.dataset.range === dashRange) btn.classList.add('active');
    else btn.classList.remove('active');
    btn.addEventListener('click', () => {
      dashRange = btn.dataset.range;
      localStorage.setItem('dashRange', dashRange);
      document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderDashboardCharts();
    });
  });

  renderDashboardCharts();
  renderInsights(S);

  function renderDashboardCharts() {
  // Feed ALL data — x-axis min/max controls the visible window
  const allActual = D.tsb.filter(d => !d.projected);
  const projected = D.tsb.filter(d => d.projected);
  if (projected.length && allActual.length) projected.unshift(allActual[allActual.length-1]);
  const xUnit = dashRange === '12w' ? 'week' : 'month';
  // Identical x-axis for all 3 charts
  const {xMin, xMax} = getXBounds();
  const sharedX = {type:'time',offset:false,min:xMin,max:xMax,time:{unit:xUnit,displayFormats:{week:"MMM d",month:"MMM ''yy"}},ticks:{color:TC(),maxTicksLimit:12},grid:{color:GC(),offset:false}};

  // Life events plugin — draws vertical markers on the TSB chart
  const lifeEventsPlugin = {
    id: 'lifeEvents',
    afterDraw(chart) {
      const events = getLifeEvents();
      if (!events.length) return;
      const {ctx, chartArea: {top, bottom, left, right}, scales: {x}} = chart;
      if (!x) return;
      ctx.save();
      events.forEach(ev => {
        const px = x.getPixelForValue(new Date(ev.date));
        if (px < left || px > right) return;
        const etype = EVENT_TYPES[ev.type] || EVENT_TYPES.other;
        const color = css(etype.color);
        // Vertical dashed line
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.globalAlpha = 0.6;
        ctx.beginPath();
        ctx.moveTo(px, top);
        ctx.lineTo(px, bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      });
      ctx.restore();
      // Store positions for tooltip hit detection
      chart._lifeEventPositions = events.map(ev => ({
        ...ev,
        px: x.getPixelForValue(new Date(ev.date)),
        etype: EVENT_TYPES[ev.type] || EVENT_TYPES.other
      })).filter(e => e.px >= left && e.px <= right);
    }
  };

  // Custom tooltip for life event markers
  const tsbCanvas = document.getElementById('tsbChart');
  // Remove old listener if re-rendering
  if (tsbCanvas._evtTip) tsbCanvas.removeEventListener('mousemove', tsbCanvas._evtTip);
  let evtTipEl = document.getElementById('evt-tooltip');
  if (!evtTipEl) {
    evtTipEl = document.createElement('div');
    evtTipEl.id = 'evt-tooltip';
    evtTipEl.style.cssText = 'position:fixed;padding:8px 12px;border-radius:8px;background:var(--bg-card);border:1px solid var(--border);font-size:12px;pointer-events:none;z-index:500;display:none;max-width:220px;box-shadow:0 4px 12px rgba(0,0,0,0.2);';
    document.body.appendChild(evtTipEl);
  }
  tsbCanvas._evtTip = function(e) {
    const chart = charts.tsb;
    if (!chart || !chart._lifeEventPositions) return;
    const rect = tsbCanvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const hit = chart._lifeEventPositions.find(ev =>
      Math.abs(ev.px - mx) < 12 && my >= chart.chartArea.top && my <= chart.chartArea.bottom
    );
    if (hit) {
      const color = css(hit.etype.color);
      evtTipEl.innerHTML = `<div style="font-weight:600;color:${color}">${hit.etype.label}: ${hit.label}</div>
        <div style="color:var(--text-dim);margin-top:2px">${fmtDate(hit.date)}</div>
        ${hit.notes ? `<div style="color:var(--text-dim);margin-top:4px;font-size:11px">${hit.notes}</div>` : ''}`;
      evtTipEl.style.display = 'block';
      evtTipEl.style.left = (e.clientX + 12) + 'px';
      evtTipEl.style.top = (e.clientY - 10) + 'px';
    } else {
      evtTipEl.style.display = 'none';
    }
  };
  tsbCanvas.addEventListener('mousemove', tsbCanvas._evtTip);
  tsbCanvas.addEventListener('mouseleave', () => { evtTipEl.style.display = 'none'; });

  // TSB chart
  if (charts.tsb) charts.tsb.destroy();
  charts.tsb = new Chart(document.getElementById('tsbChart'), {
    type:'line', plugins:[lifeEventsPlugin],
    data:{ labels: allActual.map(d=>d.date), datasets:[
      { label:'Fitness (CTL)', data:allActual.map(d=>d.ctl), ...lineStyle('--blue') },
      { label:'Fatigue (ATL)', data:allActual.map(d=>d.atl), ...lineStyle('--purple') },
      { label:'Fitness (projected)', data:projected.map(d=>({x:d.date,y:d.ctl})), ...projStyle('--blue') },
      { label:'Fatigue (projected)', data:projected.map(d=>({x:d.date,y:d.atl})), ...projStyle('--purple') },
      { label:'Daily TSS', data:allActual.map(d=>d.tss), type:'bar', backgroundColor:css('--blue-soft'), borderWidth:0, yAxisID:'y1', barPercentage:1, categoryPercentage:1 },
    ]},
    options: chartOpts({
      scales:{
        x:sharedX,
        y:{ticks:{color:TC()},grid:{color:GC()},title:{display:true,text:'Training Load',color:TC()}},
        y1:{position:'right',ticks:{color:TC()},grid:{display:false},title:{display:true,text:'Daily TSS',color:TC()}},
      },
      plugins:{legend:{labels:legendLabels()},tooltip:{position:'nearest'}}
    })
  });

  // Form chart — gradient line coloring
  const formPlugin = {
    id:'formBg',
    beforeDraw(chart){
      const {ctx,chartArea:{top,bottom,left,right},scales:{y}} = chart;
      if(!y) return;
      const isDark = (document.documentElement.getAttribute('data-theme') || 'dark') === 'dark';
      const zones = isDark ? [
        {min:20,max:100,color:'rgba(224,192,103,0.25)'},
        {min:5,max:20,color:'rgba(91,160,247,0.22)'},
        {min:-10,max:5,color:'rgba(255,255,255,0.05)'},
        {min:-30,max:-10,color:'rgba(80,200,120,0.22)'},
        {min:-100,max:-30,color:'rgba(239,100,97,0.22)'},
      ] : [
        {min:20,max:100,color:'rgba(184,150,12,0.18)'},
        {min:5,max:20,color:'rgba(43,125,233,0.15)'},
        {min:-10,max:5,color:'rgba(0,0,0,0.04)'},
        {min:-30,max:-10,color:'rgba(26,157,72,0.15)'},
        {min:-100,max:-30,color:'rgba(220,61,61,0.15)'},
      ];
      ctx.save();
      zones.forEach(z=>{
        const yT=y.getPixelForValue(Math.min(z.max,y.max));
        const yB=y.getPixelForValue(Math.max(z.min,y.min));
        if(yB>top&&yT<bottom){ ctx.fillStyle=z.color; ctx.fillRect(left,Math.max(yT,top),right-left,Math.min(yB,bottom)-Math.max(yT,top)); }
      });
      ctx.strokeStyle=isDark?'rgba(255,255,255,0.08)':'rgba(0,0,0,0.08)';ctx.lineWidth=1;ctx.setLineDash([4,4]);
      [20,5,-10,-30].forEach(v=>{ const px=y.getPixelForValue(v); if(px>=top&&px<=bottom){ctx.beginPath();ctx.moveTo(left,px);ctx.lineTo(right,px);ctx.stroke();} });
      ctx.restore();
    },
    // Build vertical gradient after layout so y-scale pixel positions are known
    afterLayout(chart) {
      const {scales:{y}} = chart;
      if (!y) return;
      const ctx = chart.ctx;
      const yTop = 25, yBot = -50;
      const top = y.getPixelForValue(yTop);
      const bottom = y.getPixelForValue(yBot);
      const grad = ctx.createLinearGradient(0, top, 0, bottom);
      // Map zone boundaries to gradient stops (0=top=25, 1=bottom=-50)
      const t = v => Math.max(0, Math.min(1, (yTop - v) / (yTop - yBot)));
      const cY=css('--yellow'), cB=css('--blue'), cG=css('--color-other'), cGr=css('--green'), cR=css('--red');
      grad.addColorStop(0, cY);       // above 20: yellow
      grad.addColorStop(t(20), cY);
      grad.addColorStop(t(20), cB);   // 5-20: blue
      grad.addColorStop(t(5), cB);
      grad.addColorStop(t(5), cG);    // -10 to 5: grey
      grad.addColorStop(t(-10), cG);
      grad.addColorStop(t(-10), cGr); // -30 to -10: green
      grad.addColorStop(t(-30), cGr);
      grad.addColorStop(t(-30), cR);  // below -30: red
      grad.addColorStop(1, cR);
      chart._formGradient = grad;
      // Apply gradient to TSB datasets
      chart.data.datasets.forEach(ds => {
        if (ds._useFormGrad) ds.borderColor = grad;
      });
    }
  };

  if(charts.form) charts.form.destroy();
  charts.form = new Chart(document.getElementById('formChart'),{
    type:'line', plugins:[formPlugin],
    data:{datasets:[
      {label:'Form (TSB)',data:allActual.map(d=>({x:d.date,y:d.tsb})),
        _useFormGrad:true, ...lineStyle('--color-form')},
      {label:'Projected',data:projected.map(d=>({x:d.date,y:d.tsb})),
        _useFormGrad:true, ...projStyle('--color-form')},
    ]},
    options: chartOpts({
      scales:{
        x:sharedX,
        y:{min:-50,max:25,ticks:{color:TC()},grid:{color:GC()},title:{display:true,text:'Form (TSB)',color:TC()}},
        y1:{position:'right',ticks:{color:'transparent'},grid:{display:false},title:{display:true,text:'Daily TSS',color:'transparent'}},
      },
      plugins:{legend:{labels:legendLabels({
        generateLabels(chart){
          return chart.data.datasets.map((ds,i)=>({
            text:ds.label,datasetIndex:i,pointStyle:'line',
            strokeStyle:css('--color-form'),fillStyle:'transparent',
            lineDash:ds.borderDash?[4,3]:[],lineWidth:2,hidden:!chart.isDatasetVisible(i)
          }));
        }
      })},tooltip:{position:'nearest'}}
    })
  });

  // Volume chart
  const wks = getVolWeeks();
  const volLabels = wks.map(weekToDate);
  if(charts.vol) charts.vol.destroy();
  charts.vol = new Chart(document.getElementById('volumeChart'),{
    type:'bar',
    data:{ labels: volLabels, datasets:[
      {label:'Run',data:wks.map(w=>D.weekly_volume[w].run),backgroundColor:sportColor('run')},
      {label:'Bike',data:wks.map(w=>D.weekly_volume[w].bike),backgroundColor:sportColor('bike')},
      {label:'Swim',data:wks.map(w=>D.weekly_volume[w].swim),backgroundColor:sportColor('swim')},
      {label:'Other',data:wks.map(w=>D.weekly_volume[w].other),backgroundColor:css('--color-other')},
    ]},
    options: chartOpts({
      scales:{
        x:{...sharedX,stacked:true},
        y:{stacked:true,ticks:{color:TC()},grid:{color:GC()},title:{display:true,text:'Hours',color:TC()}},
        y1:{position:'right',ticks:{color:'transparent'},grid:{display:false},title:{display:true,text:'Daily TSS',color:'transparent'}},
      },
      plugins:{legend:{labels:legendLabels()}}
    })
  });
  } // end renderDashboardCharts
}

// ─── Running ───
function renderRunning(S) {
  const runs = D.runs, vo2 = D.vo2max.run;
  document.getElementById('run-stats').innerHTML = `
    <div class="stat"><div class="value blue">${S.lthr_run}</div><div class="label">LTHR (bpm)</div></div>
    <div class="stat"><div class="value green">${fmtPace(S.threshold_pace_mi)}</div><div class="label">Threshold Pace (/mi)</div></div>
    <div class="stat"><div class="value">${fmtPace(Math.round(S.threshold_pace_mi*1.2))}</div><div class="label">Easy Pace (/mi)</div></div>
    <div class="stat"><div class="value green">${vo2.length ? vo2[vo2.length-1].value : '\u2013'}</div><div class="label">VO2max</div></div>
    <div class="stat"><div class="value">${S.max_hr_run}</div><div class="label">Max HR</div></div>
    <div class="stat"><div class="value">${runs.length}</div><div class="label">Total Runs</div></div>
  `;
  document.getElementById('run-count-badge').textContent = `${Math.min(runs.length,50)} shown`;

  let h = '<table><thead><tr><th>Date</th><th>Name</th><th>Dist</th><th>Pace</th><th>Avg HR</th><th>Max HR</th><th>TSS</th></tr></thead><tbody>';
  runs.slice(0,50).forEach(r => {
    h += `<tr><td>${fmtDateShort(r.date)}</td><td>${r.name.slice(0,35)}</td><td>${r.distance_mi} mi</td>
      <td>${fmtPace(r.pace_mi)}/mi</td><td>${r.avgHR||'\u2013'}</td><td>${r.maxHR||'\u2013'}</td>
      <td>${r.tss||'\u2013'}</td></tr>`;
  });
  document.getElementById('run-table').innerHTML = h + '</tbody></table>';

  const pr = runs.filter(r=>r.pace_mi).slice(0,50).reverse();
  if(charts.pace) charts.pace.destroy();
  charts.pace = new Chart(document.getElementById('paceTrendChart'),{
    type:'line',
    data:{datasets:[{ label:'Pace', data:pr.map(r=>({x:r.date,y:r.pace_mi})), ...dotStyle('--color-run') }]},
    options: chartOpts({
      scales:{
        x:{type:'time',offset:false,time:{unit:'month',displayFormats:{month:"MMM ''yy"}},ticks:{color:TC()},grid:{color:GC(),offset:false}},
        y:{reverse:true,ticks:{color:TC(),callback:v=>fmtPace(v)},grid:{color:GC()},title:{display:true,text:'Pace (min/mi)',color:TC()}},
      },
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`${fmtPace(c.raw.y)}/mi`}}}
    })
  });
}

// ─── Cycling ───
function renderCycling(S) {
  const rides = D.rides, vo2 = D.vo2max.bike, ftp = S.ftp || '\u2013';
  const wkg = (ftp !== '\u2013' && S.weight_lb) ? (ftp / (S.weight_lb / 2.205)).toFixed(1) : null;
  document.getElementById('bike-stats').innerHTML = `
    <div class="stat"><div class="value blue">${ftp}<small style="font-size:12px;font-weight:400;color:var(--text-dim)">W</small></div><div class="label">FTP</div></div>
    ${wkg ? `<div class="stat"><div class="value blue">${wkg}</div><div class="label">W/kg</div></div>` : ''}
    <div class="stat"><div class="value blue">${S.lthr_bike}</div><div class="label">LTHR (bpm)</div></div>
    <div class="stat"><div class="value green">${vo2.length ? vo2[vo2.length-1].value : '\u2013'}</div><div class="label">VO2max</div></div>
    <div class="stat"><div class="value">${S.max_hr_bike}</div><div class="label">Max HR</div></div>
    <div class="stat"><div class="value">${rides.length}</div><div class="label">Total Rides</div></div>
  `;
  document.getElementById('ride-count-badge').textContent = `${Math.min(rides.length,50)} shown`;

  let h = '<table><thead><tr><th>Date</th><th>Name</th><th>Dur</th><th>Dist</th><th>Avg W</th><th>NP</th><th>Avg HR</th><th>TSS</th></tr></thead><tbody>';
  rides.slice(0,50).forEach(r => {
    h += `<tr><td>${fmtDateShort(r.date)}</td><td>${r.name.slice(0,35)}</td><td>${fmtDur(r.duration)}</td>
      <td>${r.distance_mi} mi</td><td>${r.avgPower||'\u2013'}</td><td>${r.normPower||'\u2013'}</td>
      <td>${r.avgHR||'\u2013'}</td><td>${r.tss||'\u2013'}</td></tr>`;
  });
  document.getElementById('ride-table').innerHTML = h + '</tbody></table>';

  // Power curve — show ALL requested durations, skip nulls gracefully
  const pc = D.power_curve;
  const allLabels = pc._labels || [];
  const allDurs = (pc._durations || []).map(String);
  const allVals = allDurs.map(k => pc[k]);

  if(charts.pc) charts.pc.destroy();
  charts.pc = new Chart(document.getElementById('powerCurveChart'),{
    type:'line',
    data:{
      labels: allLabels,
      datasets:[
        { label:'Best Power', data: allVals, ...dotStyle('--color-bike', {
          borderWidth:2.5, spanGaps:true, pointRadius: allVals.map(v=>v!=null?4:0), tension:0.3
        })},
        { label:'FTP', data: allLabels.map(()=>ftp), borderColor:css('--red')+'66', borderWidth:1.5,
          borderDash:[6,4], fill:false, pointRadius:0 },
      ]
    },
    options: chartOpts({
      scales:{
        x:{offset:false,ticks:{color:TC()},grid:{color:GC(),offset:false}},
        y:{ticks:{color:TC()},grid:{color:GC()},title:{display:true,text:'Watts',color:TC()}},
      },
      plugins:{legend:{labels:legendLabels()},
        tooltip:{callbacks:{label:c=>c.raw!=null?`${c.dataset.label}: ${c.raw}W`:'No data'}}}
    })
  });
}

// ─── Swimming ───
function renderSwimming(S) {
  const swims = D.swims;
  document.getElementById('swim-stats').innerHTML = `
    <div class="stat"><div class="value purple">${fmtPace(S.css_100yd)}</div><div class="label">CSS (/100yd)</div></div>
    <div class="stat"><div class="value">${S.max_hr_swim || D.auto.max_hr_swim || '\u2013'}</div><div class="label">Max HR</div></div>
    <div class="stat"><div class="value">${swims.length}</div><div class="label">Total Swims</div></div>
  `;
  document.getElementById('swim-count-badge').textContent = `${Math.min(swims.length,50)} shown`;

  let h = '<table><thead><tr><th>Date</th><th>Name</th><th>Dur</th><th>Dist</th><th>Pace</th><th>Avg HR</th><th>TSS</th></tr></thead><tbody>';
  swims.slice(0,50).forEach(s => {
    h += `<tr><td>${fmtDateShort(s.date)}</td><td>${s.name.slice(0,30)}</td><td>${fmtDur(s.duration)}</td>
      <td>${s.distance_yd} yd</td><td>${fmtPace(s.pace_100yd)}/100yd</td>
      <td>${s.avgHR||'\u2013'}</td><td>${s.tss||'\u2013'}</td></tr>`;
  });
  document.getElementById('swim-table').innerHTML = h + '</tbody></table>';

  const ps = swims.filter(s=>s.pace_100yd).slice(0,50).reverse();
  if(charts.swimTrend) charts.swimTrend.destroy();
  charts.swimTrend = new Chart(document.getElementById('swimTrendChart'),{
    type:'line',
    data:{datasets:[{ label:'Pace', data:ps.map(s=>({x:s.date,y:s.pace_100yd})), ...dotStyle('--color-swim') }]},
    options: chartOpts({
      scales:{
        x:{type:'time',offset:false,time:{unit:'month',displayFormats:{month:"MMM ''yy"}},ticks:{color:TC()},grid:{color:GC(),offset:false}},
        y:{reverse:true,ticks:{color:TC(),callback:v=>fmtPace(v)},grid:{color:GC()},title:{display:true,text:'Pace (/100yd)',color:TC()}},
      },
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>`${fmtPace(c.raw.y)}/100yd`}}}
    })
  });
}

// ─── Zones ───
function renderZones(S) {
  let h = '';
  h += zoneTableHTML('Running \u2014 Heart Rate (Friel)', hrZones(S.lthr_run), 'bpm');
  h += zoneTableHTML('Running \u2014 Pace (Friel)', paceZones(S.threshold_pace_mi), '/mi');
  h += zoneTableHTML('Cycling \u2014 Heart Rate (Friel)', hrZones(S.lthr_bike), 'bpm');
  if (S.ftp) h += zoneTableHTML('Cycling \u2014 Power (Coggan)', pwrZones(S.ftp), 'W');
  h += zoneTableHTML('Swimming \u2014 Pace (CSS)', cssZones(S.css_100yd), '/100yd');
  document.getElementById('zone-tables').innerHTML = h;
}

// ─── Settings ───
function renderSettings(S) {
  const ov = getOverrides();
  let h = '';
  SETTINGS_KEYS.forEach(sk => {
    const auto = D.auto[sk.key];
    const cur = ov[sk.key];
    const disp = sk.format ? sk.format(auto) : `${auto} ${sk.unit}`;
    const has = cur != null;
    h += `<div class="setting-row">
      <div><div class="setting-label">${sk.label}</div><div class="setting-desc">${sk.desc}</div></div>
      <div class="auto-val">Auto: ${disp}</div>
      <input type="number" id="setting-${sk.key}" value="${has?cur:''}" placeholder="${auto}" step="1">
      <button class="btn" data-key="${sk.key}" ${!has?'disabled':''}>Reset</button>
    </div>`;
  });
  document.getElementById('settings-form').innerHTML = h;

  SETTINGS_KEYS.forEach(sk => {
    document.getElementById(`setting-${sk.key}`).addEventListener('change', e => {
      const v = e.target.value ? parseInt(e.target.value) : null;
      const o = getOverrides();
      if (v != null) o[sk.key] = v; else delete o[sk.key];
      saveSettings(o); renderAll();
    });
  });
  document.querySelectorAll('.setting-row .btn[data-key]').forEach(btn => {
    btn.addEventListener('click', () => {
      const o = getOverrides(); delete o[btn.dataset.key]; saveSettings(o); renderAll();
    });
  });
  document.getElementById('reset-all-btn').onclick = () => { localStorage.removeItem('trainingOverrides'); renderAll(); };
  document.getElementById('save-overrides-btn').onclick = () => {
    const o = getOverrides();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(o,null,2)],{type:'application/json'}));
    a.download = 'user_overrides.json'; a.click();
  };

  // ── Life Events ──
  function renderEvtTable() {
    const events = getLifeEvents();
    if (!events.length) {
      document.getElementById('evt-table').innerHTML = '<p style="color:var(--text-faint);font-size:13px;">No events yet. Add one above to see markers on the Fitness &amp; Fatigue chart.</p>';
      return;
    }
    const sorted = [...events].sort((a,b) => b.date.localeCompare(a.date));
    let h = '<table><thead><tr><th>Date</th><th>Type</th><th>Label</th><th>Notes</th><th></th></tr></thead><tbody>';
    sorted.forEach((ev, i) => {
      const etype = EVENT_TYPES[ev.type] || EVENT_TYPES.other;
      const color = css(etype.color);
      h += `<tr>
        <td>${fmtDate(ev.date)}</td>
        <td><span style="color:${color};font-weight:600">${etype.label}</span></td>
        <td>${ev.label}</td>
        <td style="color:var(--text-dim)">${ev.notes || '\u2013'}</td>
        <td><button class="btn evt-del" data-idx="${events.indexOf(ev)}" style="padding:2px 8px;font-size:11px">Delete</button></td>
      </tr>`;
    });
    document.getElementById('evt-table').innerHTML = h + '</tbody></table>';
    document.querySelectorAll('.evt-del').forEach(btn => {
      btn.addEventListener('click', () => {
        const evts = getLifeEvents();
        evts.splice(parseInt(btn.dataset.idx), 1);
        saveLifeEvents(evts);
        renderEvtTable();
        renderAll();
      });
    });
  }
  renderEvtTable();

  document.getElementById('evt-add-btn').addEventListener('click', () => {
    const d = document.getElementById('evt-date').value;
    const t = document.getElementById('evt-type').value;
    const l = document.getElementById('evt-label').value.trim();
    const n = document.getElementById('evt-notes').value.trim();
    if (!d || !l) return;
    const evts = getLifeEvents();
    evts.push({ date: d, type: t, label: l, notes: n || null });
    saveLifeEvents(evts);
    document.getElementById('evt-date').value = '';
    document.getElementById('evt-label').value = '';
    document.getElementById('evt-notes').value = '';
    renderEvtTable();
    renderAll();
  });
}

// ═══════════════════════════════════════
//  TRAINING INSIGHTS ENGINE
// ═══════════════════════════════════════
function generateInsights(S) {
  const insights = [];
  const actual = D.tsb.filter(d => !d.projected);
  const today = actual[actual.length - 1] || {};
  const tsb = today.tsb || 0;
  const ctl = today.ctl || 0;
  const atl = today.atl || 0;

  // ── 1. Training load direction ──
  const weekAgo = actual[Math.max(0, actual.length - 8)] || {};
  const twoWeeksAgo = actual[Math.max(0, actual.length - 15)] || {};
  const ctlDelta = ctl - (weekAgo.ctl || ctl);
  const ctlTrend = ctlDelta > 1 ? 'rising' : ctlDelta < -1 ? 'falling' : 'stable';
  if (ctlTrend === 'rising') {
    insights.push({ color: 'green', title: 'Fitness Trending Up',
      body: `CTL rose <strong>${ctlDelta.toFixed(1)}</strong> points this week (${weekAgo.ctl?.toFixed(0) || '?'} → ${ctl.toFixed(0)}). Your training is building fitness. Keep the consistency but watch fatigue.`
    });
  } else if (ctlTrend === 'falling') {
    insights.push({ color: 'yellow', title: 'Fitness Declining',
      body: `CTL dropped <strong>${Math.abs(ctlDelta).toFixed(1)}</strong> points this week. This could be intentional rest or a gap in training. If unplanned, getting back to consistent sessions will reverse this quickly.`
    });
  } else {
    insights.push({ color: 'blue', title: 'Fitness Holding Steady',
      body: `CTL is stable at <strong>${ctl.toFixed(0)}</strong>. You're maintaining your current fitness level. To improve, gradually increase training volume or intensity.`
    });
  }

  // ── 2. Fatigue & readiness ──
  if (tsb < -30) {
    insights.push({ color: 'red', title: 'High Fatigue — Recovery Needed',
      body: `TSB is at <strong>${tsb.toFixed(0)}</strong>, well into the high-risk zone. You've been loading hard. Take an easy day or full rest day to avoid overtraining. Light activity only.`
    });
  } else if (tsb < -10) {
    insights.push({ color: 'green', title: 'Productive Training Load',
      body: `TSB at <strong>${tsb.toFixed(0)}</strong> — you're in the optimal training zone. Fatigue is present but manageable. This is where fitness gains happen. A moderate session today is appropriate.`
    });
  } else if (tsb < 5) {
    insights.push({ color: 'blue', title: 'Balanced State',
      body: `TSB at <strong>${tsb.toFixed(0)}</strong> — you're neither fresh nor fatigued. Good day for a quality workout: tempo run, threshold intervals, or a longer endurance session.`
    });
  } else if (tsb <= 20) {
    insights.push({ color: 'blue', title: 'Fresh & Ready',
      body: `TSB at <strong>${tsb.toFixed(0)}</strong> — you're well-recovered. Great day for a hard session or race effort. Your body can absorb a big training stimulus right now.`
    });
  } else {
    insights.push({ color: 'yellow', title: 'Detraining Risk',
      body: `TSB at <strong>${tsb.toFixed(0)}</strong> — you're very fresh, which means you haven't been loading much. If this isn't a planned taper, consider getting back to structured training before fitness erodes.`
    });
  }

  // ── 3. HRV signal ──
  const hrv = D.hrv || {};
  const bl = hrv.baseline || {};
  const hrvVal = hrv.lastNightAvg;
  const blLow = bl.balancedLow || 0;
  const blHigh = bl.balancedUpper || 0;
  if (hrvVal && blLow && blHigh) {
    if (hrvVal < blLow) {
      insights.push({ color: 'red', title: 'HRV Below Baseline',
        body: `Last night's HRV was <strong>${hrvVal} ms</strong> (baseline: ${blLow}–${blHigh}). Your nervous system is signaling fatigue or stress. Prioritize sleep, hydration, and easy activity today.`
      });
    } else if (hrvVal > blHigh) {
      insights.push({ color: 'green', title: 'HRV Above Baseline',
        body: `Last night's HRV was <strong>${hrvVal} ms</strong>, above your baseline range. Your body is well-recovered. Good opportunity for a harder training session.`
      });
    } else {
      insights.push({ color: 'green', title: 'HRV Within Baseline',
        body: `Last night's HRV was <strong>${hrvVal} ms</strong> (baseline: ${blLow}–${blHigh}). Recovery looks normal. Train as planned.`
      });
    }
  }

  // ── 4. Today's suggestion ──
  const dayOfWeek = new Date().getDay(); // 0=Sun
  let suggestion = '';
  if (tsb < -25) {
    suggestion = 'Rest day or very light recovery activity (easy walk, gentle swim). Your body needs to absorb recent training.';
  } else if (tsb < -10) {
    if (dayOfWeek === 0 || dayOfWeek === 1) {
      suggestion = 'Easy aerobic session — Zone 2 run or recovery spin. Save intensity for mid-week when fatigue settles.';
    } else {
      suggestion = 'Moderate session — steady-state threshold work or tempo intervals. You have room to push a bit.';
    }
  } else if (tsb < 5) {
    suggestion = 'Quality session day — threshold intervals, hill repeats, or a race-pace workout. You\'re balanced and can handle intensity.';
  } else {
    suggestion = 'You\'re fresh — great day for your hardest workout of the week: long intervals, time trial effort, or a long endurance session.';
  }
  insights.push({ color: 'blue', title: 'Today\'s Suggestion', body: suggestion });

  return insights;
}

function renderInsights(S) {
  const insights = generateInsights(S);
  let h = '<div class="insights-header"><h3>Training Insights</h3></div>';
  h += '<div class="insights-grid">';
  insights.forEach(ins => {
    h += `<div class="insight ${ins.color}">
      <div class="insight-title">${ins.title}</div>
      <div class="insight-body">${ins.body}</div>
    </div>`;
  });
  h += '</div>';
  document.getElementById('insights-card').innerHTML = h;
}

// ═══════════════════════════════════════
renderAll();
</script>
</body>
</html>"""


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate fitness dashboard")
    parser.add_argument("--open", action="store_true", help="Open dashboard in browser")
    args = parser.parse_args()

    print("Computing data...")
    data = compute_all()

    # Save data JSON
    data_path = OUTPUT_DIR / "dashboard_data.json"
    with open(data_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Data → {data_path}")

    # Generate HTML
    html = generate_html(data)
    html_path = OUTPUT_DIR / "fitness_dashboard.html"
    with open(html_path, "w") as f:
        f.write(html)
    print(f"  Dashboard → {html_path}")

    # Summary
    eff = data["effective"]
    print(f"\n  FTP: {eff.get('ftp', 'N/A')}W  |  LTHR Run: {eff['lthr_run']}  Bike: {eff['lthr_bike']}")
    print(f"  Threshold Pace: {formatPace(eff['threshold_pace_mi'])}/mi  |  CSS: {formatSwimPace(eff['css_100yd'])}/100yd")

    today_tsb = [d for d in data["tsb"] if d["date"] == date.today().isoformat()]
    if today_tsb:
        t = today_tsb[0]
        print(f"  CTL: {t['ctl']}  ATL: {t['atl']}  TSB: {t['tsb']}")

    if args.open:
        subprocess.run(["open", str(html_path)])


def formatPace(secs):
    if not secs or secs <= 0:
        return "-"
    m = int(secs // 60)
    s = int(secs % 60)
    return f"{m}:{s:02d}"

def formatSwimPace(secs):
    return formatPace(secs)


if __name__ == "__main__":
    main()
