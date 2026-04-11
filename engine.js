// ═══════════════════════════════════════════════════════════════════
//  engine.js — Computation engine (ported from fitness_analysis.py)
//  All threshold estimation, TSS, TSB, enrichment, and aggregation
// ═══════════════════════════════════════════════════════════════════

const KM_TO_MI = 0.621371;
const M_TO_YD = 1.09361;
const RUN_TYPES = ['running', 'trail_running', 'treadmill_running'];
const BIKE_TYPES = ['road_biking', 'mountain_biking', 'cycling', 'virtual_ride', 'indoor_cycling', 'gravel_cycling'];
const SWIM_TYPES = ['lap_swimming', 'open_water_swimming'];

function sportCategory(atype) {
  if (RUN_TYPES.includes(atype)) return 'run';
  if (BIKE_TYPES.includes(atype)) return 'bike';
  if (SWIM_TYPES.includes(atype)) return 'swim';
  return 'other';
}

// ── Auto-estimation functions ────────────────────────────────────

function estimateMaxHR(acts) {
  const maxHrs = {};
  for (const a of acts) {
    const sport = (a.activityType || {}).typeKey || '';
    const hr = a.maxHR || 0;
    if (hr > (maxHrs[sport] || 0)) maxHrs[sport] = hr;
  }
  const runMax = Math.max(maxHrs['running'] || 0, maxHrs['trail_running'] || 0);
  const bikeMax = Math.max(maxHrs['road_biking'] || 0, maxHrs['mountain_biking'] || 0,
    maxHrs['cycling'] || 0, maxHrs['virtual_ride'] || 0, maxHrs['indoor_cycling'] || 0);
  const swimMax = maxHrs['lap_swimming'] || 0;
  return { run: Math.round(runMax), bike: Math.round(bikeMax), swim: Math.round(swimMax) };
}

function estimateLthrRun(acts, maxHR) {
  const candidates = [];
  for (const a of acts) {
    const atype = (a.activityType || {}).typeKey || '';
    if (atype !== 'running' && atype !== 'trail_running') continue;
    const avgHR = a.averageHR || 0;
    const dur = a.duration || 0;
    if (avgHR <= 0 || dur < 1200) continue;
    if (dur > 3600 && avgHR > maxHR * 0.85) {
      candidates.push(avgHR + 3);
    } else if (dur >= 1200 && avgHR > maxHR * 0.83) {
      const bump = dur > 2400 ? 3 : 2;
      candidates.push(avgHR + bump);
    }
  }
  if (!candidates.length) return Math.round(maxHR * 0.89);
  candidates.sort((a, b) => b - a);
  const top = candidates.slice(0, Math.min(5, candidates.length));
  return Math.round(top.reduce((s, v) => s + v, 0) / top.length);
}

function estimateLthrBike(acts, maxHR) {
  const candidates = [];
  const bikeTypes = ['road_biking', 'mountain_biking', 'cycling', 'virtual_ride', 'indoor_cycling', 'gravel_cycling'];
  for (const a of acts) {
    const atype = (a.activityType || {}).typeKey || '';
    if (!bikeTypes.includes(atype)) continue;
    const avgHR = a.averageHR || 0;
    const dur = a.duration || 0;
    const npVal = a.normPower || 0;
    if (avgHR <= 0 || dur < 1200) continue;
    if (dur <= 5400 && avgHR > maxHR * 0.80) {
      const bump = dur > 2400 ? 4 : 2;
      candidates.push(avgHR + bump);
    } else if (npVal > 220 && avgHR > maxHR * 0.75) {
      candidates.push(avgHR + 5);
    }
  }
  if (!candidates.length) return Math.round(maxHR * 0.87);
  candidates.sort((a, b) => b - a);
  const top = candidates.slice(0, Math.min(5, candidates.length));
  return Math.round(top.reduce((s, v) => s + v, 0) / top.length);
}

function estimateFtp(acts) {
  const bikeTypes = ['road_biking', 'mountain_biking', 'cycling', 'virtual_ride', 'indoor_cycling', 'gravel_cycling'];
  let best = 0;
  for (const a of acts) {
    if (!bikeTypes.includes((a.activityType || {}).typeKey || '')) continue;
    const p20 = a.max20MinPower || 0;
    if (p20 > best) best = p20;
  }
  return best > 0 ? Math.round(best * 0.95) : null;
}

