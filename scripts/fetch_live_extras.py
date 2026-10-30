#!/usr/bin/env python3
"""Hourly live values for the indicators that have a live source reachable from a GitHub runner (the
order-book family is in fetch_book_live.py; ETF flows, CFTC positions, stablecoins, ILI and the App Store
rank are daily or weekly by nature). Output: data/ck/live/extra.json, read by ck_live.py at every build.

  funding   OKX BTC-USDT-SWAP (current period, 8h) and Hyperliquid BTC (hourly x 8), open-interest weighted
            (Binance's own rate is not reachable live; its monthly file comes after the month)
  basis     OKX BTC-USD front quarterly (14+ days left), annualized x 360 / days against OKX's BTC-USD index
  usdcprem  Binance spot USDCUSDT 1-hour candles (data-api.binance.vision answers runners): the last hour
            and today's mean
  cvd       Binance spot BTCUSDT 1-hour candles: taker buys minus taker sells, the last 60 days
  bfxprem   Bitfinex BTCUSD against Coinbase BTC-USD, 1-hour closes (the last 48 hours)
  bfxlongs  Bitfinex BTCUSD margin longs, the latest minute
  dvol      Deribit DVOL (1-hour) and the option chain's 30-day 25-delta skew and ATM vols
Every part is optional: a failure is recorded in the file and the rest go on.
"""
import datetime as dt
import json
import math
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "ck", "live", "extra.json")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
UTC = dt.timezone.utc


