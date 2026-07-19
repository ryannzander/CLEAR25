#!/usr/bin/env python3
"""Backtest the DEPLOYED Toronto smoke-forecast model as a hypothetical alert trigger.

THE QUESTION (the project's last deferred decision): should the ML forecast be
allowed to influence the validated 3-rule alert engine? The user's rule: not
until backtested. This script is that backtest.

WHAT IT DOES
------------
Replays the exact deployed artifact (webapp/dashboard/services/smoke_model.json —
weights + train-fit standardization; the pure-Python prod scorer was proven equal
to torch to <1e-9, so torch batch-scoring the same weights IS the deployed model)
hour-by-hour over the NAPS history:

  * features rebuilt with the same training helpers (build_series/hourly_features
    + wind_features) on the NAPS + ERA5 wind assets;
  * standardized with the EXPORTED mu/sd (never refit — deployed behaviour);
  * "warning" = P_H(t) >= theta_H;
  * elevated EPISODE = Toronto-core median crossing >= 35 ug/m3 after >= 6 clean
    hours; detection = any warn in the H hours before onset; lead = onset minus
    first such warn; false episode = a run of warn-hours with no >= 35 hour within
    the following H hours.

HONESTY RULES
-------------
  * theta_H is selected ONLY on the validation window (2016-2020, same split the
    model was trained/early-stopped with); the 2020-2024 test tail is touched once.
  * A persistence baseline (threshold on Toronto's own current PM, tuned the same
    way on val) is reported alongside — the model must beat it to justify fusion.
  * Episodes with no valid model coverage in their pre-onset window are reported
    as 'uncovered', not silently dropped.

ISOLATION: research script; reads committed/gitignored assets only.

Usage:
    python scripts/backtest_smoke_alerts.py \
        --asset naps_hourly/naps_pm25_hourly.json.gz \
        --wind  wind_hourly/wind_naps_pm25_hourly.json.gz
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import timedelta
from pathlib import Path

import numpy as np
import torch

from train_smoke_lstm import (LSTMClassifier, build_series, hourly_features,
                              load_any, parse_t0)
from wind_features import build_features, load_wind, series_for
from poc_smoke_model import TOR_LAT, TOR_LON

REPO = Path(__file__).resolve().parent.parent
MODEL_JSON = REPO / "webapp" / "dashboard" / "services" / "smoke_model.json"

ELEVATED = 35.0
CLEAN_HOURS = 6          # hours below threshold required before a new onset
MIN_PREV_VALID = 3       # valid hours required in the pre-onset clean window


# --------------------------------------------------------------------------- #
# Deployed-model loading (exported JSON -> torch)
# --------------------------------------------------------------------------- #
def load_deployed(path=MODEL_JSON):
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    mu = np.asarray(m["standardization"]["mu"], dtype=np.float64)
    sd = np.asarray(m["standardization"]["sd"], dtype=np.float64)
    models = {}
    for hk, e in m["horizons"].items():
        net = LSTMClassifier(len(m["feature_order"]), 2, hidden=m["hidden"])
        state = {
            "lstm.weight_ih_l0": torch.tensor(e["weight_ih"]),
            "lstm.weight_hh_l0": torch.tensor(e["weight_hh"]),
            "lstm.bias_ih_l0": torch.tensor(e["bias_ih"]),
            "lstm.bias_hh_l0": torch.tensor(e["bias_hh"]),
            "head.0.weight": torch.tensor(e["head0_w"]),
            "head.0.bias": torch.tensor(e["head0_b"]),
            "head.3.weight": torch.tensor(e["head1_w"]),
            "head.3.bias": torch.tensor(e["head1_b"]),
        }
        net.load_state_dict(state)
        net.eval()
        models[int(hk)] = net
    return m, mu, sd, models


def sanity_check_reference(m, models):
    """The exported per-horizon reference window must reproduce ref_prob."""
    worst = 0.0
    for hk, e in m["horizons"].items():
        X = torch.tensor([e["ref_X"]], dtype=torch.float32)
        S = torch.tensor([e["ref_S"]], dtype=torch.float32)
        with torch.no_grad():
            p = torch.sigmoid(models[int(hk)](X, S)).item()
        worst = max(worst, abs(p - e["ref_prob"]))
    if worst > 1e-5:
        raise SystemExit(f"deployed-weight sanity check FAILED (|diff|={worst:.2e})")
    print(f"deployed-weight sanity check: PASS (worst |diff| {worst:.2e})")


# --------------------------------------------------------------------------- #
# Episode extraction
# --------------------------------------------------------------------------- #
def find_onsets(tor_med):
    """Hours where the Toronto median crosses >= ELEVATED after >= CLEAN_HOURS
    below (NaNs don't count as elevated; need MIN_PREV_VALID valid clean hours)."""
    n = len(tor_med)
    onsets = []
    for t in range(CLEAN_HOURS, n):
        v = tor_med[t]
        if not np.isfinite(v) or v < ELEVATED:
            continue
        prev = tor_med[t - CLEAN_HOURS:t]
        valid = prev[np.isfinite(prev)]
        if len(valid) >= MIN_PREV_VALID and np.all(valid < ELEVATED):
            onsets.append(t)
    return onsets


# --------------------------------------------------------------------------- #
# Warn evaluation
# --------------------------------------------------------------------------- #
def episode_metrics(onsets, warn_hours, probs_valid, tor_med, H, lo, hi):
    """Detection + lead time for onsets in [lo, hi); false warn-episodes.

    warn_hours: boolean array over the full timeline (True where P >= theta and
    the model had a valid window). probs_valid: where the model could score."""
    detected, leads, uncovered = 0, [], 0
    ons = [t for t in onsets if lo <= t < hi]
    for t in ons:
        w0, w1 = max(0, t - H), t
        window = np.arange(w0, w1)
        if len(window) == 0 or not probs_valid[window].any():
            uncovered += 1
            continue
        hits = window[warn_hours[window]]
        if len(hits):
            detected += 1
            leads.append(t - hits[0])
    # false warn-episodes: runs of warn hours where no hour in (t, t+H] of the
    # run's START has an elevated (valid) reading
    false_eps = 0
    t = lo
    while t < hi:
        if warn_hours[t]:
            run_start = t
            while t < hi and warn_hours[t]:
                t += 1
            fut = tor_med[run_start + 1:min(run_start + H + 1, len(tor_med))]
            fut = fut[np.isfinite(fut)]
            if len(fut) == 0 or np.all(fut < ELEVATED):
                false_eps += 1
        else:
            t += 1
    return {
        "episodes": len(ons), "detected": detected, "uncovered": uncovered,
        "mean_lead_h": float(np.mean(leads)) if leads else None,
        "median_lead_h": float(np.median(leads)) if leads else None,
        "false_episodes": false_eps,
    }


def pick_threshold(probs, valid, onsets, tor_med, H, lo, hi, grid):
    """Select theta on [lo,hi) maximizing episode-level F1 (detection vs false
    episodes); ties -> higher detection, then fewer false episodes."""
    best = None
    for theta in grid:
        warn = valid & (probs >= theta)
        m = episode_metrics(onsets, warn, valid, tor_med, H, lo, hi)
        det, ep, fe = m["detected"], m["episodes"], m["false_episodes"]
        if ep == 0:
            continue
        recall = det / ep
        prec = det / (det + fe) if det + fe else 0.0
        f1 = 2 * prec * recall / (prec + recall) if prec + recall else 0.0
        key = (f1, recall, -fe)
        if best is None or key > best[0]:
            best = (key, theta, m)
    return best[1], best[2]


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default=str(REPO / "naps_hourly" / "naps_pm25_hourly.json.gz"))
    ap.add_argument("--wind", default=str(REPO / "wind_hourly" / "wind_naps_pm25_hourly.json.gz"))
    ap.add_argument("--tor-radius", type=float, default=25.0)
    ap.add_argument("--min-tor", type=int, default=3)
    ap.add_argument("--out", default=str(REPO / "scripts" / "backtest_results.json"))
    args = ap.parse_args(argv)

    m, mu, sd, models = load_deployed()
    sanity_check_reference(m, models)
    window = int(m["window"])
    horizons = sorted(models)
    print(f"deployed model: window={window}  horizons={horizons}  features={len(m['feature_order'])}")

    d = load_any(args.asset)
    t0 = parse_t0(d)
    tor_med, sec_mean, n_tor, n_other, _ = build_series(d, args.tor_radius, args.min_tor)
    n = len(tor_med)
    Feat, Stat = hourly_features(tor_med, sec_mean, t0)
    w = load_wind(args.wind)
    wspd, wdir = series_for(w, t0, n, TOR_LAT, TOR_LON)
    Wf, _ = build_features(wspd, wdir, sec_mean)
    Feat = np.concatenate([Feat, Wf], axis=1)
    assert Feat.shape[1] == len(m["feature_order"]), "feature width mismatch vs deployed model"

    # Standardize with the DEPLOYED mu/sd (exactly services/smoke_forecast._standardize)
    Z = (np.where(np.isnan(Feat), mu, Feat) - mu) / sd
    Z = np.clip(np.nan_to_num(Z, nan=0.0), -8.0, 8.0).astype(np.float32)

    # Valid scoring hours: full Toronto coverage across the window (training's rule)
    tor_ok = np.isfinite(tor_med)
    valid = np.zeros(n, dtype=bool)
    csum = np.cumsum(tor_ok.astype(int))
    for t in range(window - 1, n):
        if csum[t] - (csum[t - window] if t >= window else 0) == window:
            valid[t] = True

    # Batch-score every valid hour for every horizon
    idx = np.nonzero(valid)[0]
    X = np.stack([Z[t - window + 1:t + 1] for t in idx])
    S = Stat[idx].astype(np.float32)
    print(f"scoring {len(idx):,} hours x {len(horizons)} horizons ...")
    probs = {}
    with torch.no_grad():
        Xt, St = torch.from_numpy(X), torch.from_numpy(S)
        for H in horizons:
            out = []
            for i in range(0, len(Xt), 4096):
                out.append(torch.sigmoid(models[H](Xt[i:i + 4096], St[i:i + 4096])))
            p = torch.cat(out).numpy()
            full = np.zeros(n); full[idx] = p
            probs[H] = full

    # Splits: identical fractions to training (60/15/25 chronological)
    h1, h2 = int(n * 0.60), int(n * 0.75)
    onsets = find_onsets(tor_med)
    print(f"timeline {t0:%Y-%m-%d} +{n:,}h   onsets total={len(onsets)}  "
          f"val[{h1}:{h2}]={sum(1 for t in onsets if h1 <= t < h2)}  "
          f"test[{h2}:]={sum(1 for t in onsets if t >= h2)}")

    grid = np.round(np.arange(0.02, 0.92, 0.02), 2)
    results = {"generated_for": "alert-fusion decision", "horizons": {}}
    print("\n====================  TEST-TAIL RESULTS (2020+)  ====================")
    print(f"{'H':>3} {'trigger':<22} {'theta':>6} {'episodes':>8} {'detected':>8} "
          f"{'mean lead':>9} {'false eps':>9}")
    for H in horizons:
        theta, valm = pick_threshold(probs[H], valid, onsets, tor_med, H, h1, h2, grid)
        warn = valid & (probs[H] >= theta)
        test = episode_metrics(onsets, warn, valid, tor_med, H, h2, n)

        # persistence baseline: threshold Toronto's own current PM, tuned on val
        pers_grid = np.arange(5.0, ELEVATED, 1.0)
        pvalid = tor_ok.copy()
        pprob = np.nan_to_num(tor_med, nan=-1.0)
        ptheta, _ = pick_threshold(pprob, pvalid, onsets, tor_med, H, h1, h2, pers_grid)
        pwarn = pvalid & (pprob >= ptheta)
        ptest = episode_metrics(onsets, pwarn, pvalid, tor_med, H, h2, n)

        for name, th, r in (("LSTM (deployed)", theta, test),
                            (f"persistence >= {ptheta:.0f}", ptheta, ptest)):
            lead = f"{r['mean_lead_h']:.1f}h" if r["mean_lead_h"] is not None else "-"
            print(f"{H:>3} {name:<22} {th:>6.2f} {r['episodes']:>8} {r['detected']:>8} "
                  f"{lead:>9} {r['false_episodes']:>9}")
        results["horizons"][H] = {
            "theta_val_selected": float(theta), "val": valm, "test": test,
            "persistence": {"theta": float(ptheta), "test": ptest},
        }

    days_test = (n - h2) / 24.0
    results["test_span_days"] = round(days_test, 1)
    print(f"\ntest span ~{days_test:.0f} days; false eps are per whole test span.")
    Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
