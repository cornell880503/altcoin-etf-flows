#!/usr/bin/env python3
"""Live order books for the hourly LIQ (scripts/ck_liq.py). The LIQ history uses Binance's daily
bookDepth files, which Binance publishes the next day and which no live API gives (Binance and Bybit
answer GitHub runners with 451/403; research/probe_live_book.py). So each run takes a few snapshots of
three books that do answer and reach 5% of the price (research/probe_live/report.json):
  okx   OKX USDT perpetual swaps, books-full (up to 5000 levels a side; BTC and ETH reach ~1-2%)
  cb    Coinbase spot, level 2 (the whole book; no TRX or GRAM)
  hl    Hyperliquid perps, l2Book aggregated to 2 significant figures (20 wide levels a side)
and the last hours' 1-hour closes of the OKX swaps (prices for live resonance returns).
ck_liq.py maps each book's imbalance onto Binance's on the hours both have, and uses the map for the
hours Binance has not published yet.

Rows, one gzip CSV per UTC hour in data/ck/live/hours/ (YYYY-MM-DDTHH.csv.gz):
  hour, key (venue:COIN), n (snapshots), bid1, ask1, bid2, ask2, bid5, ask5, bidR, askR, reach
notional US$ within 1/2/5% of the mid (empty where the book does not reach), and within R =
min(5%, the depth the book reaches on both sides), the mean over the hour's snapshots; and
  hour, px:COIN, 1, close   (the OKX swap's 1-hour candle that opens at that hour)
Hour files older than 30 days are deleted.
Usage: fetch_book_live.py [--rounds 3] [--gap 120]
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import glob
import gzip
import io
import json
import os
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "ck", "live")
HOURS = os.path.join(OUT, "hours")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC", "BCH", "SUI", "HYPE", "UNI",
         "AAVE", "NEAR", "GRAM", "ATOM", "APT", "ARB", "OP", "ZEC"]
CB_NONE = {"TRX", "GRAM"}  # not listed on Coinbase (research/probe_live)
FIELDS = ["hour", "key", "n", "bid1", "ask1", "bid2", "ask2", "bid5", "ask5", "bidR", "askR", "reach"]
VALS = FIELDS[3:]
UTC = dt.timezone.utc


def get(url, data=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    for k in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(2 + 3 * k)
        except Exception:
            time.sleep(2 + 3 * k)
    return None


def depth(bids, asks):
    """[(price, notional)] each side -> notional within 1/2/5% of the mid (None where the book stops short)
    and within R = min(5%, the depth reached on both sides)."""
    if not bids or not asks:
        return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    if not (bb > 0 and ba > bb):
        return None
    mid = (bb + ba) / 2
    reach = min((1 - min(p for p, _ in bids) / mid) * 100, (max(p for p, _ in asks) / mid - 1) * 100)
    out = {}
    for k in (1, 2, 5):
        if reach >= k:
            out[f"bid{k}"] = sum(v for p, v in bids if p >= mid * (1 - k / 100))
            out[f"ask{k}"] = sum(v for p, v in asks if p <= mid * (1 + k / 100))
    r = min(5.0, reach)
    out["bidR"] = sum(v for p, v in bids if p >= mid * (1 - r / 100))
    out["askR"] = sum(v for p, v in asks if p <= mid * (1 + r / 100))
    out["reach"] = r
    return out


OKX_CT = {}


def okx(c):
    inst = f"{c}-USDT-SWAP"
    if inst not in OKX_CT:
        return None
    j = get(f"https://www.okx.com/api/v5/market/books-full?instId={inst}&sz=5000")
    d = (j or {}).get("data") or []
    if not d:
        return None
    cv = OKX_CT[inst]
    return depth([(float(p), float(p) * float(s) * cv) for p, s, *_ in d[0]["bids"]],
                 [(float(p), float(p) * float(s) * cv) for p, s, *_ in d[0]["asks"]])


def coinbase(c):
    if c in CB_NONE:
        return None
    j = get(f"https://api.exchange.coinbase.com/products/{c}-USD/book?level=2")
    if not j or "bids" not in j:
        return None
    return depth([(float(p), float(p) * float(s)) for p, s, *_ in j["bids"]], [(float(p), float(p) * float(s)) for p, s, *_ in j["asks"]])


def hyperliquid(c):
    j = get("https://api.hyperliquid.xyz/info", data={"type": "l2Book", "coin": c, "nSigFigs": 2})
    if not j or not j.get("levels"):
        return None
    b, a = j["levels"]
    return depth([(float(x["px"]), float(x["px"]) * float(x["sz"])) for x in b], [(float(x["px"]), float(x["px"]) * float(x["sz"])) for x in a])


VENUES = {"okx": (okx, 0.25), "cb": (coinbase, 0.15), "hl": (hyperliquid, 0.15)}


def venue_round(name):
    fn, pause = VENUES[name]
    out = {}
    for c in COINS:
        try:
            r = fn(c)
        except Exception:
            r = None
        if r:
            out[c] = r
        time.sleep(pause)
    return name, out


def snapshot():
    """One snapshot of every venue (venues in parallel, coins one after another)."""
    with cf.ThreadPoolExecutor(len(VENUES)) as ex:
        return dict(ex.map(venue_round, VENUES))


def okx_closes():
    """The OKX swaps' last confirmed 1-hour candles: {(hour, coin): close}."""
    out = {}
    for c in COINS:
        j = get(f"https://www.okx.com/api/v5/market/candles?instId={c}-USDT-SWAP&bar=1H&limit=100")
        for r in (j or {}).get("data") or []:
            if len(r) >= 9 and r[8] == "1":  # confirmed (closed) candle
                h = dt.datetime.fromtimestamp(int(r[0]) / 1000, UTC).strftime("%Y-%m-%dT%H")
                out[(h, c)] = float(r[4])
        time.sleep(0.12)
    return out


