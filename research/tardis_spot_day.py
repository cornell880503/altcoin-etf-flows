#!/usr/bin/env python3
"""One day of full spot order books from Tardis.dev's free sample (the first day of each month needs no key):
the incremental L2 stream (a snapshot, then every change), replayed and sampled every 30 seconds.
Hourly means of the depth within 0.25 / 0.5 / 1 / 2 / 3 / 5 / 10 % of the mid, bids and asks, as quantity
(coins) and as US$: CoinKarma calls its LIQ "the ratio of quantities" in the spot order book, and the
day can be laid against their charted LIQ (data/ck/ck_charts.json; 2026-10-01 is inside it).
Note: a level that never changes during the day is known only if it was in the opening snapshot (Binance:
the top 1000-5000 levels), so the wide bands of a deep book are partly missing.
Output: research/spot/tardis_<exchange>_<symbol>_<date>.csv
Usage: tardis_spot_day.py --date 2026-10-01 --items binance:BTCUSDT,coinbase:BTC-USD [--workers 4]"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import io
import os
import time
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "spot")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
BANDS = (0.25, 0.5, 1, 2, 3, 5, 10)
STEP_US = 30_000_000
FIELDS = ["hour", "n", "reach", "levels_b", "levels_a"] + [f"{s}{k}" for k in BANDS for s in ("bidq", "askq", "bidn", "askn")]


def bands(bids, asks):
    if not bids or not asks:
        return None
    bp = np.fromiter(bids.keys(), float, len(bids))
    bq = np.fromiter(bids.values(), float, len(bids))
    ap = np.fromiter(asks.keys(), float, len(asks))
    aq = np.fromiter(asks.values(), float, len(asks))
    bb, ba = bp.max(), ap.min()
    if not (bb > 0 and ba > bb):
        return None
    mid = (bb + ba) / 2
    out = {"reach": min((1 - bp.min() / mid) * 100, (ap.max() / mid - 1) * 100), "levels_b": len(bp), "levels_a": len(ap)}
    for k in BANDS:
        mb, ma = bp >= mid * (1 - k / 100), ap <= mid * (1 + k / 100)
        out[f"bidq{k}"] = float(bq[mb].sum())
        out[f"askq{k}"] = float(aq[ma].sum())
        out[f"bidn{k}"] = float((bp[mb] * bq[mb]).sum())
        out[f"askn{k}"] = float((ap[ma] * aq[ma]).sum())
    return out


def replay(item, date):
    exch, sym = item.split(":")
    url = f"https://datasets.tardis.dev/v1/{exch}/incremental_book_L2/{date[:4]}/{date[5:7]}/{date[8:10]}/{sym}.csv.gz"
    t0 = time.time()
    acc = {}
    bids, asks = {}, {}
    nxt, prev_snap, rows = None, False, 0
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=600) as r:
        rd = csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=r), encoding="utf-8"))
        hdr = next(rd)
        ix = {k: i for i, k in enumerate(hdr)}
        i_ts, i_sn, i_side, i_px, i_amt = ix["timestamp"], ix["is_snapshot"], ix["side"], ix["price"], ix["amount"]
        for f in rd:
            rows += 1
            ts = int(f[i_ts])
            snap = f[i_sn] == "true"
            if nxt is not None and not snap:
                while ts >= nxt:  # the book as it stood at nxt
                    s = bands(bids, asks)
                    if s:
                        h = dt.datetime.fromtimestamp(nxt / 1e6, dt.timezone.utc).strftime("%Y-%m-%dT%H")
                        a = acc.setdefault(h, {"n": 0})
                        a["n"] += 1
                        for k, v in s.items():
                            a[k] = a.get(k, 0.0) + v
                    nxt += STEP_US
            if snap and not prev_snap:  # a new snapshot starts: the book starts over
                bids, asks = {}, {}
            prev_snap = snap
            book = bids if f[i_side] == "bid" else asks
            p, q = float(f[i_px]), float(f[i_amt])
            if q == 0:
                book.pop(p, None)
            else:
                book[p] = q
            if nxt is None and not snap:
                nxt = ts - ts % STEP_US + STEP_US
    out = []
    for h, a in sorted(acc.items()):
        n = a.pop("n")
        out.append([h, n] + [f"{a.get(k, 0) / n:.6g}" for k in FIELDS[2:]])
    p = os.path.join(OUT, f"tardis_{exch}_{sym}_{date}.csv")
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        w.writerows(out)
    return item, len(out), rows, round(time.time() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-01")
    ap.add_argument("--items", required=True)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    with cf.ProcessPoolExecutor(a.workers) as ex:
        futs = {ex.submit(replay, it, a.date): it for it in a.items.split(",")}
        for fu in cf.as_completed(futs):
            try:
                print(*fu.result(), flush=True)
            except Exception as e:
                print(futs[fu], "failed:", e, flush=True)


if __name__ == "__main__":
    main()
