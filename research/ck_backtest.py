#!/usr/bin/env python3
"""Backtest of order-book sources against CoinKarma's own LIQ, read off their earlier X charts on the first day
of a month (Tardis.dev's free days): which book, which band, reproduces their BTC LIQ and Overall LIQ.

CoinKarma's values: research/ck_chart/ck_days.txt (one line per chart, day and panel: bar heights in px,
26 hours from 23:00 the day before; px per unit read off each chart's axis; each chart aligned to time by
matching its candles to Binance BTC hourly closes, correlation 0.94-0.99), plus the 2026-10-29 chart
(data/ck/ck_charts.json). Where several charts show a day, their mean (after dropping the HH9Au9 chart,
whose panel sits inside highlight boxes, and single-column spikes); same-day charts agree 0.65-0.93.
Candidates, the same 24 hours: Binance spot and Coinbase spot full books (research/tardis_spot_day.py,
log(bid / ask) within 0.25-30 % of the price, as quantities and as US$), our Binance perp input
(data/ck/book, 1/2/5 %), and our LIQ. Each is scored on all 24 hours and from 06:00 UTC on: a replayed day
starts from a snapshot of the top 1000 levels a side, so BTC's and ETH's wide bands fill in over the first
hours (in our own collector they are kept across days).
Output: research/ck_chart/backtest.json and a printout."""
import csv
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ck_liq as L  # noqa: E402

SPOT = os.path.join(ROOT, "research", "spot")
BANDS = ("0.25", "0.5", "1", "2", "3", "5", "7.5", "10", "15", "20", "30")
WIDE = ("5", "7.5", "10", "15", "20", "30")  # also scored as US$
FROM_H = 6  # the second score skips the hours before this (the book fills in)
CK23 = [c for c in L.COINS if c != "ZEC"]
LAG = -1  # CoinKarma's bar at hour h-1 goes with our hour h: the best shift for every source, as for the 10/29 chart


def ck_values():
    """{(day, panel): array of 26 hourly values (hour -1 .. 24)}: the mean over the charts showing it."""
    by = {}
    for ln in open(os.path.join(ROOT, "research", "ck_chart", "ck_days.txt")).read().strip().split("\n")[1:]:
        cid, day, pn, ppu, _b, vals, _s = ln.split("|")
        if cid == "HH9Au9kbkAAmDL3":
            continue  # its Overall panel sits inside highlight boxes: heights are box edges (agrees 0.2 with the other charts)
        v = np.array([float(x) for x in vals.split(",")]) / float(ppu)
        med = np.median(v)
        v[np.abs(v - med) > 2.5] = np.nan  # single-column spikes (box edges, labels)
        by.setdefault((day, pn), []).append(v)
    # the 2026-10-29 chart (stored per our hour, already aligned)
    ckc = json.load(open(os.path.join(ROOT, "data", "ck", "ck_charts.json")))["charts"][0]
    for pn, key in (("liq", "btc"), ("ovr", "overall")):
        hours = ["2026-09-30T23"] + [f"2026-10-01T{h:02d}" for h in range(24)] + ["2026-10-02T00"]
        by.setdefault(("2026-10-01", pn), []).append(np.array([ckc[key].get(h, np.nan) for h in hours], float))
    return {k: (np.nanmean(np.vstack(v), axis=0), len(v)) for k, v in by.items()}


def tardis(exch, sym, day):
    for s_ in ([sym] if sym not in ("TONUSDT", "GRAMUSDT") else ["GRAMUSDT", "TONUSDT"]):
        p = os.path.join(SPOT, f"tardis_{exch}_{s_}_{day}.csv")
        if os.path.exists(p):
            rows = {r["hour"][11:13]: r for r in csv.DictReader(open(p))}
            if len(rows) >= 20 and all(f"bidq{b}" in next(iter(rows.values())) for b in BANDS):
                return rows
    return None


def logratio(rows, band, kind="q"):
    out = np.full(24, np.nan)
    for h in range(24):
        r = rows.get(f"{h:02d}")
        if r and float(r[f"ask{kind}{band}"]) > 0 and float(r[f"bid{kind}{band}"]) > 0:
            out[h] = np.log(float(r[f"bid{kind}{band}"]) / float(r[f"ask{kind}{band}"]))
    return out


