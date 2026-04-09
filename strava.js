// ═══════════════════════════════════════════════════════════════════
//  strava.js — Strava OAuth, API, data mapping, IndexedDB persistence
// ═══════════════════════════════════════════════════════════════════

// ── Strava Configuration ─────────────────────────────────────────
// Register your app at https://www.strava.com/settings/api
// Set redirect URI to your deployed URL (and http://localhost:8000 for dev)
const STRAVA_CLIENT_ID = '222402';
const STRAVA_CLIENT_SECRET = 'aeb4985661fd80b0f4bf90954dd33202bafc6f4d';
const STRAVA_REDIRECT_URI = window.location.origin + window.location.pathname;

// ═══════════════════════════════════════════════════════════════════
//  IndexedDB
// ═══════════════════════════════════════════════════════════════════
const DB_NAME = 'TrainingHub';
const DB_VERSION = 1;
const STORE_NAME = 'activities';

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'activityId' });
      }
    };
    req.onsuccess = e => resolve(e.target.result);
    req.onerror = e => reject(e.target.error);
  });
}

async function dbGetAll() {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

async function dbPutAll(activities) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    for (const a of activities) store.put(a);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function dbClear() {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    const store = tx.objectStore(STORE_NAME);
    const req = store.clear();
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

// ═══════════════════════════════════════════════════════════════════
//  Strava OAuth
// ═══════════════════════════════════════════════════════════════════

function stravaIsConnected() {
  const tok = stravaGetToken();
  return tok && tok.access_token;
}

function stravaGetToken() {
  try { return JSON.parse(localStorage.getItem('strava_token')); }
  catch { return null; }
}

function stravaStartAuth() {
  const url = 'https://www.strava.com/oauth/authorize'
    + '?client_id=' + encodeURIComponent(STRAVA_CLIENT_ID)
    + '&redirect_uri=' + encodeURIComponent(STRAVA_REDIRECT_URI)
    + '&response_type=code'
    + '&scope=activity:read_all'
    + '&approval_prompt=auto';
  window.location.href = url;
}

async function stravaHandleCallback() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');
  if (!code) return false;

  // Clean URL
  window.history.replaceState({}, document.title, window.location.pathname);

  try {
    const resp = await fetch('https://www.strava.com/api/v3/oauth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: STRAVA_CLIENT_ID,
        client_secret: STRAVA_CLIENT_SECRET,
        code: code,
        grant_type: 'authorization_code',
      }),
    });
    if (!resp.ok) throw new Error('Token exchange failed: ' + resp.status);
    const data = await resp.json();
    localStorage.setItem('strava_token', JSON.stringify({
      access_token: data.access_token,
      refresh_token: data.refresh_token,
      expires_at: data.expires_at,
      athlete: data.athlete,
    }));
    setDataSource('strava');
    return true;
  } catch (err) {
    console.error('Strava token exchange error:', err);
    return false;
  }
}

async function stravaRefreshToken() {
  const tok = stravaGetToken();
  if (!tok || !tok.refresh_token) return false;
  try {
    const resp = await fetch('https://www.strava.com/api/v3/oauth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_id: STRAVA_CLIENT_ID,
        client_secret: STRAVA_CLIENT_SECRET,
        refresh_token: tok.refresh_token,
        grant_type: 'refresh_token',
      }),
    });
    if (!resp.ok) throw new Error('Refresh failed: ' + resp.status);
    const data = await resp.json();
    localStorage.setItem('strava_token', JSON.stringify({
      access_token: data.access_token,
      refresh_token: data.refresh_token,
      expires_at: data.expires_at,
      athlete: tok.athlete,
    }));
    return true;
  } catch (err) {
    console.error('Strava refresh error:', err);
    return false;
  }
}

function stravaDisconnect() {
  localStorage.removeItem('strava_token');
  localStorage.removeItem('lastSyncEpoch');
  localStorage.removeItem('lastSyncDate');
  localStorage.removeItem('dataSource');
}

// ═══════════════════════════════════════════════════════════════════
//  Strava API
// ═══════════════════════════════════════════════════════════════════

async function stravaEnsureToken() {
  const tok = stravaGetToken();
  if (!tok) return null;
  // Refresh if expired (with 60s buffer)
  if (tok.expires_at && tok.expires_at < (Date.now() / 1000) + 60) {
    const ok = await stravaRefreshToken();
    if (!ok) return null;
    return stravaGetToken().access_token;
  }
  return tok.access_token;
}

async function stravaFetchActivities(accessToken, afterEpoch = 0) {
  const all = [];
  let page = 1;
  const perPage = 200;
  const statusEl = document.getElementById('sync-status');

  while (true) {
    if (statusEl) statusEl.textContent = `Fetching page ${page}...`;
    const url = 'https://www.strava.com/api/v3/athlete/activities'
      + '?per_page=' + perPage
      + '&page=' + page
      + (afterEpoch ? '&after=' + afterEpoch : '');
    const resp = await fetch(url, {
      headers: { 'Authorization': 'Bearer ' + accessToken },
    });
    if (!resp.ok) throw new Error('Strava API error: ' + resp.status);
    const batch = await resp.json();
    if (!batch.length) break;
    all.push(...batch);
    if (batch.length < perPage) break;
    page++;
  }
  if (statusEl) statusEl.textContent = '';
  return all;
}

