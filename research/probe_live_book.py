#!/usr/bin/env python3
"""Which live order books answer a GitHub runner, and how far from the price they reach: input for an
hourly (live) version of the LIQ copies, whose history uses Binance's daily bookDepth files (cumulative
notional within 1/2/5% of the price, published the next day). For each venue and coin: levels returned,
the farthest bid/ask from the mid (%), the notional within 1/2/5% and whether 5% is fully covered.
Output: research/probe_live/report.json.
"""
import datetime as dt
import json
import os
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "probe_live")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC", "BCH", "SUI", "HYPE", "UNI",
         "AAVE", "NEAR", "GRAM", "TON", "ATOM", "APT", "ARB", "OP", "ZEC"]
REPORT = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "venues": {}}


def get(url, data=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
            body = r.read()
            return r.status, body, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300], time.time() - t0
    except Exception as e:
        return None, str(e)[:300].encode(), time.time() - t0


def depth_stats(bids, asks):
    """bids/asks: [(price, notional US$)], any order. Cumulative notional within 1/2/5% of the mid."""
    if not bids or not asks:
        return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bb + ba) / 2
    out = {"mid": mid, "levels": [len(bids), len(asks)],
           "reach_pct": [round((min(p for p, _ in bids) / mid - 1) * 100, 2), round((max(p for p, _ in asks) / mid - 1) * 100, 2)]}
    for k in (1, 2, 5):
        out[f"bid{k}"] = round(sum(v for p, v in bids if p >= mid * (1 - k / 100)))
        out[f"ask{k}"] = round(sum(v for p, v in asks if p <= mid * (1 + k / 100)))
    out["covers5"] = out["reach_pct"][0] <= -5 and out["reach_pct"][1] >= 5
    return out


def venue(name, fn):
    res = {}
    t0 = time.time()
    for c in COINS:
        try:
            res[c] = fn(c)
        except Exception as e:
            res[c] = {"error": f"{type(e).__name__}: {str(e)[:160]}", "trace": traceback.format_exc()[-300:]}
        time.sleep(0.25)
    ok = [c for c, v in res.items() if isinstance(v, dict) and v.get("mid")]
    REPORT["venues"][name] = {"secs": round(time.time() - t0, 1), "ok": ok, "covers5": [c for c in ok if res[c].get("covers5")], "coins": res}
    print(name, "ok", len(ok), "covers5", len(REPORT["venues"][name]["covers5"]), flush=True)


# ---- OKX perpetual swaps: books-full (up to 5000 levels a side); size in contracts x ctVal
OKX_CT = {}


def okx_ct():
    st, body, _ = get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    for r in json.loads(body).get("data", []):
        OKX_CT[r["instId"]] = float(r.get("ctVal") or 0)


def okx_swap(c):
    inst = f"{c}-USDT-SWAP"
    if inst not in OKX_CT:
        return {"error": "no such swap"}
    st, body, secs = get(f"https://www.okx.com/api/v5/market/books-full?instId={inst}&sz=5000")
    if st != 200:
        return {"status": st, "body": body.decode("utf-8", "replace")[:200]}
    d = json.loads(body)["data"][0]
    cv = OKX_CT[inst]
    bids = [(float(p), float(p) * float(s) * cv) for p, s, *_ in d["bids"]]
    asks = [(float(p), float(p) * float(s) * cv) for p, s, *_ in d["asks"]]
    out = depth_stats(bids, asks) or {}
    out.update({"status": st, "secs": round(secs, 2), "bytes": len(body)})
    return out


def okx_spot(c):
    st, body, secs = get(f"https://www.okx.com/api/v5/market/books-full?instId={c}-USDT&sz=5000")
    if st != 200:
        return {"status": st, "body": body.decode("utf-8", "replace")[:200]}
    data = json.loads(body).get("data") or []
    if not data:
        return {"status": st, "error": json.loads(body).get("msg")}
    d = data[0]
    bids = [(float(p), float(p) * float(s)) for p, s, *_ in d["bids"]]
    asks = [(float(p), float(p) * float(s)) for p, s, *_ in d["asks"]]
    out = depth_stats(bids, asks) or {}
    out.update({"status": st, "secs": round(secs, 2), "bytes": len(body)})
    return out


