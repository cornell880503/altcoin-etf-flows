#!/usr/bin/env python3
"""Daily market cap and price of every asset with a free daily market cap at Coin Metrics
(community API, no key), from 2021-06-01. Feeds the altcoin market-cap series of the leverage
radar (ex BTC, ETH, stablecoins: the build step makes that cut). The free CoinGecko API stops at
365 days and its total-market-cap history is paid only (research/probe_oi).

Output: data/oi/cm/<YYYY-MM>.csv.gz (date, asset, mcap, price; mcap = CapMrktEstUSD, else
CapMrktCurUSD) and data/oi/cm/_meta.json. Incremental: later runs refetch from five days before
the newest stored day (Coin Metrics revises recent values).
Also data/oi/stablecoins.json: total stablecoin supply per day (DefiLlama), for the coverage check.
"""
import csv
import datetime as dt
import gzip
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "oi", "cm")
BASE = "https://community-api.coinmetrics.io/v4"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
START = "2021-06-01"
FIELDS = ["date", "asset", "mcap", "price"]


def jget(url, tries=4):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(8 * (k + 1))
                continue
            if e.code in (400, 401, 403, 404):
                raise
        except Exception as e:
            last = e
        time.sleep(3 * (k + 1))
    raise last


def read_month(ym):
    p = os.path.join(OUT, f"{ym}.csv.gz")
    if not os.path.exists(p):
        return []
    with gzip.open(p, "rt", newline="") as f:
        r = csv.reader(f)
        next(r, None)
        return [row for row in r if row]


def write_month(ym, rows):
    rows = sorted({(r[0], r[1]): r for r in rows}.values())
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIELDS)
    w.writerows(rows)
    with open(os.path.join(OUT, f"{ym}.csv.gz"), "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as g:
            g.write(buf.getvalue().encode())


def g6(s):
    try:
        v = float(s)
        return f"{v:.6g}" if v == v else ""
    except (TypeError, ValueError):
        return ""


def main():
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    months = sorted(fn[:7] for fn in os.listdir(OUT) if fn.endswith(".csv.gz"))
    start = START
    if months:
        last = max(r[0] for r in read_month(months[-1]))
        start = (dt.date.fromisoformat(last) - dt.timedelta(days=5)).isoformat()

    cat = jget(f"{BASE}/catalog-v2/asset-metrics?metrics=CapMrktEstUSD,CapMrktCurUSD&page_size=10000")
    assets = sorted({a["asset"] for a in cat.get("data", [])
                     if any(f.get("frequency") == "1d" for m in a.get("metrics", []) for f in m.get("frequencies", []))})
    print(f"assets with a daily market cap: {len(assets)}; fetching from {start}", flush=True)

    new, errors, reqs = {}, {}, 0
    for i in range(0, len(assets), 10):
        batch = assets[i:i + 10]
        q = urllib.parse.urlencode({"assets": ",".join(batch), "metrics": "CapMrktEstUSD,CapMrktCurUSD,PriceUSD",
                                    "frequency": "1d", "start_time": start, "page_size": 10000,
                                    "ignore_forbidden_errors": "true", "ignore_unsupported_errors": "true"})
        url = f"{BASE}/timeseries/asset-metrics?{q}"
        try:
            while url:
                j = jget(url)
                reqs += 1
                for r in j.get("data", []):
                    mc = g6(r.get("CapMrktEstUSD")) or g6(r.get("CapMrktCurUSD"))
                    if not mc:
                        continue
                    d = r["time"][:10]
                    new.setdefault(d[:7], []).append([d, r["asset"], mc, g6(r.get("PriceUSD"))])
                url = j.get("next_page_url")
                time.sleep(0.7)  # community limit: 10 requests per 6 seconds
        except Exception as e:
            errors[",".join(batch)] = f"{type(e).__name__}: {str(e)[:120]}"
        if i % 200 == 0:
            print(f"  {i}/{len(assets)} assets, {reqs} requests, {time.time() - t0:.0f}s", flush=True)

    for ym, rows in sorted(new.items()):
        write_month(ym, read_month(ym) + rows)
    meta = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "assets": len(assets),
            "fetched_from": start, "rows_new": sum(len(v) for v in new.values()), "requests": reqs, "errors": errors}
    with open(os.path.join(OUT, "_meta.json"), "w") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
    print("meta:", meta, f"({time.time() - t0:.0f}s)", flush=True)

    try:  # total stablecoin supply, for checking the altcoin series against CoinGecko's total
        j = jget("https://stablecoins.llama.fi/stablecoincharts/all")
        st = {dt.datetime.fromtimestamp(int(r["date"]), dt.timezone.utc).date().isoformat():
              round((r.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0) for r in j}
        with open(os.path.join(ROOT, "data", "oi", "stablecoins.json"), "w") as f:
            json.dump(st, f, separators=(",", ":"), sort_keys=True)
    except Exception as e:
        print("stablecoins failed:", e, flush=True)


if __name__ == "__main__":
    main()