function estimateThresholdPace(acts, lthr) {
  const hrFloor = lthr ? lthr * 0.90 : 155;
  const candidates = [];
  for (const a of acts) {
    const atype = (a.activityType || {}).typeKey || '';
    if (atype !== 'running' && atype !== 'trail_running') continue;
    const speed = a.averageSpeed || 0;
    const dur = a.duration || 0;
    const avgHR = a.averageHR || 0;
    if (speed <= 0 || dur < 1200 || !avgHR || avgHR < hrFloor) continue;
    candidates.push(1609.344 / speed);
  }
  if (!candidates.length) return 530;
  candidates.sort((a, b) => a - b);
  const top = candidates.slice(0, Math.min(5, candidates.length));
  return Math.round(top.reduce((s, v) => s + v, 0) / top.length);
}

// Jack Daniels VDOT-based pace calculation from race time
function vdotFromRace(distMeters, timeSec) {
  // VO2 cost of running at given speed (ml/kg/min)
  const v = distMeters / timeSec; // m/s
  const vMin = v * 60; // m/min
  const vo2 = -4.6 + 0.182258 * vMin + 0.000104 * vMin * vMin;
  // Fraction of VO2max sustainable for given duration
  const t = timeSec / 60; // minutes
  const pctMax = 0.8 + 0.1894393 * Math.exp(-0.012778 * t) + 0.2989558 * Math.exp(-0.1932605 * t);
  return vo2 / pctMax;
}

function vdotPace(vdot, fraction) {
  // Given VDOT and %VO2max fraction, return pace in sec/mile
  const vo2 = vdot * fraction;
  // Inverse of VO2-speed equation: solve for vMin
  // vo2 = -4.6 + 0.182258*vMin + 0.000104*vMin^2
  const a = 0.000104, b = 0.182258, c = -4.6 - vo2;
  const vMin = (-b + Math.sqrt(b * b - 4 * a * c)) / (2 * a);
  const vMs = vMin / 60;
  return Math.round(1609.344 / vMs);
}

function thresholdPaceFromRace(distMeters, timeSec) {
  const vdot = vdotFromRace(distMeters, timeSec);
  return vdotPace(vdot, 0.88); // threshold ~88% VO2max
}

function cssFromTimeTrial(t400sec, t200sec) {
  // Industry standard: CSS = 200 / (T400 - T200) in meters/sec, convert to sec/100yd
  const cssMs = 200 / (t400sec - t200sec);
  const cssYdS = cssMs * M_TO_YD;           // yards per second
  return Math.round(100 / cssYdS);           // seconds per 100 yards
}

function estimateCSS(acts) {
  const swims = [];
  for (const a of acts) {
    if ((a.activityType || {}).typeKey !== 'lap_swimming') continue;
    const distM = a.distance || 0;
    const avgSpeed = a.averageSpeed || 0;
    let dur;
    if (distM > 0 && avgSpeed > 0) {
      dur = distM / avgSpeed;
    } else {
      dur = a.duration || 0;
    }
    if (distM > 0 && dur > 0) {
      const distYd = distM * M_TO_YD;
      swims.push((dur / distYd) * 100);
    }
  }
  if (!swims.length) return 110;
  swims.sort((a, b) => a - b);
  const idx = Math.max(0, Math.floor(swims.length / 5));
  return Math.round(swims[idx]);
}

// ── RPE-based TSS ───────────────────────────────────────────────

// IF values calibrated so RPE-based TSS ≈ HR-based TSS: IF = (HR/LTHR)^1.75
const RPE_TO_IF = [0, 0.50, 0.56, 0.63, 0.74, 0.80, 0.87, 0.93, 0.99, 1.05, 1.12];

function calcRpeTSS(durationSec, rpe) {
  if (!durationSec || durationSec <= 0 || !rpe || rpe < 1 || rpe > 10) return 0;
  const ifVal = RPE_TO_IF[Math.round(rpe)];
  return Math.round((durationSec / 3600) * ifVal * ifVal * 100);
}

// ── TSS calculation ──────────────────────────────────────────────

