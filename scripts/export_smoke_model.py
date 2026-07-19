#!/usr/bin/env python3
"""Freeze the NAPS smoke LSTM (+wind) into a committed JSON the web app can score
WITHOUT PyTorch (pure numpy on Vercel).

Trains one model per horizon via train_smoke_lstm.train_model() — the EXACT training
code the research used, so the deployed weights carry no drift — and writes weights,
the train-fit standardization (mu/sd), the feature order, and a torch REFERENCE
(standardized window + its probability) per horizon so the numpy re-implementation in
webapp/dashboard/services/smoke_forecast.py can be verified to match torch to <1e-5.

ISOLATION: offline research tooling. Produces a static asset; touches no live code.

Usage:
    python scripts/export_smoke_model.py \
        --asset naps_hourly/naps_pm25_hourly.json.gz \
        --wind  wind_hourly/wind_naps_pm25_hourly.json.gz
Requires torch (CI / dev box with the research deps), NOT the Vercel runtime.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch

from train_smoke_lstm import train_model, load_any, parse_t0

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "webapp" / "dashboard" / "services" / "smoke_model.json"


def _weights(model):
    """Extract the LSTM + head tensors as plain nested lists (gate order i,f,g,o)."""
    sd = model.state_dict()
    def L(k):
        return sd[k].cpu().numpy().astype(float).tolist()
    return {
        "weight_ih": L("lstm.weight_ih_l0"),   # (4H, F)
        "weight_hh": L("lstm.weight_hh_l0"),   # (4H, H)
        "bias_ih": L("lstm.bias_ih_l0"),       # (4H,)
        "bias_hh": L("lstm.bias_hh_l0"),       # (4H,)
        "head0_w": L("head.0.weight"),         # (32, H+static)
        "head0_b": L("head.0.bias"),           # (32,)
        "head1_w": L("head.3.weight"),         # (1, 32)
        "head1_b": L("head.3.bias"),           # (1,)
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export the smoke LSTM to a numpy-scorable JSON.")
    ap.add_argument("--asset", default=str(REPO / "naps_hourly" / "naps_pm25_hourly.json.gz"))
    ap.add_argument("--wind", default=str(REPO / "wind_hourly" / "wind_naps_pm25_hourly.json.gz"))
    ap.add_argument("--horizons", default="6,12,24,48", help="comma-separated lead hours")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--seed", type=int, default=0)
    # LSTM hyperparameters — MUST mirror train_smoke_lstm.py defaults.
    ap.add_argument("--window", type=int, default=24)
    ap.add_argument("--hidden", type=int, default=48)
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--patience", type=int, default=12)
    ap.add_argument("--tor-radius", type=float, default=25.0)
    ap.add_argument("--min-tor", type=int, default=3)
    args = ap.parse_args(argv)

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    device = "cuda" if torch.cuda.is_available() else "cpu"
    d = load_any(args.asset)
    t0 = parse_t0(d)

    payload = {
        "created": datetime.now(timezone.utc).isoformat(),
        "asset": Path(args.asset).name,
        "wind_asset": Path(args.wind).name if args.wind else None,
        "window": args.window,
        "hidden": args.hidden,
        "horizons": {},
        # filled from the first horizon (identical across horizons — Feat and the
        # train split don't depend on horizon):
        "feature_order": None,
        "static_order": None,
        "standardization": None,
        "elevated": None,
        "tor_radius": None,
        "min_tor": None,
    }

    for h in horizons:
        print(f"\n########## horizon H={h} ##########")
        # Re-seed per horizon so each exported model matches its standalone
        # `train_smoke_lstm.py --horizon H --seed S` counterpart exactly.
        torch.manual_seed(args.seed); np.random.seed(args.seed)
        hargs = SimpleNamespace(
            window=args.window, horizon=h, hidden=args.hidden, epochs=args.epochs,
            batch=args.batch, lr=args.lr, patience=args.patience, seed=args.seed,
            tor_radius=args.tor_radius, min_tor=args.min_tor, wind=args.wind,
        )
        res = train_model(d, t0, hargs, device)

        entry = _weights(res["model"])
        entry.update({
            "ref_X": res["ref_X"], "ref_S": res["ref_S"], "ref_prob": res["ref_prob"],
            "test_roc": res["test_roc"], "test_pr": res["test_pr"], "best_auc": res["best_auc"],
        })
        payload["horizons"][str(h)] = entry

        if payload["feature_order"] is None:
            payload["feature_order"] = res["feat_names"]
            payload["static_order"] = res["static_names"]
            payload["standardization"] = {"mu": np.asarray(res["mu"]).tolist(),
                                          "sd": np.asarray(res["sd"]).tolist()}
            payload["elevated"] = res["elevated"]
            payload["tor_radius"] = res["tor_radius"]
            payload["min_tor"] = res["min_tor"]
        else:
            # mu/sd/feature order are horizon-independent; sanity-check they agree.
            assert res["feat_names"] == payload["feature_order"], "feature order drift across horizons"
            assert np.allclose(res["mu"], payload["standardization"]["mu"]), "mu drift across horizons"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"\nwrote {out}  ({kb:.0f} KiB)  horizons={horizons}")
    print("feature_order:", payload["feature_order"])
    for h in horizons:
        e = payload["horizons"][str(h)]
        print(f"  H={h:>2}: test ROC={e['test_roc']:.3f} PR={e['test_pr']:.3f}  ref_prob={e['ref_prob']:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