# ---- Coinbase spot: level 2 = the whole book aggregated by price
def coinbase(c):
    st, body, secs = get(f"https://api.exchange.coinbase.com/products/{c}-USD/book?level=2")
    if st != 200:
        return {"status": st, "body": body.decode("utf-8", "replace")[:200]}
    d = json.loads(body)
    bids = [(float(p), float(p) * float(s)) for p, s, *_ in d["bids"]]
    asks = [(float(p), float(p) * float(s)) for p, s, *_ in d["asks"]]
    out = depth_stats(bids, asks) or {}
    out.update({"status": st, "secs": round(secs, 2), "bytes": len(body)})
    return out


# ---- Hyperliquid: l2Book, 20 levels a side, aggregated to 2 or 3 significant figures
def hyperliquid(c):
    out = {}
    for sig in (2, 3):
        st, body, secs = get("https://api.hyperliquid.xyz/info", data={"type": "l2Book", "coin": c, "nSigFigs": sig})
        if st != 200 or body in (b"null", b""):
            return {"status": st, "body": body.decode("utf-8", "replace")[:200]}
        lv = json.loads(body)["levels"]
        bids = [(float(x["px"]), float(x["px"]) * float(x["sz"])) for x in lv[0]]
        asks = [(float(x["px"]), float(x["px"]) * float(x["sz"])) for x in lv[1]]
        out[f"sig{sig}"] = depth_stats(bids, asks)
    s3 = out.get("sig3") or {}
    return {**s3, **out, "status": 200}


# ---- Gate USDT perps: order_book (limit up to 300?), with price aggregation
GATE_Q = {}