function calcTSS(activity, lthrRun, lthrBike, ftp) {
  const avgHR = activity.averageHR || 0;
  const dur = activity.duration || 0;
  const npVal = activity.normPower || 0;
  const atype = (activity.activityType || {}).typeKey || '';
  if (dur <= 0) return 0;
  // Power TSS for cycling
  if (BIKE_TYPES.includes(atype) && ftp && ftp > 0 && npVal > 0) {
    const intensity = npVal / ftp;
    return Math.round((dur / 3600) * intensity * intensity * 100);
  }
  // hrTSS fallback
  if (avgHR <= 0) {
    if (activity.rpe > 0) return calcRpeTSS(dur, activity.rpe);
    return 0;
  }
  const lthr = RUN_TYPES.includes(atype) ? lthrRun : BIKE_TYPES.includes(atype) ? lthrBike : lthrRun;
  if (lthr <= 0) return 0;
  return Math.round((dur / 3600) * Math.pow(avgHR / lthr, 3.5) * 100);
}

// ── TSB model ────────────────────────────────────────────────────

function buildTSB(acts, lthrRun, lthrBike, ftp, lookahead) {
  if (lookahead === undefined) lookahead = 14;
  const dailyTSS = {};
  for (const a of acts) {
    if (!a.duration) continue;
    const d = (a.startTimeLocal || '').slice(0, 10);
    if (!d) continue;
    dailyTSS[d] = (dailyTSS[d] || 0) + calcTSS(a, lthrRun, lthrBike, ftp);
  }

  const allDates = Object.keys(dailyTSS).sort();
  if (!allDates.length) return [];

  const start = new Date(allDates[0] + 'T00:00:00');
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const end = new Date(today);
  end.setDate(end.getDate() + lookahead);

  let ctl = 0, atl = 0;
  const result = [];
  const current = new Date(start);

  while (current <= end) {
    const d = current.toISOString().slice(0, 10);
    const tss = dailyTSS[d] || 0;
    ctl += (tss - ctl) / 42;
    atl += (tss - atl) / 7;
    result.push({
      date: d,
      tss: Math.round(tss * 10) / 10,
      ctl: Math.round(ctl * 10) / 10,
      atl: Math.round(atl * 10) / 10,
      tsb: Math.round((ctl - atl) * 10) / 10,
      projected: current > today,
    });
    current.setDate(current.getDate() + 1);
  }
  return result;
}

// ── Weekly volume ────────────────────────────────────────────────

function getISOWeek(dateStr) {
  const d = new Date(dateStr + 'T00:00:00');
  const dayNum = d.getDay() || 7;
  d.setDate(d.getDate() + 4 - dayNum);
  const yearStart = new Date(d.getFullYear(), 0, 1);
  const weekNum = Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
  return `${d.getFullYear()}-W${String(weekNum).padStart(2, '0')}`;
}

function weeklyVolume(acts) {
  const weeks = {};
  for (const a of acts) {
    const dur = a.duration || 0;
    if (!dur) continue;
    const d = (a.startTimeLocal || '').slice(0, 10);
    if (!d) continue;
    const key = getISOWeek(d);
    if (!weeks[key]) weeks[key] = { run: 0, bike: 0, swim: 0, other: 0 };
    const cat = sportCategory((a.activityType || {}).typeKey || '');
    weeks[key][cat] += dur / 3600;
  }
  // Round values
  for (const wk of Object.values(weeks)) {
    for (const k of Object.keys(wk)) {
      wk[k] = Math.round(wk[k] * 10) / 10;
    }
  }
  // Sort by week key
  const sorted = {};
  for (const k of Object.keys(weeks).sort()) sorted[k] = weeks[k];
  return sorted;
}

// ── Power curve ──────────────────────────────────────────────────

