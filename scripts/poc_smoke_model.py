#!/usr/bin/env python3
"""PROOF OF CONCEPT — can an AI model give early warning that wildfire smoke will
hit Toronto, from the surrounding PurpleAir field alone?

ISOLATION: standalone research script. Reads ONLY the committed static asset
`plume_2024_2025.json.gz` (a faithful compile of data/PurpleAir2024+2025). It does
NOT touch evaluate.py, the alert engine, CachedResult, or the live tracker.

Task (binary early warning):
    Given the PM2.5 field at time t, predict whether the Toronto-core median
    PM2.5 will be ELEVATED (>= 35 ug/m3) H hours later (default H = 12).

Features (all available at or before t — no look-ahead):
    - 8 compass-sector upwind aggregates (mean PM of sensors in each sector around
      Toronto), at lags 0 / -6 / -12 h  -> captures smoke building upwind & its drift
    - Toronto-core median at lags 0 / -6 / -12 h                (persistence signal)
    - hour-of-day and month as sin/cos                          (seasonality)

Model: a small multilayer perceptron (MLP) implemented from scratch in numpy
(He init, ReLU, sigmoid output, class-weighted binary cross-entropy, Adam). No
third-party ML libs. Compared against logistic regression (same features) and two
trivial baselines (persistence, upwind-max-now).

Split: CHRONOLOGICAL. Train on the earlier part of the timeline, test on the later
part (which contains the big June–Aug 2025 wildfire-smoke events). This is the
honest, hard test: can a model that mostly saw clean / minor-winter air flag a
real summer smoke intrusion in advance? Metrics reported: ROC-AUC (threshold-free)
plus precision/recall/F1 at a chosen operating point, with train/test class
balance shown so nothing is hidden.

Uncorrected ATM PM2.5 (the asset has no CF=1/RH), so 35 is a nominal threshold, not
a calibrated regulatory one. This is a feasibility demo, not a validated model.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

# All-NaN sectors (e.g. SE over Lake Ontario) trigger cosmetic empty-slice
# warnings from numpy's nan-aware reducers; we handle those columns explicitly.
warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", message="Degrees of freedom <= 0")


def sigmoid(z):
    """Overflow-safe logistic sigmoid."""
    out = np.empty_like(z, dtype=float)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ASSET = REPO / "webapp" / "dashboard" / "static" / "dashboard" / "plume_2024_2025.json.gz"

TOR_LAT, TOR_LON = 43.6532, -79.3832
TOR_RADIUS_KM = 25.0     # "Toronto core" sensors used to define the label
MIN_TOR_SENSORS = 3      # need >= this many reporting to trust the hourly median
ELEVATED = 35.0          # ug/m3 -> "smoke/elevated" label threshold
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

RNG = np.random.default_rng(1234)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p = math.pi / 180.0
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_sector(lat, lon):
    """8-point compass sector of a sensor as seen FROM Toronto."""
    p = math.pi / 180.0
    dlon = (lon - TOR_LON) * p
    y = math.sin(dlon) * math.cos(lat * p)
    x = (math.cos(TOR_LAT * p) * math.sin(lat * p)
         - math.sin(TOR_LAT * p) * math.cos(lat * p) * math.cos(dlon))
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return SECTORS[int((brg + 22.5) % 360.0 // 45.0)]


# --------------------------------------------------------------------------- #
# Data loading & feature engineering
# --------------------------------------------------------------------------- #
def load_asset():
    d = json.loads(gzip.open(ASSET).read())
    return d


def build_series(d, tor_radius=TOR_RADIUS_KM, min_tor=MIN_TOR_SENSORS):
    """Return (toronto_median[h], sector_mean[h, sector]) as numpy arrays with NaN
    where there is no data, plus the number of Toronto/other sensors used.

    tor_radius / min_tor let a sparser network (e.g. NAPS) use a wider Toronto
    radius and a lower minimum-station count than the dense PurpleAir defaults."""
    stations, series, n = d["stations"], d["series"], d["n_steps"]

    tor_ids, sector_of = [], {}
    for i, s in enumerate(stations):
        if haversine_km(TOR_LAT, TOR_LON, s["lat"], s["lon"]) <= tor_radius:
            tor_ids.append(i)
        else:
            sector_of[i] = bearing_sector(s["lat"], s["lon"])

    # Toronto: collect all readings per hour -> median.
    tor_vals = [[] for _ in range(n)]
    for i in tor_ids:
        a = series[i]
        for k in range(0, len(a), 2):
            tor_vals[a[k]].append(a[k + 1])
    tor_med = np.full(n, np.nan)
    for h, v in enumerate(tor_vals):
        if len(v) >= min_tor:
            tor_med[h] = float(np.median(v))

    # Sectors: running sum + count per (sector, hour) -> mean.
    sidx = {name: j for j, name in enumerate(SECTORS)}
    ssum = np.zeros((n, len(SECTORS)))
    scnt = np.zeros((n, len(SECTORS)))
    for i, sec in sector_of.items():
        j = sidx[sec]
        a = series[i]
        for k in range(0, len(a), 2):
            h = a[k]
            ssum[h, j] += a[k + 1]
            scnt[h, j] += 1
    with np.errstate(invalid="ignore"):
        sec_mean = np.where(scnt > 0, ssum / np.maximum(scnt, 1), np.nan)

    sec_counts = {name: 0 for name in SECTORS}
    for sec in sector_of.values():
        sec_counts[sec] += 1
    return tor_med, sec_mean, len(tor_ids), len(sector_of), sec_counts


def make_dataset(tor_med, sec_mean, horizon, lags=(0, 6, 12), t0=None, wind_hourly=None):
    """Build X, y, and the hour index t for every valid sample.

    t0 anchors the seasonality clock to the asset's real start (defaults to
    2024-01-01, the PurpleAir asset). wind_hourly, if given, is a per-hour [n, k]
    matrix of CAUSAL wind features (scripts/wind_features.py) appended AFTER the
    seasonality columns, so main()'s log1p on the PM block never touches them."""
    n = len(tor_med)
    if t0 is None:
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows_X, rows_y, rows_t = [], [], []
    max_lag = max(lags)
    for t in range(max_lag, n - horizon):
        ty = t + horizon
        if np.isnan(tor_med[ty]):
            continue
        if any(np.isnan(tor_med[t - lag]) for lag in lags):
            continue  # need the Toronto persistence features present
        feat = []
        for lag in lags:                      # 8 sectors x len(lags)
            feat.extend(sec_mean[t - lag].tolist())
        for lag in lags:                      # Toronto median x len(lags)
            feat.append(tor_med[t - lag])
        dt = t0 + timedelta(hours=t)          # seasonality
        hod = dt.hour / 24.0 * 2 * math.pi
        mon = (dt.month - 1) / 12.0 * 2 * math.pi
        feat += [math.sin(hod), math.cos(hod), math.sin(mon), math.cos(mon)]
        if wind_hourly is not None:           # causal wind features at time t
            feat.extend(wind_hourly[t].tolist())
        rows_X.append(feat)
        rows_y.append(1.0 if tor_med[ty] >= ELEVATED else 0.0)
        rows_t.append(t)
    X = np.array(rows_X, dtype=float)
    y = np.array(rows_y, dtype=float)
    tt = np.array(rows_t, dtype=int)
    n_sector_feats = len(SECTORS) * len(lags)  # first columns are the upwind sectors
    return X, y, tt, n_sector_feats


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def roc_auc(y, score):
    """Rank-based AUC (Mann–Whitney U). NaN if a class is empty."""
    y = np.asarray(y); score = np.asarray(score)
    pos = y == 1; neg = y == 0
    npos, nneg = pos.sum(), neg.sum()
    if npos == 0 or nneg == 0:
        return float("nan")
    order = np.argsort(score, kind="mergesort")
    ranks = np.empty(len(score)); ranks[order] = np.arange(1, len(score) + 1)
    # average ranks for ties
    s_sorted = score[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    return (ranks[pos].sum() - npos * (npos + 1) / 2.0) / (npos * nneg)


def prf_at(y, score, thr):
    pred = (np.asarray(score) >= thr).astype(int)
    y = np.asarray(y).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=prec, recall=rec, f1=f1)