def corr(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    return float(np.corrcoef(x[m], y[m])[0, 1]) if m.sum() >= 10 and np.std(x[m]) > 0 and np.std(y[m]) > 0 else np.nan


def demean_by_day(v, n_days):
    v = v.reshape(n_days, 24)
    with np.errstate(invalid="ignore"):
        return (v - np.nanmean(v, axis=1, keepdims=True)).ravel()


def score(x, y, days):
    """Pooled and within-day correlation, over all hours and from FROM_H on; and per day."""
    n = len(days)
    late = np.tile(np.arange(24) >= FROM_H, n)
    xl, yl = np.where(late, x, np.nan), np.where(late, y, np.nan)
    per_day = [corr(x[i * 24:(i + 1) * 24], y[i * 24:(i + 1) * 24]) for i in range(n)]
    r = lambda v: None if v != v else round(v, 3)  # noqa: E731
    return {"pooled": r(corr(x, y)), "within_day": r(corr(demean_by_day(x.copy(), n), demean_by_day(y.copy(), n))),
            "pooled_from6": r(corr(xl, yl)), "within_day_from6": r(corr(demean_by_day(xl.copy(), n), demean_by_day(yl.copy(), n))),
            "per_day": {d: (None if np.isnan(c) else round(c, 2)) for d, c in zip(days, per_day)}}


def main():
    ck = ck_values()
    D = L.Data()
    R = L.compute(D, **L.PARAMS)
    out = {"days": {}, "btc": {}, "overall": {}}
    for panel, sect in (("liq", "btc"), ("ovr", "overall")):
        days = sorted(d for d, p in ck if p == panel)
        y = np.concatenate([ck[(d, panel)][0][1 + LAG:25 + LAG] for d in days])
        out["days"][sect] = {d: ck[(d, panel)][1] for d in days}
        cands = {}
        # ours: the LIQ we publish (Binance perp, standardized) and the raw perp log ratios
        ours = []
        for d in days:
            i0 = L.hidx(f"{d}T00") - D.h0
            ours.append(R["liq"]["BTC"][i0:i0 + 24] if panel == "liq" else R["overall"][i0:i0 + 24])
        cands["ours (Binance perp 5%, our LIQ)"] = np.concatenate(ours)
        if panel == "liq":
            for k in (1, 2, 5):
                arr = []
                for d in days:
                    i0 = L.hidx(f"{d}T00") - D.h0
                    with np.errstate(invalid="ignore", divide="ignore"):
                        arr.append(np.log(D.bid[k]["BTC"][i0:i0 + 24] / D.ask[k]["BTC"][i0:i0 + 24]))
                cands[f"Binance perp {k}% (raw)"] = np.concatenate(arr)
            for exch, sym in (("binance", "BTCUSDT"), ("coinbase", "BTC-USD")):
                for kind in ("q", "n"):
                    for b in (BANDS if kind == "q" else WIDE):
                        arr = [logratio(t, b, kind) if (t := tardis(exch, sym, d)) else np.full(24, np.nan) for d in days]
                        cands[f"{exch} spot {b}%" + (" US$" if kind == "n" else "")] = np.concatenate(arr)
        else:
            # Overall from Binance spot: each coin's log ratio over its sd across all the sampled days, market-cap weighted
            all_days = sorted({d for d, p in ck})
            for kind in ("q", "n"):
                for b in (BANDS if kind == "q" else WIDE):
                    per = {}
                    for c in CK23:
                        sym = c + "USDT"
                        xs = {d: logratio(t, b, kind) for d in all_days if (t := tardis("binance", sym, d))}
                        if not xs:
                            continue
                        sd = np.nanstd(np.concatenate(list(xs.values())))
                        if sd > 0:
                            per[c] = {d: v / sd for d, v in xs.items()}
                    arr = []
                    for d in days:
                        i0 = L.hidx(f"{d}T00") - D.h0
                        num, den = np.zeros(24), np.zeros(24)
                        for c, xs in per.items():
                            if d not in xs:
                                continue
                            w = D.mcap[c][i0:i0 + 24]
                            ok = np.isfinite(xs[d]) & np.isfinite(w)
                            num += np.where(ok, w * np.nan_to_num(xs[d]), 0)
                            den += np.where(ok, w, 0)
                        arr.append(np.where(den > 0, num / np.where(den > 0, den, 1), np.nan))
                    cands[f"binance spot {b}%" + (" US$" if kind == "n" else "") + " (24 coins)"] = np.concatenate(arr)
        for name, x in cands.items():
            out[sect][name] = score(x, y, days)
    with open(os.path.join(ROOT, "research", "ck_chart", "backtest.json"), "w") as f:
        json.dump(out, f, indent=1)
    f2 = lambda v: "  —  " if v is None else f"{v:+.2f}"  # noqa: E731
    for sect in ("btc", "overall"):
        print(f"\n== {sect}: days {out['days'][sect]}")
        print(f"{'source':38s} pooled within  from {FROM_H}h: pooled within   per day")
        for name, r in sorted(out[sect].items(), key=lambda kv: -(kv[1]["pooled"] if kv[1]["pooled"] is not None else -9)):
            print(f"{name:38s} {f2(r['pooled'])}  {f2(r['within_day'])}          {f2(r['pooled_from6'])}  {f2(r['within_day_from6'])}    "
                  + " ".join(f2(v) for v in r["per_day"].values()))


if __name__ == "__main__":
    main()