function buildPowerCurve(acts) {
  const durations = [];
  for (let i = 1; i <= 60; i++) durations.push(i);
  for (let i = 65; i <= 120; i += 5) durations.push(i);
  for (let i = 130; i <= 300; i += 10) durations.push(i);
  for (let i = 330; i <= 600; i += 30) durations.push(i);
  for (let i = 660; i <= 3600; i += 60) durations.push(i);
  for (let i = 3900; i <= 18000; i += 300) durations.push(i);

  const bikeActs = acts.filter(a => BIKE_TYPES.includes((a.activityType || {}).typeKey || ''));
  const now = new Date();
  const cutoff365 = new Date(now); cutoff365.setDate(cutoff365.getDate() - 365);
  const cutoff42 = new Date(now); cutoff42.setDate(cutoff42.getDate() - 42);
  const cutoff365Str = cutoff365.toISOString().slice(0, 10);
  const cutoff42Str = cutoff42.toISOString().slice(0, 10);
  const acts365 = bikeActs.filter(a => (a.startTimeLocal || '') >= cutoff365Str);
  const acts42 = bikeActs.filter(a => (a.startTimeLocal || '') >= cutoff42Str);

  const best = {};
  const best365 = {};
  const best42 = {};
  for (const dur of durations) {
    const field = `maxAvgPower_${dur}`;
    let maxAll = 0, max365 = 0, max42 = 0;
    for (const a of bikeActs) {
      const v = a[field] || 0;
      if (v > maxAll) maxAll = v;
    }
    for (const a of acts365) {
      const v = a[field] || 0;
      if (v > max365) max365 = v;
    }
    for (const a of acts42) {
      const v = a[field] || 0;
      if (v > max42) max42 = v;
    }
    best[String(dur)] = maxAll > 0 ? Math.round(maxAll) : null;
    best365[String(dur)] = max365 > 0 ? Math.round(max365) : null;
    best42[String(dur)] = max42 > 0 ? Math.round(max42) : null;
  }
  best._durations = durations;
  best._365 = best365;
  best._42 = best42;
  return best;
}

// ── Grade Adjusted Pace ─────────────────────────────────────────
// If per-second stream data has been synced, uses Minetti-based grade-adjusted
// distance (computed in strava.js). Otherwise falls back to a whole-run
// estimate using elevation gain with a net factor of 3.7.

function calcGAP(durationSec, distM, elevGain, gapAdjustedDist) {
  if (!durationSec || !distM || distM < 100) return null;
  // Prefer stream-based grade-adjusted distance when available
  if (gapAdjustedDist && gapAdjustedDist > 0) {
    const gapDistMi = gapAdjustedDist / 1609.344;
    return Math.round(durationSec / gapDistMi);
  }
  // Fallback: whole-run estimate from total elevation gain
  const gain = elevGain || 0;
  const pace_mi = Math.round(durationSec / (distM / 1609.344));
  if (gain <= 0) return pace_mi;
  const equivDist = distM + gain * 3.7;
  return Math.round(pace_mi * distM / equivDist);
}

// ── Enriched activity lists ──────────────────────────────────────

function enrichRuns(acts, lthrRun, lthrBike, ftp) {
  const runs = [];
  for (const a of acts) {
    const atype = (a.activityType || {}).typeKey || '';
    if (!RUN_TYPES.includes(atype)) continue;
    const speed = a.averageSpeed || 0;
    const distM = a.distance || 0;
    const dur = a.duration || 0;
    const pace_mi = speed > 0 ? Math.round(1609.344 / speed) : null;
    const elevGain = a.elevationGain || 0;
    runs.push({
      date: (a.startTimeLocal || '').slice(0, 10),
      name: a.activityName || '',
      type: atype,
      duration: Math.round(dur / 60 * 10) / 10,
      distance_mi: distM ? Math.round(distM / 1609.344 * 100) / 100 : 0,
      pace_mi,
      gap_mi: calcGAP(dur, distM, elevGain, a._gapAdjustedDist),
      avgHR: a.averageHR || null,
      maxHR: a.maxHR || null,
      cadence: a.averageRunningCadenceInStepsPerMinute || null,
      tss: calcTSS(a, lthrRun, lthrBike, ftp),
      elevGain,
      vo2max: a.vO2MaxValue || null,
    });
  }
  runs.sort((a, b) => b.date.localeCompare(a.date));
  return runs;
}