# --------------------------------------------------------------------------- #
# Models (pure numpy)
# --------------------------------------------------------------------------- #
def _bce_weight(y):
    pos = max(1, int(y.sum())); neg = max(1, int(len(y) - y.sum()))
    return neg / pos  # weight applied to positive class


class MLP:
    """Small MLP: [d, 32, 16, 1], ReLU + sigmoid, class-weighted BCE, Adam."""

    def __init__(self, d, hidden=(16, 8), lr=1e-3, epochs=200, batch=256, pos_w=1.0, wd=1e-4):
        self.lr, self.epochs, self.batch, self.pos_w, self.wd = lr, epochs, batch, pos_w, wd
        sizes = [d, *hidden, 1]
        self.W, self.b = [], []
        for a, c in zip(sizes[:-1], sizes[1:]):
            self.W.append(RNG.standard_normal((a, c)) * math.sqrt(2.0 / a))
            self.b.append(np.zeros(c))
        self._init_adam()

    def _init_adam(self):
        self.mW = [np.zeros_like(w) for w in self.W]
        self.vW = [np.zeros_like(w) for w in self.W]
        self.mb = [np.zeros_like(b) for b in self.b]
        self.vb = [np.zeros_like(b) for b in self.b]
        self.t = 0

    def _forward(self, X):
        acts, pre = [X], []
        h = X
        for li in range(len(self.W)):
            z = h @ self.W[li] + self.b[li]
            pre.append(z)
            h = np.maximum(0, z) if li < len(self.W) - 1 else sigmoid(z)
            acts.append(h)
        return acts, pre

    def fit(self, X, y):
        y = y.reshape(-1, 1)
        w = np.where(y == 1, self.pos_w, 1.0)
        b1, b2, eps = 0.9, 0.999, 1e-8
        nidx = len(X)
        for _ in range(self.epochs):
            perm = RNG.permutation(nidx)
            for s in range(0, nidx, self.batch):
                bi = perm[s:s + self.batch]
                Xb, yb, wb = X[bi], y[bi], w[bi]
                acts, _ = self._forward(Xb)
                p = np.clip(acts[-1], 1e-7, 1 - 1e-7)
                g = wb * (p - yb) / len(bi)          # dL/dz_out (weighted BCE)
                gW, gb = [None] * len(self.W), [None] * len(self.W)
                for li in reversed(range(len(self.W))):
                    gW[li] = acts[li].T @ g
                    gb[li] = g.sum(axis=0)
                    if li > 0:
                        g = (g @ self.W[li].T) * (acts[li] > 0)
                self.t += 1
                for M, V, grad, P, decay in ((self.mW, self.vW, gW, self.W, True),
                                             (self.mb, self.vb, gb, self.b, False)):
                    for li in range(len(self.W)):
                        M[li] = b1 * M[li] + (1 - b1) * grad[li]
                        V[li] = b2 * V[li] + (1 - b2) * grad[li] ** 2
                        mhat = M[li] / (1 - b1 ** self.t)
                        vhat = V[li] / (1 - b2 ** self.t)
                        P[li] -= self.lr * mhat / (np.sqrt(vhat) + eps)
                        if decay:  # decoupled weight decay (AdamW), weights only
                            P[li] -= self.lr * self.wd * P[li]
        return self

    def predict_proba(self, X):
        return self._forward(X)[0][-1].ravel()


