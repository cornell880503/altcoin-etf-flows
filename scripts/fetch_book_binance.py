#!/usr/bin/env python3
"""Hourly order-book depth of Binance USD-M perpetuals for CoinKarma's 24 resonance coins, from
Binance's public daily bookDepth files (data.binance.vision, from 2023-01-01; a snapshot about every
30 s of the cumulative depth within 1/2/3/4/5% of the price on each side). Input for the LIQ copies
(scripts/ck_indicators.py): CoinKarma's LIQ uses spot order books; Binance publishes no spot depth
history, so this is a proxy.

Rows in data/ck/book/ (scripts/oi_store.py, keyed by hour and coin): hour (YYYY-MM-DDTHH, UTC), coin,
bid/ask notional (US$) within 1%, 2% and 5% of the price, each the mean over that hour's snapshots.
Modes: default = every missing day from 2023-01-01 (time-boxed by --budget; a later run goes on where it
stopped); --daily = only the last 5 days.
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

import oi_store as store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "ck", "book")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
FIELDS = ["hour", "coin", "bid1", "ask1", "bid2", "ask2", "bid5", "ask5"]
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC", "BCH", "SUI", "HYPE", "UNI",
         "AAVE", "NEAR", "TON", "GRAM", "ATOM", "APT", "ARB", "OP", "ZEC"]
START = dt.date(2023, 1, 1)


def get(url, tries=4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"failed: {url}")


def g6(v):
    return f"{v:.6g}"


LEVELS = (-1.0, 1.0, -2.0, 2.0, -5.0, 5.0)


def one(coin, day):
    """One coin-day: the hourly mean of each level's notional over that hour's snapshots."""
    sym = f"{coin}USDT"
    raw = get(f"https://data.binance.vision/data/futures/um/daily/bookDepth/{sym}/{sym}-bookDepth-{day}.zip")
    if raw is None:
        return coin, day, None
    z = zipfile.ZipFile(io.BytesIO(raw))
    acc = {}  # "YYYY-MM-DD HH" -> {pct: [sum, n]}
    want = set(LEVELS)
    with z.open(z.namelist()[0]) as f:
        for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
            if len(row) < 4 or not row[0][:1].isdigit():
                continue
            try:
                pct = float(row[1])
                if pct not in want:
                    continue
                v = float(row[3])
            except ValueError:
                continue
            a = acc.setdefault(row[0][:13], {}).setdefault(pct, [0.0, 0])
            a[0] += v
            a[1] += 1
    rows = []
    for h, m in sorted(acc.items()):
        vals = [m[p][0] / m[p][1] if p in m and m[p][1] else None for p in LEVELS]
        if all(v is not None and v > 0 for v in vals):
            rows.append([h.replace(" ", "T"), coin] + [g6(v) for v in vals])
    return coin, day, rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=100, help="minutes after which no new download starts")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--daily", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    store.fold(OUT, FIELDS)
    have = {}
    for r in store.read_all(OUT):
        have.setdefault(r[1], set()).add(r[0][:10])
    yday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    first = yday - dt.timedelta(days=5) if a.daily else START
    meta_p = os.path.join(OUT, "_meta.json")
    try:
        meta = json.load(open(meta_p))
    except (OSError, ValueError):
        meta = {}
    absent = set(map(tuple, meta.get("absent", [])))  # (coin, day) answered 404 before a contract existed
    todo = []
    d = first
    while d <= yday:
        for c in COINS:
            if d.isoformat() not in have.get(c, set()) and (c, d.isoformat()) not in absent:
                todo.append((c, d.isoformat()))
        d += dt.timedelta(days=1)
    todo.sort(key=lambda x: (x[1], x[0]), reverse=True)  # newest first: the current reading is usable before the backfill ends
    print(f"to fetch: {len(todo)} coin-days ({'daily' if a.daily else 'full'})", flush=True)
    new, miss, done = [], [], 0
    deadline = t0 + a.budget * 60
    with cf.ProcessPoolExecutor(a.workers) as ex:  # parsing is CPU-bound: processes, not threads
        it, pending = iter(todo), set()
        while True:
            while len(pending) < a.workers * 2 and time.time() < deadline:
                nxt = next(it, None)
                if nxt is None:
                    break
                pending.add(ex.submit(one, *nxt))
            if not pending:
                break
            fin, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fu in fin:
                try:
                    c, day, rows = fu.result()
                except Exception as e:
                    print("fail:", e, flush=True)
                    continue
                done += 1
                if rows is None:
                    if day < (yday - dt.timedelta(days=3)).isoformat():
                        miss.append((c, day))
                else:
                    new += rows
                if done % 2000 == 0:
                    print(f"  {done}/{len(todo)} ({time.time() - t0:.0f}s), rows {len(new)}", flush=True)
                if len(new) > 400000:  # write in chunks to keep memory small
                    store.write_rows(OUT, FIELDS, new)
                    new = []
    if new:
        store.write_rows(OUT, FIELDS, new)
    absent |= set(miss)
    meta.update({"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily" if a.daily else "full",
                 "asked": done, "remaining": len(todo) - done, "absent": sorted(absent)})
    with open(meta_p, "w") as f:
        json.dump(meta, f, separators=(",", ":"))
    print(f"done {done}/{len(todo)} in {time.time() - t0:.0f}s; absent {len(miss)} new", flush=True)


if __name__ == "__main__":
    main()
