#!/usr/bin/env python3
"""Which other order books a GitHub runner can get, live and historical, and how far from the price
they reach (for a spot-based LIQ closer to CoinKarma's; research/ck_compare.py). Output:
research/probe_books_more/report.json."""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "probe_books_more")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
REPORT = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "live": {}, "history": {}, "ws": {}}


def get(url, rng=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if rng:
        h["Range"] = f"bytes=0-{rng}"
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.status, r.read(), round(time.time() - t0, 2), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300], round(time.time() - t0, 2), dict(e.headers or {})
    except Exception as e:
        return None, str(e)[:200].encode(), round(time.time() - t0, 2), {}


def reach(bids, asks):
    """[(price, size)] -> levels, % reach each side, notional within 1/2/5/10%."""
    if not bids or not asks:
        return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bb + ba) / 2
    out = {"levels": [len(bids), len(asks)], "reach_pct": [round((1 - min(p for p, _ in bids) / mid) * 100, 2),
                                                           round((max(p for p, _ in asks) / mid - 1) * 100, 2)]}
    for k in (1, 2, 5, 10):
        out[f"bid{k}_m"] = round(sum(p * q for p, q in bids if p >= mid * (1 - k / 100)) / 1e6, 2)
        out[f"ask{k}_m"] = round(sum(p * q for p, q in asks if p <= mid * (1 + k / 100)) / 1e6, 2)
    return out


def live(name, url, parse):
    st, body, secs, _ = get(url)
    res = {"status": st, "secs": secs}
    if st == 200:
        try:
            res.update(reach(*parse(json.loads(body))) or {})
        except Exception as e:
            res["parse_error"] = str(e)[:150]
    else:
        res["body"] = body[:160].decode("utf-8", "replace")
    REPORT["live"][name] = res
    print(name, res, flush=True)


def f2(rows, i=0, j=1):
    return [(float(r[i]), float(r[j])) for r in rows]


def main():
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"):
        live(f"binance spot data-api {sym} 5000", f"https://data-api.binance.vision/api/v3/depth?symbol={sym}&limit=5000",
             lambda j: (f2(j["bids"]), f2(j["asks"])))
    live("binance spot api.binance.com BTCUSDT", "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5000",
         lambda j: (f2(j["bids"]), f2(j["asks"])))
    live("binance.us BTCUSDT", "https://api.binance.us/api/v3/depth?symbol=BTCUSDT&limit=5000", lambda j: (f2(j["bids"]), f2(j["asks"])))
    live("bitstamp BTCUSD group=1", "https://www.bitstamp.net/api/v2/order_book/btcusd/?group=1",
         lambda j: (f2(j["bids"]), f2(j["asks"])))
    for prec in ("P2", "P3"):
        live(f"bitfinex tBTCUSD {prec} len250", f"https://api-pub.bitfinex.com/v2/book/tBTCUSD/{prec}?len=250",
             lambda j: ([(r[0], r[2]) for r in j if r[2] > 0], [(r[0], -r[2]) for r in j if r[2] < 0]))
    live("kraken XBTUSD 500", "https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=500",
         lambda j: (f2(next(iter(j["result"].values()))["bids"]), f2(next(iter(j["result"].values()))["asks"])))
    live("kucoin BTC-USDT level2_100", "https://api.kucoin.com/api/v1/market/orderbook/level2_100?symbol=BTC-USDT",
         lambda j: (f2(j["data"]["bids"]), f2(j["data"]["asks"])))
    live("bybit spot BTCUSDT 200", "https://api.bybit.com/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=200",
         lambda j: (f2(j["result"]["b"]), f2(j["result"]["a"])))
    live("okx spot BTC-USDT books-full 5000", "https://www.okx.com/api/v5/market/books-full?instId=BTC-USDT&sz=5000",
         lambda j: (f2(j["data"][0]["bids"]), f2(j["data"][0]["asks"])))
    live("coinbase BTC-USD level2", "https://api.exchange.coinbase.com/products/BTC-USD/book?level=2",
         lambda j: (f2(j["bids"]), f2(j["asks"])))
    live("upbit KRW-BTC", "https://api.upbit.com/v1/orderbook?markets=KRW-BTC", lambda j: (
        [(u["bid_price"], u["bid_size"]) for u in j[0]["orderbook_units"]], [(u["ask_price"], u["ask_size"]) for u in j[0]["orderbook_units"]]))
    # websockets (Binance's market-data-only stream host, and the main one)
    try:
        import websocket  # websocket-client
        for name, url in (("binance data-stream", "wss://data-stream.binance.vision/ws/btcusdt@depth@100ms"),
                          ("binance stream", "wss://stream.binance.com:9443/ws/btcusdt@depth@100ms")):
            t0 = time.time()
            try:
                ws = websocket.create_connection(url, timeout=10)
                msgs = [ws.recv() for _ in range(5)]
                ws.close()
                j = json.loads(msgs[-1])
                REPORT["ws"][name] = {"ok": True, "secs": round(time.time() - t0, 2), "updates_in_5_msgs": sum(len(json.loads(m).get("b", [])) + len(json.loads(m).get("a", [])) for m in msgs),
                                      "keys": list(j)[:8]}
            except Exception as e:
                REPORT["ws"][name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
            print(name, REPORT["ws"][name], flush=True)
    except ImportError as e:
        REPORT["ws"]["error"] = str(e)
    # history
    for name, url in (("tardis binance spot BTCUSDT 2026-10-01 (free first day)", "https://datasets.tardis.dev/v1/binance/incremental_book_L2/2026/10/01/BTCUSDT.csv.gz"),
                      ("tardis binance spot BTCUSDT 2026-10-02", "https://datasets.tardis.dev/v1/binance/incremental_book_L2/2026/10/02/BTCUSDT.csv.gz"),
                      ("tardis binance spot book_snapshot_25 2026-10-01", "https://datasets.tardis.dev/v1/binance/book_snapshot_25/2026/10/01/BTCUSDT.csv.gz"),
                      ("tardis coinbase BTC-USD 2026-10-01", "https://datasets.tardis.dev/v1/coinbase/incremental_book_L2/2026/10/01/BTC-USD.csv.gz"),
                      ("bybit spot ob200 2026-10-27", "https://quote-saver.bycsi.com/orderbook/spot/BTCUSDT/2026-10-27_BTCUSDT_ob200.data.zip"),
                      ("coinglass api (no key)", "https://open-api-v4.coinglass.com/api/spot/orderbook/ask-bids-history?exchange=Binance&symbol=BTCUSDT&interval=1h&range=10")):
        st, body, secs, hdr = get(url, rng=2000)
        REPORT["history"][name] = {"status": st, "secs": secs, "size": hdr.get("Content-Range") or hdr.get("Content-Length"),
                                   "body": body[:200].decode("utf-8", "replace") if st not in (200, 206) else "ok"}
        print(name, REPORT["history"][name], flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(REPORT, f, indent=1)


if __name__ == "__main__":
    main()
