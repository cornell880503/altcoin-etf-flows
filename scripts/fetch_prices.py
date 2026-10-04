#!/usr/bin/env python3
"""Daily closes (4pm ET) of each coin's reference ETF, for flow-vs-price statistics.
Primary: Yahoo Finance chart API; fallback: Stooq CSV. Written to data/prices/<TICKER>.json
as {ticker, source, dates[], close[]}; history is merged so it only grows.
The dashboard's daily job cross-checks the latest closes with Interactive Brokers."""
import csv
import datetime as dt
import io
import json
import os
import time
import urllib.request

TICKERS = ["IBIT", "ETHA", "BSOL", "XRPC", "THYP", "LTCC", "HBR", "TRXS", "GSUI", "GLNK", "GDOG",
           "VAVX", "TDOT", "NRR", "ZCSH", "VBNB"]
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "prices")


def get(url, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def yahoo(t):
    j = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=2y&interval=1d&includeAdjustedClose=false",
                       "application/json"))
    res = j["chart"]["result"][0]
    tz = res["meta"].get("gmtoffset", -14400)
    out = {}
    for ts, c in zip(res.get("timestamp", []), res["indicators"]["quote"][0].get("close", [])):
        if c is None:
            continue
        d = dt.datetime.fromtimestamp(ts + tz, dt.timezone.utc).date().isoformat()
        out[d] = round(float(c), 4)
    return out


def stooq(t):
    txt = get(f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=d", "text/csv")
    out = {}
    for r in csv.DictReader(io.StringIO(txt)):
        try:
            out[r["Date"]] = round(float(r["Close"]), 4)
        except (KeyError, ValueError):
            pass
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tickers": {}}
    for t in TICKERS:
        got, src, errs = {}, None, []
        for name, fn in (("yahoo", yahoo), ("stooq", stooq)):
            try:
                got = fn(t)
                if got:
                    src = name
                    break
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__} {str(e)[:120]}")
            time.sleep(1)
        path = os.path.join(OUT, f"{t}.json")
        try:
            store = json.load(open(path))
            hist = dict(zip(store["dates"], store["close"]))
        except (OSError, ValueError, KeyError):
            hist = {}
        today = dt.date.today().isoformat()
        for d, c in got.items():
            if d < today or dt.datetime.now(dt.timezone.utc).hour >= 21:  # skip today's live bar before the close
                hist[d] = c
        if hist:
            ds = sorted(hist)
            json.dump({"ticker": t, "source": src or "stored", "dates": ds, "close": [hist[d] for d in ds]},
                      open(path, "w"), separators=(",", ":"))
        report["tickers"][t] = {"source": src, "n": len(hist), "last": max(hist) if hist else None, "errors": errs}
        time.sleep(1)
    json.dump(report, open(os.path.join(OUT, "_report.json"), "w"), indent=1)
    print(json.dumps(report["tickers"], indent=0)[:3000])


if __name__ == "__main__":
    main()
