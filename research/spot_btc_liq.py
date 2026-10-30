#!/usr/bin/env python3
"""Spot order books for BTC, to test whether CoinKarma's LIQ (computed on spot books) is matched better by
spot depth than by our Binance perp depth (research/probe_spot_history.py found two sources a runner can
download):
  okx   OKX spot BTC-USDT, daily 5000-level books (snapshot + one update a second), replayed every 30 s
        (scripts/okx_book_history.py's replay), hourly means
  gate  Gate spot BTC_USDT, one file an hour starting with the full book: that snapshot, once an hour
Notional US$ within 1/2/5/10% of the mid (empty where the book does not reach).
Output: research/spot/btc_okx.csv and research/spot/btc_gate.csv (hour, bid1, ask1, ... bid10, ask10, reach).
Usage: spot_btc_liq.py [--first 2026-09-09] [--last 2026-10-29]"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import okx_book_history as OH  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "spot")
UA = OH.UA
BANDS = (1, 2, 5, 10)
UTC = dt.timezone.utc


def okx_urls(first, last):
    out = {}
    d = first
    while d <= last:
        e = min(last, d + dt.timedelta(days=20))
        b_ms = int(dt.datetime.combine(d, dt.time(), UTC).timestamp() * 1000)
        e_ms = int(dt.datetime.combine(e, dt.time(), UTC).timestamp() * 1000)
        j = OH.get_json(f"https://www.okx.com/api/v5/public/market-data-history?module=5&instType=SPOT&instIdList=BTC-USDT"
                        f"&dateAggrType=daily&begin={b_ms}&end={e_ms}")
        for det in ((j or {}).get("data") or [{}])[0].get("details") or []:
            for g in det.get("groupDetails") or []:
                out[dt.datetime.fromtimestamp(int(g["dateTs"]) / 1000, UTC).date().isoformat()] = g["url"]
        d = e + dt.timedelta(days=1)
        time.sleep(0.3)
    return out


def depth_bands(bids, asks):
    """bids/asks {price: size BTC} -> notional within each band, and the reach (% both sides)"""
    if not bids or not asks:
        return None
    bb, ba = max(bids), min(asks)
    if not (bb > 0 and ba > bb):
        return None
    mid = (bb + ba) / 2
    reach = min((1 - min(bids) / mid) * 100, (max(asks) / mid - 1) * 100)
    out = {"reach": reach}
    for k in BANDS:
        if reach >= k:
            out[f"bid{k}"] = sum(p * q for p, q in bids.items() if p >= mid * (1 - k / 100))
            out[f"ask{k}"] = sum(p * q for p, q in asks.items() if p <= mid * (1 + k / 100))
    return out


def gate_hour(h):
    """The full book at the start of hour h (a datetime), from Gate's hourly file."""
    url = f"https://download.gatedata.org/spot/orderbooks/{h:%Y%m}/BTC_USDT-{h:%Y%m%d%H}.csv.gz"
    for k in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r:
                g = gzip.GzipFile(fileobj=r)
                bids, asks, t0 = {}, {}, None
                for line in io.TextIOWrapper(g, encoding="utf-8"):
                    f = line.rstrip("\n").split(",")
                    if len(f) < 5:
                        continue
                    if t0 is None:
                        t0 = f[0]
                    if f[0] != t0 or f[2] != "set":
                        break
                    p, q = float(f[3]), float(f[4])
                    if q > 0:
                        (bids if f[1] == "2" else asks)[p] = q
                return h, depth_bands(bids, asks), len(bids) + len(asks)
        except Exception as e:  # noqa: PERF203
            err = str(e)[:120]
            time.sleep(3 * (k + 1))
    return h, None, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", default="2026-09-09")
    ap.add_argument("--last", default="2026-10-29")
    a = ap.parse_args()
    first, last = dt.date.fromisoformat(a.first), dt.date.fromisoformat(a.last)
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    report = {"run_at": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    # Gate: hourly snapshots of the full book
    hours = [dt.datetime.combine(first, dt.time(), UTC) + dt.timedelta(hours=i) for i in range(((last - first).days + 1) * 24)]
    rows, fails = [], []
    with cf.ThreadPoolExecutor(16) as ex:
        for h, s, info in ex.map(gate_hour, hours):
            if s:
                rows.append([h.strftime("%Y-%m-%dT%H")] + [f"{s.get(f'{s_}{k}', ''):.6g}" if s.get(f"{s_}{k}") is not None else ""
                                                           for k in BANDS for s_ in ("bid", "ask")] + [f"{s['reach']:.3g}"])
            else:
                fails.append([h.strftime("%Y-%m-%dT%H"), str(info)])
    with open(os.path.join(OUT, "btc_gate.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hour"] + [f"{s_}{k}" for k in BANDS for s_ in ("bid", "ask")] + ["reach"])
        w.writerows(sorted(rows))
    report["gate"] = {"hours": len(rows), "fails": fails[:20], "n_fail": len(fails), "secs": round(time.time() - t0)}
    print("gate", report["gate"], flush=True)
    # OKX: replayed 5000-level books, 30-second samples, hourly means (bands within the 5000 levels' reach)
    t1 = time.time()
    urls = okx_urls(first, last)
    out = []
    with cf.ProcessPoolExecutor(4) as ex:
        futs = [ex.submit(OH.replay, "BTC", day, url, 1.0) for day, url in sorted(urls.items())]
        for fu in cf.as_completed(futs):
            try:
                _, day, r = fu.result()
                out += r
                print("okx", day, len(r), f"{time.time() - t1:.0f}s", flush=True)
            except Exception as e:
                print("okx failed", e, flush=True)
    with open(os.path.join(OUT, "btc_okx.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(OH.FIELDS)
        w.writerows(sorted(out))
    report["okx"] = {"days": len(urls), "hours": len(out), "secs": round(time.time() - t1)}
    print("okx", report["okx"], flush=True)
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