def gate(c):
    if not GATE_Q:
        st, body, _ = get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
        for r in json.loads(body):
            GATE_Q[r["name"]] = float(r.get("quanto_multiplier") or 0)
    name = f"{c}_USDT"
    if name not in GATE_Q:
        return {"error": "no such contract"}
    st, body, secs = get(f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={name}&limit=300")
    if st != 200:
        return {"status": st, "body": body.decode("utf-8", "replace")[:200]}
    d = json.loads(body)
    q = GATE_Q[name]
    bids = [(float(x["p"]), float(x["p"]) * float(x["s"]) * q) for x in d["bids"]]
    asks = [(float(x["p"]), float(x["p"]) * float(x["s"]) * q) for x in d["asks"]]
    out = depth_stats(bids, asks) or {}
    out.update({"status": st, "secs": round(secs, 2)})
    return out


def binance_checks():
    res = {}
    for name, url in (("fapi_depth", "https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=1000"),
                      ("spot_api_depth", "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5000"),
                      ("data_api_spot_depth", "https://data-api.binance.vision/api/v3/depth?symbol=BTCUSDT&limit=5000"),
                      ("bybit_linear", "https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=500")):
        st, body, secs = get(url)
        r = {"status": st, "secs": round(secs, 2)}
        if st == 200:
            try:
                d = json.loads(body)
                d = d.get("result", d)
                bids = [(float(x[0]), float(x[0]) * float(x[1])) for x in d.get("bids") or d.get("b") or []]
                asks = [(float(x[0]), float(x[0]) * float(x[1])) for x in d.get("asks") or d.get("a") or []]
                r.update(depth_stats(bids, asks) or {})
            except Exception as e:
                r["error"] = str(e)[:150]
        else:
            r["body"] = body.decode("utf-8", "replace")[:160]
        res[name] = r
    REPORT["binance_bybit"] = res
    print("binance/bybit", {k: v["status"] for k, v in res.items()}, flush=True)


def extra_checks():
    """Live sources for the hourly versions of the other indicators, and OKX's historical order books
    (to calibrate the live LIQ without waiting for overlap)."""
    res = {}
    urls = {
        "binance_spot_klines": "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000",
        "binance_usdc_klines": "https://data-api.binance.vision/api/v3/klines?symbol=USDCUSDT&interval=1h&limit=5",
        "okx_funding_now": "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP",
        "okx_futures_list": "https://www.okx.com/api/v5/public/instruments?instType=FUTURES&instFamily=BTC-USD",
        "okx_index": "https://www.okx.com/api/v5/market/index-tickers?instId=BTC-USD",
        "okx_taker_volume": "https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=CONTRACTS&period=1H",
        "okx_ls_ratio": "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1H",
        "bitfinex_last": "https://api-pub.bitfinex.com/v2/stats1/pos.size:1m:tBTCUSD:long/last",
    }
    for mod in (1, 2, 3, 4, 5, 6):
        urls[f"okx_history_m{mod}"] = ("https://www.okx.com/api/v5/public/market-data-history?module=%d&instType=SWAP&instFamilyList=BTC-USDT"
                                       "&dateAggrType=daily&begin=%d&end=%d" % (mod, int(time.time() * 1000) - 5 * 86400000, int(time.time() * 1000) - 2 * 86400000))
    for name, url in urls.items():
        st, body, secs = get(url)
        txt = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        res[name] = {"status": st, "secs": round(secs, 2), "bytes": len(body or b""), "head": txt[:900]}
        time.sleep(0.3)
    st, body, secs = get("https://api.hyperliquid.xyz/info", data={"type": "metaAndAssetCtxs"})
    res["hl_ctxs"] = {"status": st, "bytes": len(body or b""), "head": (body or b"")[:400].decode("utf-8", "replace")}
    REPORT["extra"] = res
    print("extra", {k: v["status"] for k, v in res.items()}, flush=True)


def okx_l2_sample():
    """Format of OKX's daily 5000-level order-book files: the first records, how many, how far apart."""
    import io
    import tarfile
    out = {}
    end = int(time.time() * 1000) - 2 * 86400000
    for fam in ("OP-USDT", "BTC-USDT"):
        url = ("https://www.okx.com/api/v5/public/market-data-history?module=5&instType=SWAP&instFamilyList=%s"
               "&dateAggrType=daily&begin=%d&end=%d" % (fam, end - 86400000, end))
        st, body, _ = get(url)
        try:
            det = json.loads(body)["data"][0]["details"][0]["groupDetails"][0]
        except Exception as e:
            out[fam] = {"list_error": str(e)[:200], "body": body[:300].decode("utf-8", "replace")}
            continue
        t0 = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(det["url"], headers={"User-Agent": UA}), timeout=300) as r:
                raw = r.read()
        except Exception as e:
            out[fam] = {"download_error": str(e)[:200], "file": det.get("filename")}
            continue
        info = {"file": det.get("filename"), "mb": round(len(raw) / 1e6, 1), "download_s": round(time.time() - t0, 1), "members": []}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            for m in tf.getmembers()[:3]:
                info["members"].append({"name": m.name, "size": m.size})
                f = tf.extractfile(m)
                if not f:
                    continue
                lines, first, ts = 0, [], []
                for line in f:
                    lines += 1
                    if len(first) < 2:
                        first.append(line[:600].decode("utf-8", "replace"))
                    if lines <= 200:
                        try:
                            d = json.loads(line)
                            ts.append((d.get("ts") or (d.get("data") or [{}])[0].get("ts"), d.get("action"),
                                       len(d.get("bids") or (d.get("data") or [{}])[0].get("bids") or []),
                                       len(d.get("asks") or (d.get("data") or [{}])[0].get("asks") or [])))
                        except Exception:
                            pass
                info["members"][-1].update({"lines": lines, "first": first, "ts_first": ts[:12], "parse_s": round(time.time() - t0, 1)})
        out[fam] = info
        print("okx l2", fam, info["mb"], "MB", flush=True)
    REPORT["okx_l2"] = out


def main():
    os.makedirs(OUT, exist_ok=True)
    try:
        okx_l2_sample()
    except Exception as e:
        REPORT["okx_l2_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    try:
        extra_checks()
    except Exception as e:
        REPORT["extra_error"] = str(e)[:300]
    try:
        binance_checks()
    except Exception as e:
        REPORT["binance_bybit_error"] = str(e)
    try:
        okx_ct()
    except Exception as e:
        REPORT["okx_ct_error"] = str(e)
    for name, fn in (("okx_swap", okx_swap), ("okx_spot", okx_spot), ("coinbase", coinbase), ("hyperliquid", hyperliquid), ("gate", gate)):
        try:
            venue(name, fn)
        except Exception as e:
            REPORT["venues"][name] = {"error": str(e)[:200]}
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(REPORT, f, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