class Logistic:
    """L2-regularized logistic regression, class-weighted, full-batch gradient."""

    def __init__(self, d, lr=0.1, epochs=800, l2=1e-3, pos_w=1.0):
        self.w = np.zeros(d); self.b = 0.0
        self.lr, self.epochs, self.l2, self.pos_w = lr, epochs, l2, pos_w

    def fit(self, X, y):
        wt = np.where(y == 1, self.pos_w, 1.0)
        for _ in range(self.epochs):
            p = sigmoid(X @ self.w + self.b)
            g = wt * (p - y)
            self.w -= self.lr * (X.T @ g / len(X) + self.l2 * self.w)
            self.b -= self.lr * (g.mean())
        return self

    def predict_proba(self, X):
        return sigmoid(X @ self.w + self.b)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def standardize(train, *others):
    # Robust to columns that are entirely NaN in the training slice (e.g. a
    # compass sector with no sensors): fall back to mean 0 / sd 1 and zero-fill.
    with np.errstate(invalid="ignore", divide="ignore"):
        mu = np.nanmean(train, axis=0)
        sd = np.nanstd(train, axis=0)
    mu = np.where(np.isfinite(mu), mu, 0.0)
    sd = np.where(np.isfinite(sd) & (sd > 0), sd, 1.0)
    out = []
    for M in (train, *others):
        Z = (np.where(np.isnan(M), mu, M) - mu) / sd  # NaN -> train mean (=0 after)
        out.append(np.nan_to_num(Z, nan=0.0))
    return out


