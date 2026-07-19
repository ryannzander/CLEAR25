"""Live Toronto smoke-forecast scorer — PURE PYTHON stdlib (no numpy, no torch).

Loads the frozen model exported by scripts/export_smoke_model.py
(webapp/dashboard/services/smoke_model.json) and scores P(Toronto-core median PM2.5 >=
elevated) at each trained horizon from a 24-hour window of the surrounding field + wind.

WHY pure Python (no numpy): the Vercel lambda is size-capped (vercel.json maxLambdaSize
50mb) and deliberately slim — numpy is NOT installed there, and importing it at module load
took the whole app down once. Everything here uses only the stdlib `math`, so this module
imports and runs on the serverless function with zero extra dependencies.

ISOLATION: read-only. Never imports the alert engine, never touches
CachedResult(key="latest"). It is NOT an alert — it is a forecast surface.

FEATURE PARITY: the feature construction MUST match the training code. Geometry mirrors
scripts/poc_smoke_model.py (SECTORS / haversine / bearing_sector) and the wind features
mirror scripts/wind_features.py build_features(). Those scripts aren't deployed, so the
math is duplicated here with this keep-in-sync note; verify_against_reference() + the
committed feature_order guard against drift.

Missing values are represented as None (JSON null), NOT NaN — Postgres jsonb rejects NaN,
so the rolling buffer that stores these rows must never contain NaN.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "smoke_model.json"

TOR_LAT, TOR_LON = 43.6532, -79.3832
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]   # MUST match poc_smoke_model.SECTORS
_CENTERS = [i * 45.0 for i in range(8)]                  # compass bearing of each sector


# --------------------------------------------------------------------------- #
# Model loading
# --------------------------------------------------------------------------- #
def load_model(path=MODEL_PATH):
    """Load and lightly validate the exported model JSON. Returns the dict or None.

    Weights stay as plain nested lists (no numpy). Missing/short file -> None so the
    caller can degrade gracefully."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not m.get("horizons") or not m.get("standardization"):
        return None
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
    within tor_radius. Missing sector / too-few-core -> None (never NaN). Raw values."""
    ssum = [0.0] * 8
    scnt = [0] * 8
    tor_vals = []
    for st in results:
        pm, lat, lon = st.get("pm25"), st.get("lat"), st.get("lon")
        if pm is None or lat is None or lon is None:
            continue
        try:
            pm, lat, lon = float(pm), float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if _haversine_km(TOR_LAT, TOR_LON, lat, lon) <= tor_radius:
            tor_vals.append(pm)
        else:
            j = SECTORS.index(_bearing_sector(lat, lon))
            ssum[j] += pm
            scnt[j] += 1
    sec_mean = [ssum[j] / scnt[j] if scnt[j] > 0 else None for j in range(8)]
    tor_med = float(median(tor_vals)) if len(tor_vals) >= min_tor else None
    return sec_mean, tor_med


# --------------------------------------------------------------------------- #
# Wind features — keep in sync with scripts/wind_features.build_features (single row)
# --------------------------------------------------------------------------- #
def _wind_feature_row(speed, direc, sec_mean):
    """5 causal wind features: [logspeed, dir_sin, dir_cos, upwind_pm, upwind_flux].
    Returns [None]*5 when wind is missing (imputed to the train mean downstream)."""
    if speed is None or direc is None:
        return [None] * 5
    try:
        speed, direc = float(speed), float(direc)
    except (TypeError, ValueError):
        return [None] * 5
    if math.isnan(speed) or math.isnan(direc):
        return [None] * 5
    pos_speed = max(speed, 0.0)
    rad = math.radians(direc)
    upwind = 0.0
    for c, s in zip(_CENTERS, sec_mean):
        if s is None or s < 0:
            continue
        align = max(0.0, math.cos(math.radians(direc - c)))
        upwind += align * s
    return [math.log1p(pos_speed), math.sin(rad), math.cos(rad),
            math.log1p(upwind), math.log1p(pos_speed * upwind)]


def build_feature_row(results, wind_speed, wind_dir, when, model):
    """Build the raw (pre-standardization) feature row matching training's Feat layout:
    log1p(8 sector means) + log1p(Toronto median) + hour sin/cos + 5 wind features.
    `when` is a UTC datetime. Missing entries are None (JSON-safe, imputed at score time)."""
    tor_radius = model.get("tor_radius", 25.0)
    min_tor = model.get("min_tor", 3)
    sec_mean, tor_med = build_sector_row(results, tor_radius, min_tor)
    # log1p the PM columns (sectors + Toronto), preserving None — as
    # scripts/train_smoke_lstm.hourly_features does (log1p(max(0, pm))).
    sec_log = [math.log1p(max(0.0, s)) if s is not None else None for s in sec_mean]
    tor_log = math.log1p(tor_med) if tor_med is not None else None
    hod = when.hour / 24.0 * 2 * math.pi
    row = sec_log + [tor_log, math.sin(hod), math.cos(hod)]
    row += _wind_feature_row(wind_speed, wind_dir, sec_mean)      # raw sec_mean, per training
    return row


def static_row(when):
    """Static month sin/cos fed to the head (raw, not standardized)."""
    mon = (when.month - 1) / 12.0 * 2 * math.pi
    return [math.sin(mon), math.cos(mon)]


# --------------------------------------------------------------------------- #
# Pure-Python LSTM forward (matches torch nn.LSTM, gate order i,f,g,o) + head
# --------------------------------------------------------------------------- #
def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _matvec(mat, vec):
    """mat (m x n) as list of rows, vec (n,) -> list (m,)."""
    return [sum(w * v for w, v in zip(row, vec)) for row in mat]


def _forward(window_std, static, h):
    """window_std: list of `window` rows (each F floats), standardized; static: [2];
    h: a horizon entry. Returns prob. Gate order i,f,g,o (PyTorch)."""
    Wih, Whh = h["weight_ih"], h["weight_hh"]
    bih, bhh = h["bias_ih"], h["bias_hh"]
    H = len(Whh[0])
    hid = [0.0] * H
    cell = [0.0] * H
    for x in window_std:
        gih = _matvec(Wih, x)
        ghh = _matvec(Whh, hid)
        g = [gih[k] + bih[k] + ghh[k] + bhh[k] for k in range(4 * H)]
        for j in range(H):
            i = _sigmoid(g[j])
            f = _sigmoid(g[H + j])
            gg = math.tanh(g[2 * H + j])
            o = _sigmoid(g[3 * H + j])
            cell[j] = f * cell[j] + i * gg
            hid[j] = o * math.tanh(cell[j])
    feat = hid + list(static)                              # (H + 2,)
    z0 = [max(0.0, v + b) for v, b in zip(_matvec(h["head0_w"], feat), h["head0_b"])]  # ReLU
    logit = _matvec(h["head1_w"], z0)[0] + h["head1_b"][0]
    return _sigmoid(logit)


def _standardize(rows, mu, sd):
    """Replicate scripts/train_smoke_lstm.standardize_train: None/NaN -> train mean
    (=> 0 after centering), else (v-mu)/sd clipped to +/-8."""
    out = []
    for row in rows:
        z = []
        for j, v in enumerate(row):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                z.append(0.0)
            else:
                zj = (v - mu[j]) / sd[j]
                z.append(-8.0 if zj < -8.0 else 8.0 if zj > 8.0 else zj)
        out.append(z)
    return out


def score(buffer_rows, when, model):
    """buffer_rows: list of >= window raw feature rows (oldest..newest). Returns
    {horizon_str: prob} using the last `window` rows. `when` sets the static month."""
    window = int(model.get("window", 24))
    rows = buffer_rows[-window:]
    if len(rows) < window:
        raise ValueError(f"need {window} rows, have {len(rows)}")
    mu, sd = model["standardization"]["mu"], model["standardization"]["sd"]
    win_std = _standardize(rows, mu, sd)
    static = static_row(when)
    return {hk: _forward(win_std, static, hv) for hk, hv in model["horizons"].items()}


# --------------------------------------------------------------------------- #
# Verification gate — pure-Python forward must reproduce the torch reference
# --------------------------------------------------------------------------- #
def verify_against_reference(model, tol=1e-5):
    """For each horizon, run the forward on the stored (already-standardized) torch
    reference window and compare to the stored torch probability. (ok, details)."""
    ok = True
    details = {}
    for hk, hv in model["horizons"].items():
        got = _forward(hv["ref_X"], hv["ref_S"], hv)
        ref = float(hv["ref_prob"])
        diff = abs(got - ref)
        details[hk] = {"python": got, "torch": ref, "abs_diff": diff}
        ok = ok and (diff < tol)
    return ok, details


if __name__ == "__main__":
    m = load_model()
    if not m:
        raise SystemExit(f"No model at {MODEL_PATH} — run scripts/export_smoke_model.py first.")
    ok, det = verify_against_reference(m)
    for hk, d in sorted(det.items(), key=lambda kv: int(kv[0])):
        print(f"  H={hk:>2}: python={d['python']:.8f}  torch={d['torch']:.8f}  |diff|={d['abs_diff']:.2e}")
    print("VERIFY:", "PASS — pure-Python matches torch" if ok else "FAIL — mismatch > tol")
    raise SystemExit(0 if ok else 1)
