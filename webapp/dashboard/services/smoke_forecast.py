"""Live Toronto smoke-forecast scorer — pure numpy, no PyTorch (runs on Vercel).

Loads the frozen model exported by scripts/export_smoke_model.py
(webapp/dashboard/services/smoke_model.json) and scores P(Toronto-core median PM2.5 >=
elevated) at each trained horizon from a 24-hour window of the surrounding field + wind.

ISOLATION: read-only. Never imports the alert engine, never touches
CachedResult(key="latest"). It is NOT an alert — it is a forecast surface.

FEATURE PARITY: the feature construction below MUST match the training code exactly.
The geometry mirrors scripts/poc_smoke_model.py (SECTORS / haversine / bearing_sector)
and the wind features mirror scripts/wind_features.py build_features(). Those scripts are
not deployed to Vercel, so the math is duplicated here with this keep-in-sync note; the
verify_against_reference() gate + the committed feature_order guard against drift.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "smoke_model.json"

TOR_LAT, TOR_LON = 43.6532, -79.3832
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]   # MUST match poc_smoke_model.SECTORS
_CENTERS = np.array([i * 45.0 for i in range(8)])         # compass bearing of each sector


# --------------------------------------------------------------------------- #
# Model loading
# --------------------------------------------------------------------------- #
def load_model(path=MODEL_PATH):
    """Load and lightly validate the exported model JSON. Returns the dict or None."""
    p = Path(path)
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    if not m.get("horizons") or not m.get("standardization"):
        return None
    # pre-convert standardization + per-horizon weights to numpy for speed
    m["_mu"] = np.asarray(m["standardization"]["mu"], dtype="float64")
    m["_sd"] = np.asarray(m["standardization"]["sd"], dtype="float64")
    for h in m["horizons"].values():
        for k in ("weight_ih", "weight_hh", "bias_ih", "bias_hh",
                  "head0_w", "head0_b", "head1_w", "head1_b"):
            h["_" + k] = np.asarray(h[k], dtype="float64")
    return m


# --------------------------------------------------------------------------- #
# Geometry — keep in sync with scripts/poc_smoke_model.py
# --------------------------------------------------------------------------- #
def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _bearing_sector(lat, lon):
    """8-point compass sector of a point as seen FROM Toronto."""
    p = math.pi / 180.0
    dlon = (lon - TOR_LON) * p
    y = math.sin(dlon) * math.cos(lat * p)
    x = (math.cos(TOR_LAT * p) * math.sin(lat * p)
         - math.sin(TOR_LAT * p) * math.cos(lat * p) * math.cos(dlon))
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return SECTORS[int((brg + 22.5) % 360.0 // 45.0)]


def build_sector_row(results, tor_radius, min_tor):
    """From live station dicts ({lat,lon,pm25}) build (sec_mean[8], tor_med).

    Mirrors scripts/poc_smoke_model.build_series aggregation: sector = MEAN PM of
    non-core stations in that compass sector; Toronto core = MEDIAN PM of stations
    within tor_radius (NaN if fewer than min_tor report). Raw (un-logged) values.
    """
    ssum = np.zeros(8)
    scnt = np.zeros(8)
    tor_vals = []
    for st in results:
        pm = st.get("pm25")
        lat = st.get("lat")
        lon = st.get("lon")
        if pm is None or lat is None or lon is None:
            continue
        try:
            pm = float(pm); lat = float(lat); lon = float(lon)
        except (TypeError, ValueError):
            continue
        if _haversine_km(TOR_LAT, TOR_LON, lat, lon) <= tor_radius:
            tor_vals.append(pm)
        else:
            j = SECTORS.index(_bearing_sector(lat, lon))
            ssum[j] += pm
            scnt[j] += 1
    with np.errstate(invalid="ignore"):
        sec_mean = np.where(scnt > 0, ssum / np.maximum(scnt, 1), np.nan)
    tor_med = float(np.median(tor_vals)) if len(tor_vals) >= min_tor else np.nan
    return sec_mean, tor_med


# --------------------------------------------------------------------------- #
# Wind features — keep in sync with scripts/wind_features.build_features (single row)
# --------------------------------------------------------------------------- #
def _wind_feature_row(speed, direc, sec_mean):
    """5 causal wind features for one hour: [logspeed, dir_sin, dir_cos, upwind_pm,
    upwind_flux]. NaN row when wind is missing (imputed to the train mean downstream)."""
    if speed is None or direc is None:
        return [np.nan] * 5
    try:
        speed = float(speed); direc = float(direc)
    except (TypeError, ValueError):
        return [np.nan] * 5
    if math.isnan(speed) or math.isnan(direc):
        return [np.nan] * 5
    pos_speed = max(speed, 0.0)
    logspeed = math.log1p(pos_speed)
    rad = math.radians(direc)
    align = np.maximum(0.0, np.cos(np.radians(direc - _CENTERS)))     # (8,) >= 0
    pm = np.where((sec_mean < 0) | np.isnan(sec_mean), 0.0, sec_mean)
    upwind = float(np.sum(align * pm))
    return [logspeed, math.sin(rad), math.cos(rad),
            math.log1p(upwind), math.log1p(pos_speed * upwind)]


def build_feature_row(results, wind_speed, wind_dir, when, model):
    """Build the raw (pre-standardization) feature row matching training's Feat layout:
    log1p(8 sector means) + log1p(Toronto median) + hour sin/cos + 5 wind features.
    `when` is a UTC datetime. NaN entries are fine — score() imputes them to the mean."""
    tor_radius = model.get("tor_radius", 25.0)
    min_tor = model.get("min_tor", 3)
    sec_mean, tor_med = build_sector_row(results, tor_radius, min_tor)
    # log1p the PM columns (sectors + Toronto), preserving NaN — exactly as
    # scripts/train_smoke_lstm.hourly_features does.
    with np.errstate(invalid="ignore"):
        sec_log = np.log1p(np.where(sec_mean < 0, 0.0, sec_mean))
    tor_log = math.log1p(tor_med) if not math.isnan(tor_med) else np.nan
    hod = when.hour / 24.0 * 2 * math.pi
    row = list(sec_log) + [tor_log, math.sin(hod), math.cos(hod)]
    row += _wind_feature_row(wind_speed, wind_dir, sec_mean)      # raw sec_mean, per training
    return row


def static_row(when):
    """Static month sin/cos fed to the head (raw, not standardized)."""
    mon = (when.month - 1) / 12.0 * 2 * math.pi
    return np.array([math.sin(mon), math.cos(mon)], dtype="float64")


# --------------------------------------------------------------------------- #
# numpy LSTM forward (matches torch nn.LSTM, gate order i,f,g,o) + head
# --------------------------------------------------------------------------- #
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def _forward(window_std, static, h):
    """window_std: (W, F) standardized; static: (2,); h: a horizon entry. Returns prob."""
    Wih, Whh = h["_weight_ih"], h["_weight_hh"]
    bih, bhh = h["_bias_ih"], h["_bias_hh"]
    H = Whh.shape[1]
    hid = np.zeros(H)
    cell = np.zeros(H)
    for x in window_std:
        g = Wih @ x + bih + Whh @ hid + bhh          # (4H,)
        i = _sigmoid(g[0:H]); f = _sigmoid(g[H:2 * H])
        gg = np.tanh(g[2 * H:3 * H]); o = _sigmoid(g[3 * H:4 * H])
        cell = f * cell + i * gg
        hid = o * np.tanh(cell)
    feat = np.concatenate([hid, static])             # (H + 2,)
    z0 = np.maximum(0.0, h["_head0_w"] @ feat + h["_head0_b"])   # ReLU
    logit = float((h["_head1_w"] @ z0 + h["_head1_b"])[0])       # head1_w is (1, 32)
    return float(_sigmoid(logit))


def _standardize(rows, mu, sd):
    """Replicate scripts/train_smoke_lstm.standardize_train: NaN->mean, /sd, clip +/-8."""
    F = np.asarray(rows, dtype="float64")
    Z = (np.where(np.isnan(F), mu, F) - mu) / sd
    return np.clip(np.nan_to_num(Z, nan=0.0), -8.0, 8.0)


def score(buffer_rows, when, model):
    """buffer_rows: list of >= window raw feature rows (oldest..newest). Returns
    {horizon_str: prob} using the last `window` rows. `when` sets the static month."""
    window = int(model.get("window", 24))
    rows = buffer_rows[-window:]
    if len(rows) < window:
        raise ValueError(f"need {window} rows, have {len(rows)}")
    win_std = _standardize(rows, model["_mu"], model["_sd"])
    static = static_row(when)
    return {hk: _forward(win_std, static, hv) for hk, hv in model["horizons"].items()}


# --------------------------------------------------------------------------- #
# Verification gate — numpy forward must reproduce the torch reference to < tol
# --------------------------------------------------------------------------- #
def verify_against_reference(model, tol=1e-5):
    """For each horizon, run the numpy forward on the stored (already-standardized)
    torch reference window and compare to the stored torch probability.
    Returns (ok, details)."""
    ok = True
    details = {}
    for hk, hv in model["horizons"].items():
        win = np.asarray(hv["ref_X"], dtype="float64")
        static = np.asarray(hv["ref_S"], dtype="float64")
        got = _forward(win, static, hv)
        ref = float(hv["ref_prob"])
        diff = abs(got - ref)
        details[hk] = {"numpy": got, "torch": ref, "abs_diff": diff}
        ok = ok and (diff < tol)
    return ok, details


if __name__ == "__main__":
    m = load_model()
    if not m:
        raise SystemExit(f"No model at {MODEL_PATH} — run scripts/export_smoke_model.py first.")
    ok, det = verify_against_reference(m)
    for hk, d in sorted(det.items(), key=lambda kv: int(kv[0])):
        print(f"  H={hk:>2}: numpy={d['numpy']:.8f}  torch={d['torch']:.8f}  |diff|={d['abs_diff']:.2e}")
    print("VERIFY:", "PASS — numpy matches torch" if ok else "FAIL — mismatch > tol")
    raise SystemExit(0 if ok else 1)