def _parse_asset_t0(d):
    """Anchor the timeline to the asset's real start; fall back to the PurpleAir t0."""
    s = d.get("t0")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            continue
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=12, help="early-warning lead in hours")
    ap.add_argument("--test-frac", type=float, default=0.35, help="chronological test tail fraction")
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--wind", default=None,
                    help="optional wind asset (scripts/gen_wind_hourly.py) — adds wind "
                         "features and a with/without comparison")
    args = ap.parse_args(argv)

    print(f"Loading {ASSET.name} ...")
    d = load_asset()
    tor_med, sec_mean, n_tor, n_other, sec_counts = build_series(d)
    cov = np.isfinite(tor_med).mean()
    print(f"  Toronto-core sensors (<= {TOR_RADIUS_KM:.0f} km): {n_tor}   other sensors: {n_other}")
    print(f"  sensors per upwind sector: " + "  ".join(f"{k}={v}" for k, v in sec_counts.items()))
    print(f"  hours with a usable Toronto median: {np.isfinite(tor_med).sum():,} ({cov*100:.0f}%)")

    t0 = _parse_asset_t0(d)
    wind_hourly = None
    if args.wind:
        from wind_features import load_wind, series_for, build_features
        w = load_wind(args.wind)
        wspd, wdir = series_for(w, t0, len(tor_med), TOR_LAT, TOR_LON)
        wind_hourly, wind_names = build_features(wspd, wdir, sec_mean)
        print(f"  wind: {Path(args.wind).name} — {len(w['points'])} point(s), "
              f"{np.isfinite(wspd).mean()*100:.0f}% hours covered, +{len(wind_names)} features")

    X, y, tt, n_sec = make_dataset(tor_med, sec_mean, args.horizon,
                                   t0=t0, wind_hourly=wind_hourly)
    # PM is heavy-tailed (values to ~1000); log1p tames outliers before scaling.
    # Columns [0 : n_sec+3] are PM (sectors + Toronto medians); the rest are sin/cos.
    X[:, :n_sec + 3] = np.log1p(np.where(X[:, :n_sec + 3] < 0, 0.0, X[:, :n_sec + 3]))
    print(f"\nTask: predict Toronto median PM2.5 >= {ELEVATED:.0f} ug/m3, "
          f"{args.horizon} h ahead.")
    print(f"  samples: {len(X):,}   features: {X.shape[1]}   "
          f"positives: {int(y.sum()):,} ({y.mean()*100:.1f}%)")

    # Chronological split by hour index.
    order = np.argsort(tt, kind="mergesort")
    X, y, tt = X[order], y[order], tt[order]
    cut = int(len(X) * (1 - args.test_frac))
    split_hour = tt[cut]
    print(f"  split @ hour {split_hour} ({(t0+timedelta(hours=int(split_hour))):%Y-%m-%d})  "
          f"train={cut:,}  test={len(X)-cut:,}")
    Xtr, ytr = X[:cut], y[:cut]
    Xte, yte = X[cut:], y[cut:]
    print(f"  train positives: {int(ytr.sum()):,} ({ytr.mean()*100:.1f}%)   "
          f"test positives: {int(yte.sum()):,} ({yte.mean()*100:.1f}%)")
    if ytr.sum() == 0 or yte.sum() == 0:
        print("\n!! One split has no positive events — adjust --horizon/--test-frac.")
        return 1

    Ztr, Zte = standardize(Xtr, Xte)
    Ztr, Zte = np.clip(Ztr, -8, 8), np.clip(Zte, -8, 8)
    # Extreme class weights (only ~97 train positives) destabilize the net and
    # overfit those few winter events; cap the up-weighting.
    pos_w = min(_bce_weight(ytr), 20.0)

    # Non-wind feature width = sectors + Toronto medians + seasonality. Any wind
    # features sit after these, so the "no wind" models train on the [:nb] slice and
    # the "+ WIND" models train on the full matrix — a clean within-run ablation.
    nb = n_sec + 3 + 4                    # (sectors*lags) + Toronto*lags + 4 seasonality
    Ztr_nw, Zte_nw = Ztr[:, :nb], Zte[:, :nb]

    # Upwind-only feature set = drop the 3 Toronto-median persistence columns, to
    # isolate whether the SURROUNDING field alone predicts Toronto.
    tor_cols = list(range(n_sec, n_sec + 3))
    keep = [c for c in range(nb) if c not in tor_cols]

    # ---- Models ----------------------------------------------------------- #
    print("\nTraining models (numpy)...")
    mlp = MLP(Ztr_nw.shape[1], pos_w=pos_w, epochs=args.epochs).fit(Ztr_nw, ytr)
    mlp_up = MLP(len(keep), pos_w=pos_w, epochs=args.epochs).fit(Ztr_nw[:, keep], ytr)
    logit = Logistic(Ztr_nw.shape[1], pos_w=pos_w).fit(Ztr_nw, ytr)
    logit_up = Logistic(len(keep), pos_w=pos_w).fit(Ztr_nw[:, keep], ytr)

    scores = {
        "Persistence (Toronto now)": Xte[:, n_sec],           # Toronto median @ t
        "Upwind max now": np.nanmax(np.where(np.isnan(Xte[:, :8]), -1, Xte[:, :8]), axis=1),
        "Logistic (upwind only)": logit_up.predict_proba(Zte_nw[:, keep]),
        "Logistic (all feats)": logit.predict_proba(Zte_nw),
        "MLP (upwind only)": mlp_up.predict_proba(Zte_nw[:, keep]),
        "MLP (all feats)": mlp.predict_proba(Zte_nw),
    }

    if wind_hourly is not None:              # same features + WIND, for a fair delta
        mlp_w = MLP(Ztr.shape[1], pos_w=pos_w, epochs=args.epochs).fit(Ztr, ytr)
        logit_w = Logistic(Ztr.shape[1], pos_w=pos_w).fit(Ztr, ytr)
        scores["Logistic (all + WIND)"] = logit_w.predict_proba(Zte)
        scores["MLP (all + WIND)"] = mlp_w.predict_proba(Zte)

    print("\n================  TEST-SET RESULTS  ================")
    print(f"{'model':28} {'ROC-AUC':>8}   {'precision':>9} {'recall':>7} {'F1':>6}  (@op point)")
    base = yte.mean()
    for name, sc in scores.items():
        auc = roc_auc(yte, sc)
        # operating point: threshold at the score's (1-base) quantile -> flag the
        # top ~base fraction, a fair per-model comparison on an imbalanced set.
        thr = np.quantile(sc, 1 - base) if np.isfinite(sc).all() else np.nanquantile(sc, 1 - base)
        m = prf_at(yte, sc, thr)
        print(f"{name:28} {auc:8.3f}   {m['precision']:9.2f} {m['recall']:7.2f} {m['f1']:6.2f}"
              f"   tp={m['tp']} fp={m['fp']} fn={m['fn']}")

    print("\nReading: AUC 0.5 = coin flip, 1.0 = perfect. 'Persistence' and 'Upwind")
    print("max now' are dumb baselines; the MLP earns its keep only if it beats them.")
    print(f"Test-set base rate (positives) = {base*100:.1f}%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
