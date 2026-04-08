# TrainingHub

An AI-powered multi-sport training dashboard that syncs with Garmin Connect and turns raw workout data into actionable insight. Built for runners, cyclists, and swimmers who want to understand their training — not just track it.

## What It Does

TrainingHub pulls your activity history from Garmin Connect, computes training metrics, and generates an interactive single-page dashboard you can open in any browser.

**Dashboard** — At-a-glance view of your current form (TSB), fitness trend (CTL), fatigue (ATL), HRV status, and AI-generated training insights with a daily suggestion.

**Sport Pages** — Dedicated pages for Running, Cycling, and Swimming with activity tables showing TSS, pace/power trends, and sport-specific stats.

**Cycling Power Curve** — Best wattage at every duration across all rides, with FTP reference line.

**Training Zones** — Auto-calculated zone tables for heart rate (Friel 7-zone), power (Coggan), and swimming pace (CSS-based). Zones update instantly when you change thresholds.

**Life Events** — Mark races, injuries, illness, surgery, and travel on your Fitness & Fatigue chart to add context to your training story.

**Learn Page** — Technical explanations of TSB, TSS, HRV, training zones, power curves, and key thresholds so you understand what the numbers mean.

**Training Insights** — Rule-based analysis engine that reads your CTL trend, TSB state, HRV baseline, and generates a daily training suggestion — the seed of the AI coach.

## Setup

### Prerequisites

- Python 3.8+
- A Garmin Connect account with workout data

### Install

```bash
git clone <repo-url>
cd garmin
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root:

```
GARMIN_EMAIL=your-garmin-email@example.com
GARMIN_PASSWORD=your-garmin-password
```

### Sync & Generate

```bash
# Pull last 90 days of activities from Garmin
python3 garmin_sync.py

# Pull more history
python3 garmin_sync.py --days 365

# Also pull sleep data
python3 garmin_sync.py --sleep

# Generate the dashboard and open in browser
python3 fitness_analysis.py --open
```

Your data is saved locally as JSON in `garmin_data/`. The dashboard is a self-contained HTML file — no server required.

### Settings

Thresholds are auto-calculated from your workout data:

| Setting | Description |
|---------|-------------|
| FTP | Functional Threshold Power (cycling) |
| LTHR Run | Lactate Threshold Heart Rate (running) |
| LTHR Bike | Lactate Threshold Heart Rate (cycling) |
| Threshold Pace | Running threshold pace per mile |
| CSS | Critical Swim Speed per 100yd |

Override any value in the Settings page — changes are saved in your browser and update all zones and charts instantly.

## Project Structure

```
garmin/
  garmin_sync.py        # Pulls activities, metrics, HRV, sleep from Garmin Connect
  fitness_analysis.py   # Computes TSB/zones/metrics, generates HTML dashboard
  serve.py              # Optional local dev server
  requirements.txt      # Python dependencies
  .env                  # Garmin credentials (not committed)
  garmin_data/          # Synced JSON data + generated dashboard (not committed)
```

## How Training Metrics Work

**TSS (Training Stress Score)** — Quantifies workout difficulty as a single number. 100 = one hour at threshold. Calculated from power (cycling), heart rate (running), or pace (swimming) relative to your personal thresholds.

**CTL (Chronic Training Load / Fitness)** — 42-day rolling average of daily TSS. Represents your accumulated fitness. Builds slowly, decays slowly.

**ATL (Acute Training Load / Fatigue)** — 7-day rolling average of daily TSS. Represents recent fatigue. Spikes after hard training, drops quickly with rest.

**TSB (Training Stress Balance / Form)** — CTL minus ATL. Tells you how ready you are to perform today. Negative = fatigued but building fitness. Positive = fresh and ready to race.

**HRV (Heart Rate Variability)** — Nervous system recovery indicator. Compared against your personal baseline range from Garmin data.

## Roadmap

- [x] Historical data dashboard with TSB/form charts
- [x] Multi-sport support (run, bike, swim)
- [x] Training zones (HR, power, pace)
- [x] Power curve analysis
- [x] Life events on charts
- [x] Training insights engine
- [x] Learn page with technical explanations
- [ ] Training plan builder — create, modify, and schedule structured plans
- [ ] AI coach — conversational training guidance powered by your real data
- [ ] Nutrition planning — load-aware, recovery-aware meal guidance
- [ ] Sports psychology — mental side of training: consistency, injury coping, motivation
- [ ] Text-based Quick Log — describe a workout in plain language and AI parses it
- [ ] .fit file upload — manual activity import
- [ ] Strava integration
- [ ] Social features — data-driven workout buddy matching by fitness level and goals
- [ ] Full web app — evolve beyond single HTML file

## Design Inspiration

- [intervals.icu](https://intervals.icu) — TSB charts, power analysis
- [TrainingPeaks](https://www.trainingpeaks.com) — training plans, CTL/ATL model
- [Strava](https://www.strava.com) — social, activity feed
