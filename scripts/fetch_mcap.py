#!/usr/bin/env python3
"""Daily market caps for the 16 tracked assets (CoinGecko free API), used to express ETF flows
as a share of each coin's market cap. Runs at most once per 20 hours; a missing or short file is
backfilled with 365 days. data/mcap/<SYM>.json: {"id", "rows": {date: [mcap, price, volume]}}
(US$). A date's row is CoinGecko's 00:00 UTC snapshot, i.e. a few hours after the previous US
close, which is the market cap the dashboard divides that day's flow by."""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple", "HYPE": "hyperliquid", "DOGE": "dogecoin",
       "LINK": "chainlink", "AVAX": "avalanche-2", "HBAR": "hedera-hashgraph", "LTC": "litecoin", "BNB": "binancecoin",
       "DOT": "polkadot", "SUI": "sui", "NEAR": "near", "TRX": "tron", "ZEC": "zcash"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "mcap")
UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows)"
MIN_HOURS_BETWEEN = 20


def get(url):
    for k in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(60)
                continue
            raise
    raise RuntimeError("rate limited")


def merge(store, j):
    n = 0
    for key, idx in (("market_caps", 0), ("prices", 1), ("total_volumes", 2)):
        for ts, v in j.get(key, []):
            t = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc)
            if t.hour or t.minute:  # the extra "now" point: not a daily snapshot
                continue
            row = store["rows"].setdefault(t.date().isoformat(), [None, None, None])
            if v is not None:
                row[idx] = round(float(v), 6 if idx == 1 else 0)
                n += 1
    return n


def main():
    os.makedirs(OUT, exist_ok=True)
    rep_path = os.path.join(OUT, "_report.json")
    try:
        prev = json.load(open(rep_path))
    except (OSError, ValueError):
        prev = {}
    now = dt.datetime.now(dt.timezone.utc)
    last = prev.get("last_success")
    if last and (now - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds() < MIN_HOURS_BETWEEN * 3600:
        print("market caps fetched less than", MIN_HOURS_BETWEEN, "hours ago; skipping")
        return
    report = {"run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "assets": {}, "last_success": last}
    ok = 0
    for sym, cid in IDS.items():
        path = os.path.join(OUT, f"{sym}.json")
        try:
            store = json.load(open(path))
        except (OSError, ValueError):
            store = {"id": cid, "unit": "US$", "rows": {}}
        days = 365 if len(store["rows"]) < 30 else 7
        try:
            j = get(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?vs_currency=usd&days={days}&interval=daily")
            n = merge(store, j)
            store["rows"] = {k: store["rows"][k] for k in sorted(store["rows"])}
            json.dump(store, open(path, "w"), separators=(",", ":"))
            report["assets"][sym] = {"points": n, "last": max(store["rows"]) if store["rows"] else None}
            ok += 1
        except Exception as e:  # keep the other coins
            report["assets"][sym] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        time.sleep(8)
    if ok:
        report["last_success"] = report["run_at"]
    json.dump(report, open(rep_path, "w"), indent=1, sort_keys=True)
    print(json.dumps(report["assets"]))


if __name__ == "__main__":
    main()
