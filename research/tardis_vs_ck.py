#!/usr/bin/env python3
"""2026-10-01, hour by hour: CoinKarma's charted BTC LIQ and Overall LIQ (data/ck/ck_charts.json) against the
log(bids / asks) of full spot books from Tardis's free sample (Binance, Coinbase; research/tardis_spot_day.py)
within each band, by quantity, next to our Binance perp input and OKX's spot book.
Output: research/ck_chart/tardis_vs_ck.json and a printout."""
import csv
import glob
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ck_liq as L  # noqa: E402

DAY = "2026-10-01"
BANDS = ("0.25", "0.5", "1", "2", "3", "5", "10")


def corr(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    return round(float(np.corrcoef(x[m], y[m])[0, 1]), 3) if m.sum() >= 12 and np.std(x[m]) > 0 else None


def tardis(exch, sym):
    p = os.path.join(ROOT, "research", "spot", f"tardis_{exch}_{sym}_{DAY}.csv")
    return {r["hour"]: r for r in csv.DictReader(open(p))} if os.path.exists(p) else {}


def main():
    ckc = L.ck_charts()
    hours = [f"{DAY}T{h:02d}" for h in range(24)]
    ck_b = [ckc["btc"].get(h, np.nan) for h in hours]
    ck_o = [ckc["overall"].get(h, np.nan) for h in hours]
    D = L.Data()
    R = L.compute(D, **L.PARAMS)
    out = {"day": DAY, "hours": 24, "ck_btc_range": [min(ck_b), max(ck_b)], "btc": {}, "overall": {}}
    for exch, sym in (("binance", "BTCUSDT"), ("coinbase", "BTC-USD")):
        t = tardis(exch, sym)
        for k in BANDS:
            for kind in ("q", "n"):
                x = [np.log(float(t[h][f"bid{kind}{k}"]) / float(t[h][f"ask{kind}{k}"])) if h in t and float(t[h][f"ask{kind}{k}"]) > 0 else np.nan
                     for h in hours]
                out["btc"][f"{exch} spot {k}% {'quantity' if kind == 'q' else 'US$'}"] = corr(x, ck_b)
    perp = [R["raw"]["BTC"][L.hidx(h) - D.h0] for h in hours]
    out["btc"]["binance perp 5% (ours)"] = corr(perp, ck_b)
    okx = {r["hour"]: r for r in csv.DictReader(open(os.path.join(ROOT, "research", "spot", "okx_BTC.csv")))}
    out["btc"]["okx spot (~1%)"] = corr([np.log(float(okx[h]["bidR"]) / float(okx[h]["askR"])) if h in okx else np.nan for h in hours], ck_b)
    # Overall: Binance spot, every coin we have, market-cap weighted (raw log ratios; one day is too short to standardize)
    for k in BANDS:
        num, den = np.zeros(24), np.zeros(24)
        for p in glob.glob(os.path.join(ROOT, "research", "spot", f"tardis_binance_*USDT_{DAY}.csv")):
            sym = os.path.basename(p).split("_")[2]
            c = {"TON": "GRAM"}.get(sym[:-4], sym[:-4])
            if c not in L.COINS:
                continue
            t = {r["hour"]: r for r in csv.DictReader(open(p))}
            for i, h in enumerate(hours):
                j = L.hidx(h) - D.h0
                w = D.mcap[c][j] if 0 <= j < D.n else np.nan
                if h in t and float(t[h][f"askq{k}"]) > 0 and float(t[h][f"bidq{k}"]) > 0 and np.isfinite(w):
                    num[i] += w * np.log(float(t[h][f"bidq{k}"]) / float(t[h][f"askq{k}"]))
                    den[i] += w
        out["overall"][f"binance spot {k}% quantity"] = corr(np.where(den > 0, num / np.where(den > 0, den, 1), np.nan), ck_o)
    out["overall"]["ours (binance perp)"] = corr([R["overall"][L.hidx(h) - D.h0] for h in hours], ck_o)
    with open(os.path.join(ROOT, "research", "ck_chart", "tardis_vs_ck.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
