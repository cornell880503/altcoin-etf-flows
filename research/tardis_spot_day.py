#!/usr/bin/env python3
"""One day of full spot order books from Tardis.dev's free sample (the first day of each month needs no key):
the incremental L2 stream (a snapshot, then every change), replayed and sampled every 30 seconds.
Hourly means of the depth within BANDS % of the mid (as scripts/binance_spot_ws.py records them), bids and
asks, as quantity (coins) and as US$: CoinKarma calls its LIQ "the ratio of quantities" in the spot order
book, and the day can be laid against their charted LIQ (research/ck_backtest.py).
A level that never changes during the day is known only if it was in a snapshot (Binance: the top 1000
levels a side), so the wide bands of a deep book (BTC, ETH) fill in during the day. A snapshot in the middle
of the day (Tardis reconnected) is merged as our collector merges one: within its range it replaces the book,
the farther levels we had stay (Binance; Coinbase's snapshots are the whole book and replace it).
Output: research/spot/tardis_<exchange>_<symbol>_<date>.csv
Usage: tardis_spot_day.py --date 2026-10-01 --items binance:BTCUSDT,coinbase:BTC-USD [--workers 4]"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import io
import math
import os
import time
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "spot")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
BANDS = (0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30)
STEP_US = 30_000_000
PARTIAL = 1000  # a snapshot side this long is the top of the book (Binance's REST depth), shorter is the whole side
FIELDS = ["hour", "n", "snaps", "reach", "levels_b", "levels_a"] + [f"{s}{k}" for k in BANDS for s in ("bidq", "askq", "bidn", "askn")]


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


def merge(bids, asks, sb, sa, keep):
    """The book after a snapshot (sb, sa): the snapshot within its range; with keep, our farther levels beyond it."""
    if not keep or not (bids or asks):
        return dict(sb), dict(sa)
    lo = min(sb) if len(sb) >= PARTIAL else 0.0
    hi = max(sa) if len(sa) >= PARTIAL else math.inf
    nb, na = dict(sb), dict(sa)
    nb.update({p: q for p, q in bids.items() if p < lo})
    na.update({p: q for p, q in asks.items() if p > hi})
    return nb, na


def replay_rows(rd, keep):
    """Replay a Tardis incremental_book_L2 csv reader (header first); {hour: {"n": samples, "snaps": .., value sums}}."""
    hdr = next(rd)
    ix = {k: i for i, k in enumerate(hdr)}
    i_ts, i_sn, i_side, i_px, i_amt = ix["timestamp"], ix["is_snapshot"], ix["side"], ix["price"], ix["amount"]
    acc, bids, asks = {}, {}, {}
    sb, sa = {}, {}
    nxt, in_snap, rows = None, False, 0
    for f in rd:
        rows += 1
        ts = int(f[i_ts])
        p, q = float(f[i_px]), float(f[i_amt])
        if f[i_sn] == "true":
            if not in_snap:  # a new snapshot starts
                sb, sa, in_snap = {}, {}, True
            if q != 0:
                (sb if f[i_side] == "bid" else sa)[p] = q
            continue
        if in_snap:  # the snapshot is complete: into the book
            bids, asks = merge(bids, asks, sb, sa, keep)
            in_snap = False
            h = dt.datetime.fromtimestamp(ts / 1e6, dt.timezone.utc).strftime("%Y-%m-%dT%H")
            a = acc.setdefault(h, {"n": 0})
            a["snaps"] = a.get("snaps", 0) + 1
        if nxt is None:
            nxt = ts - ts % STEP_US + STEP_US
        while ts >= nxt:  # the book as it stood at nxt
            s = bands(bids, asks)
            if s:
                h = dt.datetime.fromtimestamp(nxt / 1e6, dt.timezone.utc).strftime("%Y-%m-%dT%H")
                a = acc.setdefault(h, {"n": 0})
                a["n"] += 1
                for k, v in s.items():
                    a[k] = a.get(k, 0.0) + v
            nxt += STEP_US
        book = bids if f[i_side] == "bid" else asks
        if q == 0:
            book.pop(p, None)
        else:
            book[p] = q
    return acc, rows


def table(acc):
    out = []
    for h, a in sorted(acc.items()):
        n = a.get("n", 0)
        if not n:
            continue
        out.append([h, n, a.get("snaps", 0)] + [f"{a.get(k, 0) / n:.6g}" for k in FIELDS[3:]])
    return out


def replay(item, date):
    exch, sym = item.split(":")
    url = f"https://datasets.tardis.dev/v1/{exch}/incremental_book_L2/{date[:4]}/{date[5:7]}/{date[8:10]}/{sym}.csv.gz"
    t0 = time.time()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=600) as r:
        rd = csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=r), encoding="utf-8"))
        acc, rows = replay_rows(rd, keep=exch == "binance")
    out = table(acc)
    p = os.path.join(OUT, f"tardis_{exch}_{sym}_{date}.csv")
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        w.writerows(out)
    return item, len(out), rows, sum(a.get("snaps", 0) for a in acc.values()), round(time.time() - t0)


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
