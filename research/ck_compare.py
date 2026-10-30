#!/usr/bin/env python3
"""Our LIQ against CoinKarma's own (data/ck/ck_charts.json, read off the chart in their 2026-10-29 post), and
what other order books we can get would do: OKX spot (research/okx_spot_history.py, OKX's daily
5000-level files, 24 coins) and Gate spot (research/spot_btc_liq.py, BTC, the full book once an hour).
Output: research/ck_chart/compare.json, and a printout."""
import csv
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ck_liq as L  # noqa: E402

SPOT = os.path.join(ROOT, "research", "spot")


def corr(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    return round(float(np.corrcoef(x[m], y[m])[0, 1]), 3) if m.sum() > 30 else None


def main():
    D = L.Data()
    R = L.compute(D, **L.PARAMS)
    ckc = L.ck_charts()
    h0, h1 = L.hidx("2026-09-12T02"), min(D.h0 + D.n - 1, L.hidx(max(ckc["overall"])))
    H = np.arange(h0, h1 + 1)
    I = H - D.h0
    ck_o = np.array([ckc["overall"].get(L.hstr(h), np.nan) for h in H])
    ck_b = np.array([ckc["btc"].get(L.hstr(h), np.nan) for h in H])

    def spot_x(c, fb="bidR", fa="askR", path=None):
        x = np.full(len(H), np.nan)
        p = path or os.path.join(SPOT, f"okx_{c}.csv")
        if not os.path.exists(p):
            return None
        for r in csv.DictReader(open(p)):
            h = L.hidx(r["hour"])
            if h0 <= h <= h1 and r.get(fb) and r.get(fa) and float(r[fb]) > 0 and float(r[fa]) > 0:
                x[h - h0] = np.log(float(r[fb]) / float(r[fa]))
        return x

    def overall(liq):
        num, den = np.zeros(len(H)), np.zeros(len(H))
        for c, z in liq.items():
            w = D.mcap[c][I]
            ok = np.isfinite(z) & np.isfinite(w) & R["elig"][c][I]
            num += np.where(ok, w * z, 0)
            den += np.where(ok, w, 0)
        return np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)

    spot = {c: x for c in L.COINS if (x := spot_x(c)) is not None and np.isfinite(x).sum() > 200}
    if "GRAM" not in spot and (x := spot_x("TON")) is not None:
        spot["GRAM"] = x
    spot_liq = {c: x / np.nanstd(x) for c, x in spot.items()}  # the period's sd (too short for a 720-hour window)
    o_ours, o_spot = R["overall"][I], overall(spot_liq)
    hyb = dict(spot_liq)
    hyb["BTC"] = R["liq"]["BTC"][I]
    wk = (H >= L.hidx("2026-10-22T00")) & (H <= L.hidx("2026-10-28T23"))
    w2 = (H >= L.hidx("2026-10-21T00")) & (H <= L.hidx("2026-10-28T23"))

    def week(o):
        v = o[wk]
        return {"mean": round(float(np.nanmean(v)), 2), "min": round(float(np.nanmin(v)), 2), "max": round(float(np.nanmax(v)), 2),
                "hours_ge2": int(np.sum(v >= 2)), "hours_lt0": int(np.sum(v < 0))}
    gate = {k: spot_x("BTC", f"bid{k}", f"ask{k}", os.path.join(SPOT, "btc_gate.csv")) for k in (1, 2, 5, 10)}
    out = {
        "period_utc": [L.hstr(h0), L.hstr(h1)], "hours": int(np.isfinite(ck_o).sum()), "source": ckc["posts"],
        "overall_corr": {"ours_binance_perp": corr(o_ours, ck_o), "okx_spot_24": corr(o_spot, ck_o),
                         "perp_btc_plus_spot_alts": corr(overall(hyb), ck_o),
                         "spot_alts_only": corr(overall({c: v for c, v in spot_liq.items() if c != "BTC"}), ck_o)},
        "btc_liq_corr": {"ours_binance_perp_5": corr(R["liq"]["BTC"][I], ck_b), "okx_spot": corr(spot.get("BTC"), ck_b),
                         **{f"gate_spot_{k}": corr(v, ck_b) for k, v in gate.items() if v is not None}},
        "oct21_28_corr": {"btc_ours": corr(np.where(w2, R["liq"]["BTC"][I], np.nan), ck_b),
                          "btc_okx_spot": corr(np.where(w2, spot.get("BTC"), np.nan), ck_b),
                          "overall_ours": corr(np.where(w2, o_ours, np.nan), ck_o), "overall_okx_spot": corr(np.where(w2, o_spot, np.nan), ck_o)},
        "week_oct22_28": {"coinkarma": week(ck_o), "ours": week(o_ours), "okx_spot": week(o_spot)},
        "level": {"coinkarma_mean": round(float(np.nanmean(ck_o)), 2), "ours_mean": round(float(np.nanmean(o_ours)), 2),
                  "okx_spot_mean": round(float(np.nanmean(o_spot)), 2)},
        "per_coin_vs_ck_overall": {c: {"okx_spot": corr(spot[c], ck_o), "perp": corr(R["liq"][c][I], ck_o)} for c in spot},
    }
    os.makedirs(os.path.join(ROOT, "research", "ck_chart"), exist_ok=True)
    with open(os.path.join(ROOT, "research", "ck_chart", "compare.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "per_coin_vs_ck_overall"}, indent=1))


if __name__ == "__main__":
    main()
