#!/usr/bin/env python3
"""Which spot exchanges' order books, alone or summed, reproduce CoinKarma's BTC LIQ and ETH LIQ?

CoinKarma's values: their Telegram tables (data/ck/pulse/pulse.csv), three a day; a table updated at hour U
describes the hour before it. Order books: Tardis.dev's free days (the first of each month, 2025-02 .. 2026-11),
replayed by research/tardis_spot_day.py for every spot exchange Tardis offers (research/spot/tardis_<exchange>_
<symbol>_<date>.csv, hourly means of the depth within 0.25-30 % of the mid, bids and asks in coins and in US$).
On each day the tables at 08:00 and 16:00 UTC (hours 07 and 15) and the next day's 00:00 (hour 23) are used.

For each candidate x = log(bids / asks) within the band: the correlation with CoinKarma's LIQ over all the
points (their LIQ is that ratio over a slowly moving scale, so a correlation is the right test), overall and on
the points from hour 15 on only (a replayed day starts from a snapshot of the top of the book, so Binance's
wide bands fill in during the first hours). Candidates: each exchange alone; the sum over all exchanges; the
sum over the big ones; the sum without Binance; the mean of the exchanges' log ratios.
Output: research/ck_multi_venue.json and a printout."""
import csv
import glob
import json
import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ck_liq as L  # noqa: E402

SPOT = os.path.join(ROOT, "research", "spot")
BANDS = ("2", "5", "10")  # the older replays have no wider bands
HOURS = (7, 15, 23)
BIG = ("binance", "coinbase", "okex", "bybit-spot", "kraken", "bitfinex")


def tables():
    """{(date, hour): {"BTC": value, "ETH": value}} from the Telegram tables, on our candle hours."""
    out = {}
    for r in csv.DictReader(open(os.path.join(ROOT, "data", "ck", "pulse", "pulse.csv"))):
        u = r.get("update_at") or ""
        if len(u) < 16 or not u[11:13].isdigit() or int(u[11:13]) > 23:
            continue
        h = L.hidx(u[:13]) - 1
        d = {}
        for coin, k, st in (("BTC", "btc_liq", "btc_liq_st"), ("ETH", "eth_liq", "eth_liq_st")):
            try:
                v = float(r[k])
            except (TypeError, ValueError):
                continue
            # a lost minus sign: the status word decides (as in ck_pulse_backtest)
            if r.get(st) in ("Sell", "Caution") and v > 0:
                v = -v
            d[coin] = v
        out[(L.hstr(h)[:10], h % 24)] = d
    return out


def books():
    """{(exchange, coin, date): {hour: row}} from the replayed days."""
    out = {}
    for p in glob.glob(os.path.join(SPOT, "tardis_*_20*.csv")):
        m = re.match(r"tardis_(.+?)_(.+)_(\d{4}-\d{2}-\d{2})\.csv$", os.path.basename(p))
        if not m:
            continue
        ex, sym, date = m.groups()
        coin = "BTC" if sym.upper().startswith(("BTC", "XBT")) else "ETH" if sym.upper().startswith("ETH") else None
        if not coin:
            continue
        rows = {int(r["hour"][11:13]): r for r in csv.DictReader(open(p))}
        if len(rows) < 20 or "bidq10" not in next(iter(rows.values())):
            continue
        key = (ex, coin, date)
        if key in out:  # two symbols of one exchange (USDT and USD pairs): keep the deeper one
            old = out[key]
            if np.nanmean([float(r["bidq10"]) + float(r["askq10"]) for r in old.values()]) >= np.nanmean([float(r["bidq10"]) + float(r["askq10"]) for r in rows.values()]):
                continue
        out[key] = rows
    return out


