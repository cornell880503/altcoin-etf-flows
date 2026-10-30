#!/usr/bin/env python3
"""OKX spot order books, replayed from OKX's daily 5000-level files (snapshot + one update a second; the
same replay as scripts/okx_book_history.py, every 30 seconds, hourly means). Research input: on
2026-10-21..28 OKX's BTC spot book tracked CoinKarma's charted LIQ far better than any perp book we have
(research/spot_btc_liq.py, research/ck_chart), and CoinKarma says its LIQ is computed on spot books.
Output: research/spot/okx_<COIN>.csv (scripts/fetch_book_live.py's fields; key okxspot:COIN).
Also reports, per coin, the first day OKX has a 5000-level spot file for.
Usage: okx_spot_history.py --coins BTC,ETH [--first 2026-09-09] [--last 2026-10-29] [--workers 4]"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import okx_book_history as OH  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "spot")
UTC = dt.timezone.utc


def ms(d):
    return int(dt.datetime.combine(d, dt.time(), UTC).timestamp() * 1000)


def urls(coin, first, last, step=5):
    out, errs = {}, []
    d = first
    while d <= last:
        e = min(last, d + dt.timedelta(days=step - 1))
        j = OH.get_json(f"https://www.okx.com/api/v5/public/market-data-history?module=5&instType=SPOT&instIdList={coin}-USDT"
                        f"&dateAggrType=daily&begin={ms(d)}&end={ms(e)}")
        if not j or j.get("code") != "0":
            errs.append([d.isoformat(), (j or {}).get("msg")])
        for det in ((j or {}).get("data") or [{}])[0].get("details") or []:
            for g in det.get("groupDetails") or []:
                out[dt.datetime.fromtimestamp(int(g["dateTs"]) / 1000, UTC).date().isoformat()] = g["url"]
        d = e + dt.timedelta(days=1)
        time.sleep(0.25)
    return out, errs


def first_day(coin):
    """The earliest month (first day checked) with a 5000-level spot file, looking back to 2024."""
    found = None
    for y, m in [(2024, 1), (2024, 7), (2025, 1), (2025, 4), (2025, 7), (2025, 10), (2026, 1), (2026, 3), (2026, 5), (2026, 7), (2026, 9)]:
        d = dt.date(y, m, 1)
        u, _ = urls(coin, d, d + dt.timedelta(days=4))
        if u:
            found = min(u)
            break
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", required=True)
    ap.add_argument("--first", default="2026-09-09")
    ap.add_argument("--last", default="2026-10-29")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    first, last = dt.date.fromisoformat(a.first), dt.date.fromisoformat(a.last)
    os.makedirs(OUT, exist_ok=True)
    report = {}
    t0 = time.time()
    for coin in a.coins.split(","):
        u, errs = urls(coin, first, last)
        rows = []
        with cf.ProcessPoolExecutor(a.workers) as ex:
            futs = [ex.submit(OH.replay, coin, day, url, 1.0) for day, url in sorted(u.items())]
            for fu in cf.as_completed(futs):
                try:
                    _, day, r = fu.result()
                    rows += [[x[0], f"okxspot:{coin}"] + x[2:] for x in r]
                except Exception as e:
                    errs.append(["replay", str(e)[:120]])
        with open(os.path.join(OUT, f"okx_{coin}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(OH.FIELDS)
            w.writerows(sorted(rows))
        report[coin] = {"days": len(u), "hours": len(rows), "errors": errs[:10], "first_5000lv": first_day(coin), "secs": round(time.time() - t0)}
        print(coin, report[coin], flush=True)
    with open(os.path.join(OUT, f"report_{a.coins.replace(',', '_')}.json"), "w") as f:
        json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