// ═══════════════════════════════════════════════════════════════════
//  Power Streams — fetch per-ride watt data, compute best powers
// ═══════════════════════════════════════════════════════════════════

const POWER_DURATIONS = (() => {
  const d = [];
  for (let i = 1; i <= 60; i++) d.push(i);
  for (let i = 65; i <= 120; i += 5) d.push(i);
  for (let i = 130; i <= 300; i += 10) d.push(i);
  for (let i = 330; i <= 600; i += 30) d.push(i);
  for (let i = 660; i <= 3600; i += 60) d.push(i);
  for (let i = 3900; i <= 18000; i += 300) d.push(i);
  return d;
})();
const BIKE_TYPE_KEYS = ['road_biking', 'mountain_biking', 'cycling', 'virtual_ride', 'indoor_cycling', 'gravel_cycling'];

// Compute best average power for each standard duration from raw watts array
function computeBestPowers(watts) {
  const result = {};
  for (const dur of POWER_DURATIONS) {
    if (watts.length < dur) { result[dur] = null; continue; }
    // Sliding window
    let sum = 0;
    for (let i = 0; i < dur; i++) sum += (watts[i] || 0);
    let best = sum;
    for (let i = dur; i < watts.length; i++) {
      sum += (watts[i] || 0) - (watts[i - dur] || 0);
      if (sum > best) best = sum;
    }
    result[dur] = Math.round(best / dur);
  }
  return result;
}

// Fetch power stream for a single activity, returns watts array or null
async function stravaFetchPowerStream(accessToken, activityId) {
  const url = `https://www.strava.com/api/v3/activities/${activityId}/streams?keys=watts&key_by_type=true`;
  const resp = await fetch(url, {
    headers: { 'Authorization': 'Bearer ' + accessToken },
  });
  if (resp.status === 429) {
    // Rate limited — return sentinel to signal caller to wait
    return 'RATE_LIMITED';
  }
  if (!resp.ok) return null;
  const data = await resp.json();
  if (data.watts && data.watts.data) return data.watts.data;
  return null;
}

// Fetch power streams for all bike rides that don't have power curve data yet.
// Respects rate limits by pacing requests and pausing when throttled.
// Updates statusEl with progress. Calls onProgress(activity) after each ride is processed.
async function syncPowerStreams(accessToken, activities, onProgress) {
  const statusEl = document.getElementById('sync-status');
  const bikeRides = activities.filter(a => {
    const atype = (a.activityType || {}).typeKey || '';
    if (!BIKE_TYPE_KEYS.includes(atype)) return false;
    if (!(a.normPower > 0 || a.avgPower > 0)) return false;
    return !a._powerSynced || !a.maxAvgPower_2;
  });

  if (!bikeRides.length) {
    if (statusEl) statusEl.textContent = '';
    return;
  }

  const total = bikeRides.length;
  let done = 0;
  let rateLimitPause = 0;

  for (const ride of bikeRides) {
    done++;
    if (statusEl) statusEl.textContent = `Fetching power data: ${done}/${total}...`;

    const watts = await stravaFetchPowerStream(accessToken, ride.activityId);

    if (watts === 'RATE_LIMITED') {
      // Wait 60 seconds and retry
      if (statusEl) statusEl.textContent = `Rate limited — waiting 60s (${done}/${total})...`;
      await new Promise(r => setTimeout(r, 60000));
      const retry = await stravaFetchPowerStream(accessToken, ride.activityId);
      if (retry && retry !== 'RATE_LIMITED') {
        const bests = computeBestPowers(retry);
        for (const dur of POWER_DURATIONS) {
          ride[`maxAvgPower_${dur}`] = bests[dur];
        }
        if (bests[1200]) ride.max20MinPower = bests[1200];
      }
    } else if (watts && watts.length) {
      const bests = computeBestPowers(watts);
      for (const dur of POWER_DURATIONS) {
        ride[`maxAvgPower_${dur}`] = bests[dur];
      }
      if (bests[1200]) ride.max20MinPower = bests[1200];
    }

    ride._powerSynced = true;
    // Save incrementally every ride so progress isn't lost
    await dbPutAll([ride]);
    if (onProgress) onProgress(ride, done, total);

    // Pace requests: ~1.5/sec stays under 100/15min comfortably
    if (done < total) await new Promise(r => setTimeout(r, 700));
  }

  if (statusEl) statusEl.textContent = '';
}

// ═══════════════════════════════════════════════════════════════════
//  Data Mapping: Strava → Garmin format
// ═══════════════════════════════════════════════════════════════════

