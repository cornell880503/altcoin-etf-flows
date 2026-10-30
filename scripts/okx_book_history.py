#!/usr/bin/env python3
"""OKX's daily 5000-level order-book files for the LIQ coins (public, via /api/v5/public/market-data-
history, module 5: a snapshot at 00:00 UTC, then one update a second; published the next day). Replaying
them gives OKX's book every 30 seconds, the same 5000 levels as the live books-full the hourly LIQ
polls, so the map from OKX's imbalance to Binance's (scripts/ck_liq.py live_extend) can be fitted on
days both exist instead of waiting for the live polling to overlap Binance's next-day files.

Rows, one gzip CSV per UTC day in data/ck/live/okxhist/ (the live hours' format,
scripts/fetch_book_live.py): hour, okx:COIN, n, bid1, ask1, bid2, ask2, bid5, ask5, bidR, askR, reach
(notional US$ within 1/2/5% of the mid and within R = min(5%, the depth both sides reach), the mean of
the hour's 30-second samples).
Usage: okx_book_history.py [--days 8] [--workers 4]
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import io
import json
import os
import tarfile
import tempfile
import time
import urllib.request

import numpy as np

from fetch_book_live import COINS, FIELDS, UA, g6

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "ck", "live", "okxhist")
UTC = dt.timezone.utc
STEP_MS = 30000


def get_json(url):
    for k in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(3 * (k + 1))
    return None


def file_urls(coin, first, last):
    """{day: url} of OKX's daily 5000-level files for COIN-USDT-SWAP between two dates."""
    b = int(dt.datetime.combine(first, dt.time(), UTC).timestamp() * 1000)
    e = int(dt.datetime.combine(last, dt.time(), UTC).timestamp() * 1000)
    j = get_json(f"https://www.okx.com/api/v5/public/market-data-history?module=5&instType=SWAP&instFamilyList={coin}-USDT"
                 f"&dateAggrType=daily&begin={b}&end={e}")
    out = {}
    for d in ((j or {}).get("data") or [{}])[0].get("details") or []:
        for g in d.get("groupDetails") or []:
            day = dt.datetime.fromtimestamp(int(g["dateTs"]) / 1000, UTC).date().isoformat()
            out[day] = g["url"]
    return out


def depth(bids, asks, ct):
    if not bids or not asks:
        return None
    bp = np.fromiter(bids.keys(), float, len(bids))
    bs = np.fromiter(bids.values(), float, len(bids))
    ap = np.fromiter(asks.keys(), float, len(asks))
    as_ = np.fromiter(asks.values(), float, len(asks))
    bb, ba = bp.max(), ap.min()
    if not (bb > 0 and ba > bb):
        return None
    mid = (bb + ba) / 2
    bn, an = bp * bs * ct, ap * as_ * ct
    reach = min((1 - bp.min() / mid) * 100, (ap.max() / mid - 1) * 100)
    out = {}
    for k in (1, 2, 5):
        if reach >= k:
            out[f"bid{k}"] = float(bn[bp >= mid * (1 - k / 100)].sum())
            out[f"ask{k}"] = float(an[ap <= mid * (1 + k / 100)].sum())
    r = min(5.0, reach)
    out["bidR"] = float(bn[bp >= mid * (1 - r / 100)].sum())
    out["askR"] = float(an[ap <= mid * (1 + r / 100)].sum())
    out["reach"] = r
    return out


def replay(coin, day, url, ct):
    """Hourly means of the 30-second samples of one coin-day."""
    acc = {}
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=600) as r:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                tmp.write(chunk)
        tmp.close()
        bids, asks, nxt = {}, {}, None
        with tarfile.open(tmp.name, mode="r:gz") as tf:
            for m in tf:
                f = tf.extractfile(m)
                if not f:
                    continue
                for line in f:
                    d = json.loads(line)
                    ts = int(d["ts"])
                    if nxt is not None:
                        while ts >= nxt:  # the book as it stood at nxt
                            s = depth(bids, asks, ct)
                            if s:
                                h = dt.datetime.fromtimestamp(nxt / 1000, UTC).strftime("%Y-%m-%dT%H")
                                a = acc.setdefault(h, {"n": 0})
                                a["n"] += 1
                                for k, v in s.items():
                                    t = a.setdefault(k, [0.0, 0])
                                    t[0] += v
                                    t[1] += 1
                            nxt += STEP_MS
                    if d.get("action") == "snapshot":
                        bids = {float(p): float(q) for p, q, *_ in d.get("bids", [])}
                        asks = {float(p): float(q) for p, q, *_ in d.get("asks", [])}
                    else:
                        for side, book in (("bids", bids), ("asks", asks)):
                            for p, q, *_ in d.get(side, []):
                                q = float(q)
                                if q == 0:
                                    book.pop(float(p), None)
                                else:
                                    book[float(p)] = q
                    if nxt is None:
                        nxt = ts - ts % STEP_MS + STEP_MS
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    rows = []
    for h, a in sorted(acc.items()):
        row = [h, f"okx:{coin}", str(a["n"])]
        for fld in FIELDS[3:]:
            t = a.get(fld)
            row.append(g6(t[0] / t[1]) if t and t[1] else "")
        rows.append(row)
    return coin, day, rows


def read_day(p):
    if not os.path.exists(p):
        return {}
    with gzip.open(p, "rt", newline="") as f:
        r = csv.reader(f)
        next(r, None)
        return {(row[0], row[1]): row for row in r if row}


def write_day(p, rows):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIELDS)
    for k in sorted(rows):
        w.writerow(rows[k])
    with open(p + ".tmp", "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as g:
            g.write(buf.getvalue().encode())
    os.replace(p + ".tmp", p)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args(argv)
    t0 = time.time()
    j = get_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    ct = {r["instId"]: float(r.get("ctVal") or 0) for r in (j or {}).get("data") or []}
    today = dt.datetime.now(UTC).date()
    first = today - dt.timedelta(days=a.days)
    jobs = []
    for c in COINS:
        inst = f"{c}-USDT-SWAP"
        if not ct.get(inst):
            continue
        for day, url in sorted(file_urls(c, first, today).items()):
            have = read_day(os.path.join(OUT, f"{day}.csv.gz"))
            if any(k[1] == f"okx:{c}" for k in have) or day >= today.isoformat():
                continue
            jobs.append((c, day, url, ct[inst]))
        time.sleep(0.2)
    print(f"{len(jobs)} coin-days to replay", flush=True)
    by_day = {}
    with cf.ProcessPoolExecutor(a.workers) as ex:
        for fu in cf.as_completed([ex.submit(replay, *jb) for jb in jobs]):
            try:
                c, day, rows = fu.result()
            except Exception as e:
                print("failed:", e, flush=True)
                continue
            by_day.setdefault(day, []).extend(rows)
            print(f"  {c} {day}: {len(rows)} hours ({time.time() - t0:.0f}s)", flush=True)
    for day, rows in by_day.items():
        p = os.path.join(OUT, f"{day}.csv.gz")
        have = read_day(p)
        for r in rows:
            have[(r[0], r[1])] = r
        write_day(p, have)
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