def read_hour(h):
    p = os.path.join(HOURS, f"{h}.csv.gz")
    if not os.path.exists(p):
        return {}
    with gzip.open(p, "rt", newline="") as f:
        r = csv.reader(f)
        next(r, None)
        return {row[1]: row for row in r if row}


def write_hour(h, rows):
    os.makedirs(HOURS, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIELDS)
    for k in sorted(rows):
        w.writerow(rows[k])
    p = os.path.join(HOURS, f"{h}.csv.gz")
    with open(p + ".tmp", "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as g:
            g.write(buf.getvalue().encode())
    os.replace(p + ".tmp", p)


def g6(v):
    return "" if v is None else f"{v:.6g}"


def merge(acc, h, key, vals):
    """Add one snapshot to the running sums of (hour, key)."""
    a = acc.setdefault((h, key), {"n": 0, **{f: [0.0, 0] for f in VALS}})
    a["n"] += 1
    for f in VALS:
        v = vals.get(f)
        if v is not None:
            a[f][0] += v
            a[f][1] += 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap", type=float, default=120, help="seconds between snapshot rounds")
    a = ap.parse_args(argv)
    t0 = time.time()
    j = get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    for r in (j or {}).get("data") or []:
        OKX_CT[r["instId"]] = float(r.get("ctVal") or 0)
    acc, got = {}, {}
    t_round = time.time()
    for k in range(a.rounds):
        if k:
            time.sleep(max(0.0, a.gap - (time.time() - t_round)))
        t_round = time.time()
        h = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H")
        snap = snapshot()
        for v, coins in snap.items():
            got[v] = max(got.get(v, 0), len(coins))
            for c, vals in coins.items():
                merge(acc, h, f"{v}:{c}", vals)
        print(f"round {k + 1}: {h} " + ", ".join(f"{v} {len(cs)}" for v, cs in snap.items()) + f" ({time.time() - t_round:.0f}s)", flush=True)
    # merge into the hour files (running means)
    by_hour = {}
    for (h, key), s in acc.items():
        by_hour.setdefault(h, {})[key] = s
    for h, new in by_hour.items():
        rows = read_hour(h)
        for key, s in new.items():
            old = rows.get(key)
            n_old = int(old[2]) if old else 0
            out = [h, key, str(n_old + s["n"])]
            for i, f in enumerate(VALS):
                tot, cnt = s[f]
                ov = old[3 + i] if old and len(old) > 3 + i else ""
                if ov != "" and cnt:
                    out.append(g6((float(ov) * n_old + tot) / (n_old + cnt)))
                elif cnt:
                    out.append(g6(tot / cnt))
                else:
                    out.append(ov)
            rows[key] = out
        write_hour(h, rows)
    # prices: confirmed 1-hour closes
    px = okx_closes()
    for (h, c), close in px.items():
        rows = read_hour(h)
        rows[f"px:{c}"] = [h, f"px:{c}", "1", g6(close)] + [""] * (len(FIELDS) - 4)
        write_hour(h, rows)
    # prune
    cut = (dt.datetime.now(UTC) - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H")
    for p in glob.glob(os.path.join(HOURS, "*.csv.gz")):
        if os.path.basename(p)[:13] < cut:
            os.remove(p)
    meta = {"run_at": dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "rounds": a.rounds, "books": got,
            "hours": sorted(by_hour), "closes": len(px), "secs": round(time.time() - t0)}
    mp = os.path.join(OUT, "_meta.json")
    try:
        old = json.load(open(mp))
    except (OSError, ValueError):
        old = {}
    old.update(meta)
    with open(mp, "w") as f:
        json.dump(old, f, indent=1, sort_keys=True)
    print(json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