const STRAVA_TYPE_MAP = {
  'Run': 'running',
  'TrailRun': 'trail_running',
  'VirtualRun': 'treadmill_running',
  'Ride': 'road_biking',
  'MountainBikeRide': 'mountain_biking',
  'GravelRide': 'gravel_cycling',
  'EBikeRide': 'cycling',
  'VirtualRide': 'virtual_ride',
  'Swim': 'lap_swimming',
  'Hike': 'hiking',
  'Walk': 'walking',
  'WeightTraining': 'strength_training',
  'Yoga': 'yoga',
  'Rowing': 'rowing',
  'Elliptical': 'elliptical',
  'BackcountrySki': 'backcountry_skiing',
  'AlpineSki': 'resort_skiing',
  'NordicSki': 'cross_country_skiing',
  'RockClimbing': 'rock_climbing',
  'StairStepper': 'stair_climbing',
  'Kayaking': 'kayaking',
  'Workout': 'strength_training',
  'Crossfit': 'strength_training',
  'Snowboard': 'snowboarding',
  'Snowshoe': 'snowshoeing',
  'StandUpPaddling': 'paddleboarding',
  'Surfing': 'surfing',
  'InlineSkate': 'inline_skating',
  'Skateboard': 'skateboarding',
  'Golf': 'golf',
  'Handcycle': 'cycling',
  'Velomobile': 'cycling',
  'Wheelchair': 'walking',
  'Canoeing': 'kayaking',
  'Sail': 'sailing',
  'Soccer': 'other',
  'Tennis': 'other',
  'Pickleball': 'other',
  'Racquetball': 'other',
  'Squash': 'other',
  'TableTennis': 'other',
  'IceSkate': 'ice_skating',
  'RollerSki': 'cross_country_skiing',
  'Kitesurf': 'kitesurfing',
  'Windsurf': 'windsurfing',
  'Pilates': 'yoga',
};

function mapStravaActivity(sa) {
  const typeKey = STRAVA_TYPE_MAP[sa.type] || 'other';
  // Convert ISO date "2024-01-15T10:30:00Z" to "2024-01-15 10:30:00"
  let dateStr = (sa.start_date_local || sa.start_date || '').replace('T', ' ');
  if (dateStr.endsWith('Z')) dateStr = dateStr.slice(0, -1);

  return {
    activityId: sa.id,
    activityName: sa.name || '',
    activityType: { typeKey },
    startTimeLocal: dateStr,
    duration: sa.moving_time || sa.elapsed_time || 0,
    movingDuration: sa.moving_time || 0,
    distance: sa.distance || 0,
    averageHR: sa.average_heartrate || 0,
    maxHR: sa.max_heartrate || 0,
    averageSpeed: sa.average_speed || 0,
    maxSpeed: sa.max_speed || 0,
    avgPower: sa.average_watts || 0,
    normPower: sa.weighted_average_watts || 0,
    calories: sa.calories || 0,
    elevationGain: sa.total_elevation_gain || 0,
    // Not available from Strava summary:
    // max20MinPower, maxAvgPower_X, vO2MaxValue, averageRunningCadenceInStepsPerMinute
  };
}

function mapStravaActivities(stravaActs) {
  return stravaActs.map(mapStravaActivity);
}

function detectAndMapActivities(rawArray) {
  if (!rawArray || !rawArray.length) return [];
  const first = rawArray[0];
  // Garmin format: has activityType object
  if (first.activityType && typeof first.activityType === 'object') return rawArray;
  // Strava format: has type as string
  if (typeof first.type === 'string') return mapStravaActivities(rawArray);
  // Unknown — return as-is
  return rawArray;
}

// ═══════════════════════════════════════════════════════════════════
//  Sync Orchestration
// ═══════════════════════════════════════════════════════════════════

async function syncFromStrava(fullSync = false, onPowerProgress) {
  const accessToken = await stravaEnsureToken();
  if (!accessToken) {
    stravaStartAuth();
    return null;
  }

  const afterEpoch = fullSync ? 0 : parseInt(localStorage.getItem('lastSyncEpoch') || '0');
  const raw = await stravaFetchActivities(accessToken, afterEpoch);
  const mapped = mapStravaActivities(raw);

  if (mapped.length) {
    await dbPutAll(mapped);
    // Set epoch to most recent activity start
    const latest = Math.max(...raw.map(a => new Date(a.start_date).getTime() / 1000));
    localStorage.setItem('lastSyncEpoch', String(Math.floor(latest)));
  }

  // Fetch power streams for bike rides that don't have power curve data
  const allActs = await dbGetAll();
  await syncPowerStreams(accessToken, allActs, onPowerProgress);

  setDataSource('strava');
  setLastSync();
  return dbGetAll();
}

async function loadFromImport(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = async (e) => {
      try {
        const raw = JSON.parse(e.target.result);
        const acts = detectAndMapActivities(raw);
        await dbClear();
        await dbPutAll(acts);
        setDataSource('import');
        setLastSync();
        resolve(acts);
      } catch (err) {
        reject(err);
      }
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

async function getStoredActivities() {
  try { return await dbGetAll(); }
  catch { return []; }
}

function getDataSource() { return localStorage.getItem('dataSource'); }
function setDataSource(s) { localStorage.setItem('dataSource', s); }
function getLastSync() { return localStorage.getItem('lastSyncDate'); }
function setLastSync() { localStorage.setItem('lastSyncDate', new Date().toISOString().slice(0, 10)); }