function enrichRides(acts, lthrRun, lthrBike, ftp) {
  const rides = [];
  for (const a of acts) {
    const atype = (a.activityType || {}).typeKey || '';
    if (!BIKE_TYPES.includes(atype)) continue;
    const distM = a.distance || 0;
    rides.push({
      date: (a.startTimeLocal || '').slice(0, 10),
      name: a.activityName || '',
      type: atype,
      duration: Math.round((a.duration || 0) / 60 * 10) / 10,
      distance_mi: distM ? Math.round(distM / 1609.344 * 100) / 100 : 0,
      avgPower: a.avgPower || null,
      normPower: a.normPower ? Math.round(a.normPower * 10) / 10 : null,
      maxPower: a.maxPower || null,
      max20Min: a.max20MinPower ? Math.round(a.max20MinPower * 10) / 10 : null,
      avgHR: a.averageHR || null,
      maxHR: a.maxHR || null,
      tss: calcTSS(a, lthrRun, lthrBike, ftp),
      elevGain: a.elevationGain || null,
      vo2max: a.vO2MaxValue || null,
    });
  }
  rides.sort((a, b) => b.date.localeCompare(a.date));
  return rides;
}

function enrichSwims(acts, lthrRun, lthrBike, ftp) {
  const swims = [];
  for (const a of acts) {
    if (!SWIM_TYPES.includes((a.activityType || {}).typeKey || '')) continue;
    const distM = a.distance || 0;
    const avgSpeed = a.averageSpeed || 0;
    let dur;
    if (distM > 0 && avgSpeed > 0) {
      dur = distM / avgSpeed;
    } else {
      dur = a.duration || 0;
    }
    const distYd = distM ? distM * M_TO_YD : 0;
    const pace = distYd > 0 ? Math.round((dur / distYd) * 100) : null;
    swims.push({
      date: (a.startTimeLocal || '').slice(0, 10),
      name: a.activityName || '',
      duration: Math.round(dur / 60 * 10) / 10,
      distance_yd: Math.round(distYd),
      pace_100yd: pace,
      avgHR: a.averageHR || null,
      maxHR: a.maxHR || null,
      tss: calcTSS(a, lthrRun, lthrBike, ftp),
    });
  }
  swims.sort((a, b) => b.date.localeCompare(a.date));
  return swims;
}

// ── VO2max trends ────────────────────────────────────────────────

function vo2maxTrends(acts) {
  const run = [], bike = [];
  for (const a of acts) {
    const v = a.vO2MaxValue;
    if (!v) continue;
    const atype = (a.activityType || {}).typeKey || '';
    const entry = { date: (a.startTimeLocal || '').slice(0, 10), value: v };
    if (RUN_TYPES.includes(atype)) run.push(entry);
    else if (BIKE_TYPES.includes(atype)) bike.push(entry);
  }
  return {
    run: run.sort((a, b) => a.date.localeCompare(b.date)),
    bike: bike.sort((a, b) => a.date.localeCompare(b.date)),
  };
}

// ── Main computation ─────────────────────────────────────────────

function computeAll(acts, overrides) {
  overrides = overrides || {};
  const maxHrs = estimateMaxHR(acts);

  const lthrRun = estimateLthrRun(acts, maxHrs.run);
  const auto = {
    ftp: estimateFtp(acts),
    lthr_run: lthrRun,
    lthr_bike: estimateLthrBike(acts, maxHrs.bike),
    threshold_pace_mi: estimateThresholdPace(acts, overrides.lthr_run || lthrRun),
    css_100yd: estimateCSS(acts),
    max_hr_run: maxHrs.run,
    max_hr_bike: maxHrs.bike,
    max_hr_swim: maxHrs.swim,
    weight_lb: null,
  };

  // Effective = auto merged with overrides
  const effective = { ...auto };
  for (const [k, v] of Object.entries(overrides)) {
    if (v != null) effective[k] = v;
  }

  const tsb = buildTSB(acts, effective.lthr_run, effective.lthr_bike, effective.ftp, 14);

  return {
    generated: new Date().toISOString().slice(0, 10),
    auto,
    overrides,
    effective,
    tsb,
    weekly_volume: weeklyVolume(acts),
    power_curve: buildPowerCurve(acts),
    runs: enrichRuns(acts, effective.lthr_run, effective.lthr_bike, effective.ftp),
    rides: enrichRides(acts, effective.lthr_run, effective.lthr_bike, effective.ftp),
    swims: enrichSwims(acts, effective.lthr_run, effective.lthr_bike, effective.ftp),
    vo2max: vo2maxTrends(acts),
    garmin_status: {},
    hrv: {},
  };
}
