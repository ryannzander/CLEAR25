#!/usr/bin/env python3
"""Train a real (PyTorch) recurrent neural network for Toronto wildfire-smoke
EARLY WARNING, on the 2024-2025 hourly PurpleAir field.

This is the "proper training" upgrade over scripts/poc_smoke_model.py (which used a
flat MLP on fixed lags). Here the model is an LSTM that ingests a WINDOW of the last
`--window` hours of the surrounding sensor field and predicts whether the Toronto-
core median PM2.5 will be elevated (>= 35 ug/m3) `--horizon` hours later.

Proper-training ingredients:
  * sequence model (LSTM) that respects temporal build-up / advection of smoke
  * chronological TRAIN / VAL / TEST split (val for early stopping, test touched once)
  * class-imbalance handling via BCEWithLogitsLoss(pos_weight)
  * early stopping + best-checkpoint on validation ROC-AUC
  * standardization fit on TRAIN only; log1p on heavy-tailed PM
  * honest eval vs the persistence baseline (Toronto's own current PM)

ISOLATION: reads only the committed asset via the PoC's feature helpers; touches
nothing in the live app. Uncorrected ATM PM2.5, so 35 is a nominal threshold.

Reuses build_series() / load_asset() / roc_auc() from poc_smoke_model.py.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

# Same-dir import (scripts/ is on sys.path[0] when run as a script).
from poc_smoke_model import build_series, roc_auc, SECTORS, ELEVATED, TOR_LAT, TOR_LON

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

REPO = Path(__file__).resolve().parent.parent
DEFAULT_ASSET = REPO / "webapp" / "dashboard" / "static" / "dashboard" / "plume_2024_2025.json.gz"


def load_any(path):
    """Load a compact hourly asset from .json or .json.gz (gzip-magic sniffed)."""
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def parse_t0(d):
    s = d.get("t0")
    if s:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Per-hour feature matrix + windowing
# --------------------------------------------------------------------------- #
def hourly_features(tor_med, sec_mean, t0):
    """Feat[n, F] per-hour features (NaN where a sector has no data) and
    Stat[n, S] static seasonality. F = 8 sectors + Toronto median + hour sin/cos."""
    n = len(tor_med)
    F = len(SECTORS) + 1 + 2
    Feat = np.full((n, F), np.nan)
    Stat = np.zeros((n, 2))
    for t in range(n):
        Feat[t, :len(SECTORS)] = sec_mean[t]
        Feat[t, len(SECTORS)] = tor_med[t]
        dt = t0 + timedelta(hours=t)
        hod = dt.hour / 24.0 * 2 * math.pi
        Feat[t, len(SECTORS) + 1] = math.sin(hod)
        Feat[t, len(SECTORS) + 2] = math.cos(hod)
        mon = (dt.month - 1) / 12.0 * 2 * math.pi
        Stat[t] = [math.sin(mon), math.cos(mon)]
    # log1p the PM columns (0..8): sectors + Toronto median.
    pm = Feat[:, :len(SECTORS) + 1]
    Feat[:, :len(SECTORS) + 1] = np.log1p(np.where(pm < 0, 0.0, pm))
    return Feat, Stat


def standardize_train(Feat, train_mask):
    with np.errstate(invalid="ignore"):
        mu = np.nanmean(Feat[train_mask], axis=0)
        sd = np.nanstd(Feat[train_mask], axis=0)
    mu = np.where(np.isfinite(mu), mu, 0.0)
    sd = np.where(np.isfinite(sd) & (sd > 0), sd, 1.0)
    Z = (np.where(np.isnan(Feat), mu, Feat) - mu) / sd
    return np.clip(np.nan_to_num(Z, nan=0.0), -8, 8)


def make_windows(tor_med, Feat_std, Stat, window, horizon):
    """Return X[N,window,F], S[N,2], y[N], t[N] for every valid prediction time."""
    n = len(tor_med)
    Xs, Ss, ys, ts = [], [], [], []
    for t in range(window - 1, n - horizon):
        ty = t + horizon
        if np.isnan(tor_med[ty]):
            continue
        seg = tor_med[t - window + 1:t + 1]
        if np.isnan(seg).any():        # require Toronto coverage across the window
            continue
        Xs.append(Feat_std[t - window + 1:t + 1])
        Ss.append(Stat[t])
        ys.append(1.0 if tor_med[ty] >= ELEVATED else 0.0)
        ts.append(t)
    return (np.asarray(Xs, np.float32), np.asarray(Ss, np.float32),
            np.asarray(ys, np.float32), np.asarray(ts, np.int64))


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
class LSTMClassifier(nn.Module):
    def __init__(self, in_feat, static, hidden=48, layers=1, dropout=0.2):
        super().__init__()
        self.lstm = nn.LSTM(in_feat, hidden, layers, batch_first=True,
                            dropout=dropout if layers > 1 else 0.0)
        self.head = nn.Sequential(
            nn.Linear(hidden + static, 32), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(32, 1),
        )

    def forward(self, x, s):
        out, _ = self.lstm(x)
        h = out[:, -1, :]                       # last-timestep hidden state
        return self.head(torch.cat([h, s], dim=1)).squeeze(-1)   # logits


# --------------------------------------------------------------------------- #
# Train / eval
# --------------------------------------------------------------------------- #
def evaluate(model, loader, device):
    model.eval()
    ys, ps = [], []
    with torch.no_grad():
        for xb, sb, yb in loader:
            logits = model(xb.to(device), sb.to(device))
            ps.append(torch.sigmoid(logits).cpu().numpy())
            ys.append(yb.numpy())
    y = np.concatenate(ys); p = np.concatenate(ps)
    return y, p


def pr_auc(y, score):
    """Average precision (area under precision-recall), numpy."""
    order = np.argsort(-score)
    y = y[order]
    tp = np.cumsum(y)
    fp = np.cumsum(1 - y)
    prec = tp / np.maximum(tp + fp, 1)
    rec = tp / max(y.sum(), 1)
    # integrate precision over recall increments
    rec_prev = np.concatenate([[0], rec[:-1]])
    return float(np.sum(prec * (rec - rec_prev)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=24, help="input history length (hours)")
    ap.add_argument("--horizon", type=int, default=12, help="early-warning lead (hours)")
    ap.add_argument("--hidden", type=int, default=48)
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--patience", type=int, default=12, help="early-stop patience (epochs)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--asset", default=str(DEFAULT_ASSET), help="hourly asset (.json/.json.gz)")
    ap.add_argument("--tor-radius", type=float, default=25.0, help="Toronto-core radius (km)")
    ap.add_argument("--min-tor", type=int, default=3, help="min Toronto stations for a valid label")
    ap.add_argument("--wind", default=None,
                    help="optional wind asset (scripts/gen_wind_hourly.py) — appends causal "
                         "wind features to every window timestep")
    args = ap.parse_args(argv)

    torch.manual_seed(args.seed); np.random.seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"torch {torch.__version__}  device={device}")

    d = load_any(args.asset)
    t0 = parse_t0(d)
    print(f"asset: {Path(args.asset).name}  t0={d.get('t0')}  n_steps={d['n_steps']:,}  "
          f"stations={len(d['stations'])}")
    tor_med, sec_mean, n_tor, n_other, sec_counts = build_series(d, args.tor_radius, args.min_tor)
    n = len(tor_med)
    print(f"Toronto stations (<= {args.tor_radius:.0f} km): {n_tor}   other: {n_other}   "
          f"Toronto-covered hours: {np.isfinite(tor_med).sum():,}")

    Feat, Stat = hourly_features(tor_med, sec_mean, t0)

    # Optional wind: append causal per-hour wind features so the LSTM sees wind
    # (speed / direction / upwind-PM flux) evolve across the whole input window.
    if args.wind:
        from wind_features import load_wind, series_for, build_features
        w = load_wind(args.wind)
        wspd, wdir = series_for(w, t0, n, TOR_LAT, TOR_LON)
        Wf, wnames = build_features(wspd, wdir, sec_mean)
        Feat = np.concatenate([Feat, Wf], axis=1)
        print(f"wind: {Path(args.wind).name} — +{len(wnames)} features "
              f"({np.isfinite(wspd).mean()*100:.0f}% hrs covered): {wnames}")

    # Chronological hour split: 60% train / 15% val / 25% test.
    h1, h2 = int(n * 0.60), int(n * 0.75)
    train_mask = np.zeros(n, bool); train_mask[:h1] = True
    Feat_std = standardize_train(Feat, train_mask)

    X, S, y, t = make_windows(tor_med, Feat_std, Stat, args.window, args.horizon)
    tr = t < h1
    va = (t >= h1) & (t < h2)
    te = t >= h2
    print(f"\nLSTM early warning: predict Toronto PM2.5 >= {ELEVATED:.0f} "
          f"{args.horizon} h ahead from a {args.window} h window.")
    for name, m in (("train", tr), ("val", va), ("test", te)):
        span = f"{(t0+timedelta(hours=int(t[m].min()))):%Y-%m-%d}..{(t0+timedelta(hours=int(t[m].max()))):%Y-%m-%d}"
        print(f"  {name:5}: {m.sum():6,} samples  pos={int(y[m].sum()):4} "
              f"({y[m].mean()*100:4.1f}%)  {span}")
    if y[tr].sum() == 0 or y[va].sum() == 0 or y[te].sum() == 0:
        print("!! a split has no positives — adjust window/horizon."); return 1

    def loader(mask, shuffle):
        return DataLoader(TensorDataset(torch.from_numpy(X[mask]),
                                        torch.from_numpy(S[mask]),
                                        torch.from_numpy(y[mask])),
                          batch_size=args.batch, shuffle=shuffle)
    dl_tr, dl_va, dl_te = loader(tr, True), loader(va, False), loader(te, False)

    model = LSTMClassifier(X.shape[2], S.shape[1], hidden=args.hidden).to(device)
    pos_w = min(float((y[tr] == 0).sum() / max((y[tr] == 1).sum(), 1)), 20.0)
    lossf = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_w, device=device))
    opt = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=1e-4)
    print(f"model params: {sum(p.numel() for p in model.parameters()):,}  pos_weight={pos_w:.1f}")

    best_auc, best_state, bad = -1.0, None, 0
    print("\nepoch   train_loss   val_AUC   val_PR-AUC")
    for ep in range(1, args.epochs + 1):
        model.train()
        tot = 0.0
        for xb, sb, yb in dl_tr:
            opt.zero_grad()
            loss = lossf(model(xb.to(device), sb.to(device)), yb.to(device))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            tot += loss.item() * len(yb)
        yv, pv = evaluate(model, dl_va, device)
        auc = roc_auc(yv, pv); prc = pr_auc(yv, pv)
        flag = ""
        if auc > best_auc:
            best_auc, best_state, bad = auc, {k: v.cpu().clone() for k, v in model.state_dict().items()}, 0
            flag = "  *best"
        else:
            bad += 1
        if ep <= 5 or ep % 5 == 0 or flag:
            print(f"{ep:5d}   {tot/max(tr.sum(),1):10.4f}   {auc:7.3f}   {prc:9.3f}{flag}")
        if bad >= args.patience:
            print(f"early stop @ epoch {ep} (no val-AUC gain in {args.patience})"); break

    model.load_state_dict(best_state)

    # ---- Test (touched once) + persistence baseline ----------------------- #
    yte, pte = evaluate(model, dl_te, device)
    # Persistence baseline on the SAME test samples: Toronto median at t (the
    # standardized col 8 = last-window-step Toronto value ranks identically).
    tor_now = X[te][:, -1, len(SECTORS)]  # standardized Toronto median @ t
    base = yte.mean()
    print("\n================  TEST-SET RESULTS  ================")
    print(f"{'model':26} {'ROC-AUC':>8} {'PR-AUC':>8}")
    print(f"{'Persistence (Toronto now)':26} {roc_auc(yte, tor_now):8.3f} {pr_auc(yte, tor_now):8.3f}")
    print(f"{'LSTM (this model)':26} {roc_auc(yte, pte):8.3f} {pr_auc(yte, pte):8.3f}")
    print(f"\ntest base rate = {base*100:.1f}%   best val AUC = {best_auc:.3f}")
    print("PR-AUC (vs base rate) is the honest metric on a rare-event problem.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
