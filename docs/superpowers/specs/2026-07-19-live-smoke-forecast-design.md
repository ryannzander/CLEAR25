# Live Toronto Smoke Forecast — Design Spec

**Date:** 2026-07-19
**Branch:** `feature/live-smoke-forecast`
**Status:** design, pending implementation

## Goal

Surface a **wind-aware Toronto wildfire-smoke forecast** on the live site, driven by the
live surrounding PM2.5 field + the live ECCC wind that was just deployed. For each lead
time **H ∈ {6, 12, 24, 48} h**, report the model's probability that the Toronto-core
median PM2.5 will be **elevated (≥ 35 µg/m³)** H hours from now.

This deploys the validated research model (`scripts/train_smoke_lstm.py` NAPS LSTM +wind,
test ROC-AUC ≈ 0.95, beats persistence at every lead) as a **read-only** product surface.

## Non-goals (explicit isolation)

- **NOT an alert.** It never feeds `evaluate.py`, the 3-rule engine, or
  `CachedResult(key="latest")`. Fusing model output into alerts stays deferred pending
  backtest — a separate methodology decision.
- No change to the existing `/api/refresh/` flow or `core.py` refresh/alert code.
- No PyTorch in production (it does not fit the slim Vercel lambda).

## Constraints discovered

1. **No stored history.** `CachedResult(key="latest")` and `ReadingSnapshot` are
   `update_or_create` (latest only). The LSTM needs the last **24 h** of the sector field.
   → We add a small rolling buffer and warm up ~24 h (see Warm-up).
2. **Torch ∉ Vercel.** → the LSTM forward pass is re-expressed in **pure numpy** from
   exported weights, and verified to reproduce torch to < 1e-5 before we trust it.
3. **Live field is available.** `GET /api/live/` (= `CachedResult(key="latest").results`)
   returns 184 CLEAR-network stations with `lat/lon/pm25/target_city` — enough to build
   the 8 compass sectors + Toronto-core (≤ 25 km) median exactly like the training
   `build_series()`.
4. **Live wind is available.** `GET /api/eccc/?kind=wind_analysis` gives current 10 m
   `wind_speed`/`wind_dir` at `CITY:Toronto` (deg-from, same convention as training).

## Architecture

Five isolated units. Data flows **live field + live wind → hourly feature row → 24-row
buffer → numpy LSTM → forecast**, all triggered by a dedicated cron endpoint that only
*reads* `key="latest"`.

### 1. Frozen model asset — `scripts/export_smoke_model.py`
Trains one NAPS LSTM (+wind) **per horizon** and exports all of them to a single committed
JSON keyed by horizon (`webapp/dashboard/services/smoke_model.json`, ~60 KB × 4 ≈ 240 KB):
- Per horizon: LSTM weights (`weight_ih`, `weight_hh`, `bias_ih`, `bias_hh` — the 4 gates
  stacked, exactly as PyTorch lays them out), head MLP weights, and metadata.
- Shared: per-**window-feature** standardization μ/σ (the 16 window features only — the
  static month sin/cos fed to the head is raw, in [-1, 1], not standardized), the exact
  feature order, `window=24`, `elevated=35`, `tor_radius=25`, `min_tor=3`.
Weights are float32 lists (stdlib JSON). Regenerate note documented in the file.

Feature dims (must match `train_smoke_lstm.py` exactly): the LSTM ingests a **(24, 16)**
window = 8 sector means + Toronto median + hour sin/cos (11) **+ 5 wind features**; the head
also takes a **static (2,)** month sin/cos vector, derived from the score timestamp.

### 2. numpy scorer — `webapp/dashboard/services/smoke_forecast.py`
Pure-numpy (already a Vercel dep) LSTM forward pass + head, one per horizon. Consumes a
`(24, F)` standardized feature window, returns `P(elevated)`. Includes:
- `build_feature_row(live_results, wind_now)` → the current hour's raw feature row
  (8 sector means + Toronto median + hour sin/cos + 5 wind features), reusing the SAME
  sector/haversine geometry and `wind_features.build_features` math as training (imported
  or duplicated-with-sync-note; numpy-only, no torch).