def get(url, data=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    last = None
    for k in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(2 + 3 * k)
    raise last


def hour_of(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%dT%H")


def done_hour():
    return (dt.datetime.now(UTC) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H")


def funding():
    okx = get("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP")["data"][0]
    oi = get("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP")["data"][0]
    meta, ctxs = get("https://api.hyperliquid.xyz/info", data={"type": "metaAndAssetCtxs"})
    i = next(k for k, u in enumerate(meta["universe"]) if u["name"] == "BTC")
    hl = ctxs[i]
    parts = [("okx", float(okx["fundingRate"]) * 100, float(oi["oiCcy"])),
             ("hyperliquid", float(hl["funding"]) * 8 * 100, float(hl["openInterest"]))]
    w = sum(o for _, _, o in parts)
    return {"h": done_hour(), "v": sum(r * o for _, r, o in parts) / w,
            "parts": {n: {"pct8h": round(r, 5), "oi_btc": round(o)} for n, r, o in parts}}


def basis():
    now_ms = time.time() * 1000
    futs = get("https://www.okx.com/api/v5/public/instruments?instType=FUTURES&instFamily=BTC-USD")["data"]
    # quarterlies only, like Binance's COIN-M history (OKX also lists weeklies and monthlies)
    cands = [(int(f["expTime"]), f["instId"]) for f in futs
             if f.get("alias") in ("quarter", "next_quarter") and int(f["expTime"]) - now_ms >= 14 * 86400000]
    exp, inst = min(cands)
    last = float(get(f"https://www.okx.com/api/v5/market/ticker?instId={inst}")["data"][0]["last"])
    idx = float(get("https://www.okx.com/api/v5/market/index-tickers?instId=BTC-USD")["data"][0]["idxPx"])
    left = (exp - now_ms) / 86400000
    prem = last / idx - 1
    return {"h": done_hour(), "v": prem * 360 / left * 100, "basis_pct": prem * 100, "contract": inst, "days_left": round(left, 1)}


def bklines(sym, limit=1000, end=None):
    q = f"symbol={sym}&interval=1h&limit={limit}" + (f"&endTime={end}" if end else "")
    return get(f"https://data-api.binance.vision/api/v3/klines?{q}")


def usdcprem():
    k = [r for r in bklines("USDCUSDT", 30) if int(r[6]) < time.time() * 1000]  # closed candles
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    td = [float(r[4]) for r in k if hour_of(r[0]).startswith(today)]
    return {"h": hour_of(k[-1][0]), "v": (float(k[-1][4]) - 1) * 100, "today_mean": (sum(td) / len(td) - 1) * 100 if td else None,
            "today_hours": len(td)}


def cvd():
    rows, end = [], None
    for _ in range(2):
        k = bklines("BTCUSDT", 1000, end)
        if not k:
            break
        rows = k + rows
        end = int(k[0][0]) - 1
    rows = sorted({int(r[0]): r for r in rows if int(r[6]) < time.time() * 1000}.values(), key=lambda r: int(r[0]))[-1440:]
    delta = [round((2 * float(r[10]) - float(r[7])) / 1e6, 3) for r in rows]  # US$ millions: taker buys - taker sells
    return {"h0": hour_of(rows[0][0]), "h": hour_of(rows[-1][0]), "delta": delta, "close": [round(float(r[4])) for r in rows]}


def bfxprem():
    start = int((time.time() - 50 * 3600) * 1000)
    b = get(f"https://api-pub.bitfinex.com/v2/candles/trade:1h:tBTCUSD/hist?limit=60&sort=1&start={start}")
    q = urllib.parse.urlencode({"granularity": 3600, "start": dt.datetime.fromtimestamp(start / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
    c = get(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?{q}")
    bh = {hour_of(r[0]): float(r[2]) for r in b}
    ch = {hour_of(int(r[0]) * 1000): float(r[4]) for r in c}
    now_h = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H")
    hrs = sorted(h for h in set(bh) & set(ch) if h < now_h)
    prem = {h: (bh[h] / ch[h] - 1) * 100 for h in hrs}
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    td = [v for h, v in prem.items() if h.startswith(today)]
    return {"h": hrs[-1], "v": prem[hrs[-1]], "hourly": {h: round(v, 4) for h, v in prem.items()},
            "today_mean": sum(td) / len(td) if td else None, "today_hi": max(td) if td else None, "today_lo": min(td) if td else None}


def bfxlongs():
    t, v = get("https://api-pub.bitfinex.com/v2/stats1/pos.size:1m:tBTCUSD:long/last")
    return {"h": hour_of(t), "v": v / 1000}


def _bs_delta(S, K, T, iv, call):
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    nd = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return nd if call else nd - 1


def dvol():
    b = "https://www.deribit.com/api/v2/public"
    now_ms = int(time.time() * 1000)
    q = urllib.parse.urlencode({"currency": "BTC", "start_timestamp": now_ms - 6 * 3600000, "end_timestamp": now_ms, "resolution": 3600})
    data = get(f"{b}/get_volatility_index_data?{q}")["result"]["data"]
    t, o, h, lo, c = data[-1]
    chain = get(f"{b}/get_book_summary_by_currency?currency=BTC&kind=option")["result"]
    exps = {}
    for x in chain:
        try:
            _, ex, k, cp = x["instrument_name"].split("-")
            exp = dt.datetime.strptime(ex, "%d%b%y").replace(hour=8, tzinfo=UTC)
        except ValueError:
            continue
        T = (exp.timestamp() * 1000 - now_ms) / (365.25 * 86400000)
        S, iv = x.get("underlying_price"), x.get("mark_iv")
        if not S or not iv or T <= 2 / 365:
            continue
        exps.setdefault(round(T, 5), []).append((float(k), cp, iv, _bs_delta(S, float(k), T, iv / 100, cp == "C"), S))
    ten = {}
    for T, opts in exps.items():
        S = opts[0][4]
        calls = [(abs(x[3] - 0.25), x[2]) for x in opts if x[1] == "C"]
        puts = [(abs(x[3] + 0.25), x[2]) for x in opts if x[1] == "P"]
        atm = sorted(opts, key=lambda x: abs(x[0] - S))[:2]
        if calls and puts:
            ten[T] = {"skew25": min(puts)[1] - min(calls)[1], "atm": sum(x[2] for x in atm) / len(atm)}

    def at(days, key):
        ts, x = sorted(ten), days / 365.25
        lo_, hi_ = [t for t in ts if t <= x], [t for t in ts if t >= x]
        if not lo_ or not hi_:
            return None
        a, b2 = lo_[-1], hi_[0]
        if a == b2:
            return ten[a][key]
        w = (x - a) / (b2 - a)
        return ten[a][key] * (1 - w) + ten[b2][key] * w
    return {"h": hour_of(t), "v": c, "skew25_30d": at(30, "skew25"), "atm_30d": at(30, "atm"), "atm_90d": at(90, "atm"), "atm_180d": at(180, "atm")}


PARTS = {"funding": funding, "basis": basis, "usdcprem": usdcprem, "cvd": cvd, "bfxprem": bfxprem, "bfxlongs": bfxlongs, "dvol": dvol}


def main():
    try:
        out = json.load(open(OUT))
    except (OSError, ValueError):
        out = {}
    errors = {}
    for name, fn in PARTS.items():
        try:
            out[name] = fn()
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {str(e)[:160]}"
            traceback.print_exc()
    out["asof"] = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["errors"] = errors
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print("extras:", {k: (v.get("h") if isinstance(v, dict) else v) for k, v in out.items() if k in PARTS}, "errors:", errors, flush=True)


if __name__ == "__main__":
    main()
