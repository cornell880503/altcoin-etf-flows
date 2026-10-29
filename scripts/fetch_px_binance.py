#!/usr/bin/env python3
"""Hourly candles of Binance USD-M perpetuals for the LIQ coins (scripts/fetch_book_binance.py's list),
from Binance's public files (data.binance.vision: monthly files for finished months, daily files for
the running month and for the last month until its monthly file is out). Used by the LIQ copies for
the resonance prices (CoinKarma: the close of the 1-hour candle), RSI and CVD.

Rows in data/ck/px1h/ (scripts/oi_store.py, keyed by hour and coin): hour (YYYY-MM-DDTHH, UTC, the
candle's open), coin, close, quote volume (US$), taker-buy quote volume (US$).
A symbol renamed on Binance keeps its old files under the old name (TONUSDT before GRAMUSDT); both
are fetched and the LIQ code joins them.
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
from fetch_book_binance import COINS, START, UA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "ck", "px1h")
FIELDS = ["hour", "coin", "close", "qvol", "taker_qvol"]
BASE = "https://data.binance.vision/data/futures/um"


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


def parse(raw, coin):
    z = zipfile.ZipFile(io.BytesIO(raw))
    rows = []
    with z.open(z.namelist()[0]) as f:
        for r in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
            if len(r) < 11 or not r[0][:1].isdigit():
                continue
            t = int(r[0])
            t = t // 1000 if t > 10 ** 14 else t
            h = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H")
            rows.append([h, coin, f"{float(r[4]):.6g}", f"{float(r[7]):.6g}", f"{float(r[10]):.6g}"])
    return rows


def job(coin, period):
    """period: 'YYYY-MM' (monthly file) or 'YYYY-MM-DD' (daily file)."""
    sym = f"{coin}USDT"
    kind = "monthly" if len(period) == 7 else "daily"
    raw = get(f"{BASE}/{kind}/klines/{sym}/1h/{sym}-1h-{period}.zip")
    return coin, period, (parse(raw, coin) if raw else None)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--budget", type=float, default=30, help="minutes after which no new download starts")
    a = ap.parse_args(argv)
    t0 = time.time()
    deadline = t0 + a.budget * 60
    os.makedirs(OUT, exist_ok=True)
    store.fold(OUT, FIELDS)
    today = dt.datetime.now(dt.timezone.utc).date()
    yday = today - dt.timedelta(days=1)
    recent = (yday - dt.timedelta(days=3)).isoformat()  # a 404 this recent may just be "not published yet"
    meta_p = os.path.join(OUT, "_meta.json")
    try:
        meta = json.load(open(meta_p))
    except (OSError, ValueError):
        meta = {}
    done = set(map(tuple, meta.get("done", [])))      # (coin, period) fetched
    absent = set(map(tuple, meta.get("absent", [])))  # (coin, period) with no file (before a listing)
    skip = done | absent

    def days_of(ym):
        y, m = int(ym[:4]), int(ym[5:])
        d = dt.date(y, m, 1)
        while d.month == m and d <= yday:
            yield d.isoformat()
            d += dt.timedelta(days=1)

    def run(jobs):
        out = []
        with cf.ThreadPoolExecutor(a.workers) as ex:
            futs = [ex.submit(job, *jb) for jb in jobs if time.time() < deadline]
            for fu in cf.as_completed(futs):
                try:
                    out.append(fu.result())
                except Exception as e:
                    print("fail:", e, flush=True)
        return out

    months, days = [], []
    for c in COINS:
        y, m = START.year, START.month
        while (y, m) <= (yday.year, yday.month):
            ym = f"{y:04d}-{m:02d}"
            if (y, m) < (today.year, today.month):
                if (c, ym) not in skip:
                    months.append((c, ym))
            else:
                days += [(c, d) for d in days_of(ym) if (c, d) not in skip]
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    print(f"to fetch: {len(months)} monthly and {len(days)} daily files", flush=True)
    new, n_files, no_month = [], 0, []
    for c, period, rows in run(months):
        if rows is None:  # not out yet, or no trading that month: its days
            days += [(c, d) for d in days_of(period) if (c, d) not in skip]
            no_month.append((c, period))
        else:
            new += rows
            done.add((c, period))
            n_files += 1
    for c, period, rows in run(days):
        if rows is None:
            if period < recent:
                absent.add((c, period))
        else:
            new += rows
            done.add((c, period))
            n_files += 1
    for c, ym in no_month:  # a month with no file on any day (before the listing): skip it from now on
        if all((c, d) in absent for d in days_of(ym)):
            absent.add((c, ym))
    if new:
        store.write_rows(OUT, FIELDS, new)
    meta.update({"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files": n_files, "rows": len(new),
                 "done": sorted(done), "absent": sorted(absent)})
    with open(meta_p, "w") as f:
        json.dump(meta, f, separators=(",", ":"))
    print(f"done: {n_files} files, {len(new)} rows in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
