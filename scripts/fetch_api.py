#!/usr/bin/env python3
"""Daily net flows from the official cryptoetf.today REST API (needs the repository
secret CRYPTOETF_API_KEY; skipped quietly without it).

GET https://api.cryptoetf.today/api/v1/flows/{asset}  ->  {symbol, asset, windowDays,
days:[{date, netFlowUsdM}], updatedAt}. The free tier returns the latest 30 days of
asset totals; a key with a longer windowDays fills in more history automatically.
History is merged into data/flows_api/<SYM>.json on every run. The key never leaves
the environment variable: nothing here writes it to disk or prints it."""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://api.cryptoetf.today/api/v1"
CODES = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp", "HYPE": "hyp", "DOGE": "doge", "LINK": "link",
         "AVAX": "avax", "HBAR": "hbar", "LTC": "ltc", "BNB": "bnb", "DOT": "dot", "SUI": "sui", "NEAR": "near",
         "TRX": "trx", "ZEC": "zec"}
MIN_HOURS_BETWEEN = 20  # the source updates once a day, before the US open
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "flows_api")
UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows)"


def get(path, key):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                                                       "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        limits = {h: r.headers.get(h) for h in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")}
        return json.loads(r.read().decode("utf-8")), limits


def main():
    key = os.environ.get("CRYPTOETF_API_KEY", "").strip()
    os.makedirs(OUT, exist_ok=True)
    rep_path = os.path.join(OUT, "_report.json")
    try:
        prev = json.load(open(rep_path))
    except (OSError, ValueError):
        prev = {}
    now = dt.datetime.now(dt.timezone.utc)
    if not key:
        print("CRYPTOETF_API_KEY not set; skipping the REST API")
        return
    last = prev.get("last_success")
    if last and (now - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds() < MIN_HOURS_BETWEEN * 3600:
        print("fetched less than", MIN_HOURS_BETWEEN, "hours ago; skipping")
        return
    report = {"run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "assets": {}, "last_success": prev.get("last_success")}
    ok = 0
    for sym, code in CODES.items():
        try:
            data, limits = get(f"/flows/{code}", key)
            rows = [{"date": str(d["date"])[:10], "flow": float(d["netFlowUsdM"])}
                    for d in data.get("days", []) if d.get("date") and isinstance(d.get("netFlowUsdM"), (int, float))]
            path = os.path.join(OUT, f"{sym}.json")
            try:
                store = json.load(open(path))
            except (OSError, ValueError):
                store = {"asset": sym, "source": BASE + "/flows/" + code, "unit": "USD millions", "rows": []}
            by_date = {r["date"]: r for r in store["rows"]}
            for r in rows:
                by_date[r["date"]] = r
            merged = [by_date[k] for k in sorted(by_date)]
            if merged != store["rows"] or store.get("windowDays") != data.get("windowDays"):
                store.update(rows=merged, windowDays=data.get("windowDays"), updatedAt=data.get("updatedAt"))
                json.dump(store, open(path, "w"), separators=(",", ":"))
            report["assets"][sym] = {"window_days": data.get("windowDays"), "returned": len(rows), "stored": len(merged),
                                     "first": merged[0]["date"] if merged else None, "last": merged[-1]["date"] if merged else None,
                                     "rate_limit": limits}
            ok += 1
        except urllib.error.HTTPError as e:
            report["assets"][sym] = {"error": f"HTTP {e.code}"}
            if e.code in (401, 403):
                break  # bad key: stop instead of burning requests
            if e.code == 429:
                time.sleep(20)
        except Exception as e:  # keep going for the other assets
            report["assets"][sym] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        time.sleep(1.2)
    if ok:
        report["last_success"] = report["run_at"]
    json.dump(report, open(rep_path, "w"), indent=1, sort_keys=True)
    print(json.dumps({k: {kk: v.get(kk) for kk in ("window_days", "stored", "last", "error")} for k, v in report["assets"].items()}))


if __name__ == "__main__":
    main()