- `score(buffer_rows, month_now, asset)` → `{horizon: prob}`; standardize the 24×16 window
  with the asset's μ/σ, pass the raw static month vector to each horizon's head.
- A self-test / verification hook comparing against a saved torch reference vector.

### 3. Rolling buffer — `CachedResult(key="smoke_forecast_buffer")`
JSON: `{"rows": [[raw feature row], ...], "hours_utc": [...]}`, capped at the last
**24 hourly rows**. One row appended per hourly cron fire (dedup by UTC hour so a jittered
fire is idempotent). No new DB model/migration — reuses `CachedResult`.

### 4. Scoring endpoint — `POST /api/refresh/forecast/` (CRON_SECRET-gated, CSRF-exempt)
Mirrors the existing external-cron endpoints. Steps, all guarded:
1. Read `CachedResult(key="latest").results` (read-only) + the stored `eccc_wind_analysis`.
2. Build the current feature row; append to the buffer (dedup by hour); prune to 24.
3. If ≥ 24 rows: run the numpy scorer → store
   `CachedResult(key="smoke_forecast")` = `{horizons:{H:prob}, run, updated, warming_up:false}`.
   Else store `{warming_up:true, have:N, need:24}`.
Errors are contained — this endpoint is fully independent of `/api/refresh/`.

### 5. Read endpoint + UI
- `GET /api/forecast/` (public, read-only) returns the stored `smoke_forecast`.
- Dashboard **"Toronto Smoke Forecast"** card: four horizon probabilities as labelled
  bars, a timestamp, the "warming up (N/24 h)" state, and a one-line caveat.

## Data contracts

```
GET /api/forecast/  →
  { "updated": ISO, "run": "<wind run stamp>", "warming_up": false,
    "horizons": { "6": 0.03, "12": 0.11, "24": 0.22, "48": 0.30 },
    "elevated_threshold": 35, "note": "read-only forecast, not an alert" }
  (warming up) → { "warming_up": true, "have": N, "need": 24, "updated": ISO }
```

## Isolation & safety

- Only NEW `CachedResult` keys (`smoke_forecast`, `smoke_forecast_buffer`); `key="latest"`
  is read-only from this feature's perspective.
- The scoring endpoint is separate from `/api/refresh/`; a failure here cannot affect the
  live map or alerts.
- No torch, no new heavy deps on Vercel (numpy only). No new external hosts → no CSP change.
- Cron: an **external** hourly cron (cron-job.org / UptimeRobot) hits
  `/api/refresh/forecast/` with the `CRON_SECRET` bearer — no GitHub Actions minutes
  (per the private-repo budget lesson).

## Warm-up (decision)

Accumulate forward with an honest **"warming up (N/24 h)"** state; real probabilities
appear after ~24 h. **Backfill is deferred** — past ECCC analyses aren't stored, so a true
backfill isn't cheaply available; not worth blocking the MVP.

## Verification plan

- numpy scorer reproduces the torch model's probabilities to < 1e-5 on a saved reference
  window (gate: if it fails, stop and fall back to CI scoring).
- `build_feature_row` reproduces training `build_series` sector values on a fixed synthetic
  station set (unit test).
- Buffer dedup/prune unit-tested; endpoint POST→store→GET round-trip tested with the Django
  client (like the eccc wind test); `manage.py check` clean.
- Domain-shift caveat surfaced in the UI (trained on NAPS reference stations, served on the
  WAQI CLEAR-network field).

## Risks

- **Domain shift** (NAPS training vs WAQI live field) may bias probabilities; acceptable for
  a read-only forecast, flagged in-UI. A future recalibration is a follow-up.
- **Sparse/again-missing sectors live**: handled by the training standardizer's NaN→mean
  imputation, carried into the numpy port.

## Staged build order (for the implementation plan)

1. `export_smoke_model.py` + committed `smoke_model.json` (+ torch reference vector).
2. numpy scorer + verification vs torch (**gate**).
3. buffer + scoring endpoint (`/api/refresh/forecast/`) + read endpoint (`/api/forecast/`).
4. dashboard card.
5. external-cron doc + wire-up note.