def corr(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    return (float(np.corrcoef(x[m], y[m])[0, 1]), int(m.sum())) if m.sum() >= 8 and x[m].std() > 0 and y[m].std() > 0 else (None, int(m.sum()))


def main():
    T = tables()
    B = books()
    dates = sorted({d for _, _, d in B})
    exchanges = sorted({e for e, _, _ in B})
    out = {"dates": dates, "exchanges": exchanges, "results": {}}
    for coin in ("BTC", "ETH"):
        pts = [(d, h) for d in dates for h in HOURS if (d, h) in T and coin in T[(d, h)]]
        y = np.array([T[(d, h)][coin] for d, h in pts])
        late = np.array([h >= 15 for d, h in pts])
        res = {}
        depth = {}
        for band in BANDS:
            def ratio_of(exs):
                xs = []
                for d, h in pts:
                    b = a = 0.0
                    ok = False
                    for e in exs:
                        r = B.get((e, coin, d), {}).get(h)
                        if r:
                            b += float(r[f"bidq{band}"]); a += float(r[f"askq{band}"]); ok = True
                    xs.append(np.log(b / a) if ok and b > 0 and a > 0 else np.nan)
                return np.array(xs)
            cands = {}
            for e in exchanges:
                cands[f"{e} alone"] = ratio_of([e])
                if band == "10":
                    depth[e] = float(np.nanmean([float(r["bidq10"]) + float(r["askq10"]) for (ee, c, d), rows in B.items() if ee == e and c == coin for r in rows.values()]))
            avail = [e for e in exchanges if any((e, coin, d) in B for d in dates)]
            cands["all exchanges summed"] = ratio_of(avail)
            cands["big six summed"] = ratio_of([e for e in BIG if e in avail])
            cands["all but Binance summed"] = ratio_of([e for e in avail if e != "binance"])
            cands["Binance + Coinbase summed"] = ratio_of([e for e in ("binance", "coinbase") if e in avail])
            cands["Binance + OKX + Bybit summed"] = ratio_of([e for e in ("binance", "okex", "bybit-spot") if e in avail])
            with np.errstate(invalid="ignore"):
                single = np.vstack([cands[f"{e} alone"] for e in avail])
                cands["mean of the exchanges' log ratios"] = np.nanmean(single, axis=0)
            res[band] = {}
            for name, x in cands.items():
                c_all, n_all = corr(x, y)
                c_late, n_late = corr(x[late], y[late])
                res[band][name] = {"corr": None if c_all is None else round(c_all, 3), "n": n_all,
                                   "corr_from15": None if c_late is None else round(c_late, 3), "n_from15": n_late}
        out["results"][coin] = {"n_points": len(pts), "depth_within_10pct_coins": {e: round(v, 1) for e, v in depth.items()}, "by_band": res}
    with open(os.path.join(ROOT, "research", "ck_multi_venue.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(f"days {len(dates)}: {dates[0]} .. {dates[-1]}; exchanges {exchanges}")
    for coin in ("BTC", "ETH"):
        r = out["results"][coin]
        print(f"\n== {coin} LIQ: {r['n_points']} tables; mean depth within 10% (coins): "
              + ", ".join(f"{e} {v:,.0f}" for e, v in sorted(r["depth_within_10pct_coins"].items(), key=lambda kv: -kv[1])))
        print(f"{'candidate':40s} " + "  ".join(f"{b + '%':>13s}" for b in BANDS) + "   (corr all / from 15h)")
        names = list(r["by_band"]["10"].keys())
        names.sort(key=lambda nm: -(r["by_band"]["10"][nm]["corr"] or -9))
        for nm in names:
            cells = []
            for b in BANDS:
                v = r["by_band"][b][nm]
                cells.append(f"{(v['corr'] if v['corr'] is not None else float('nan')):+.2f}/{(v['corr_from15'] if v['corr_from15'] is not None else float('nan')):+.2f}")
            print(f"{nm:40s} " + "  ".join(f"{c:>13s}" for c in cells) + f"   n={r['by_band']['10'][nm]['n']}")


if __name__ == "__main__":
    main()
