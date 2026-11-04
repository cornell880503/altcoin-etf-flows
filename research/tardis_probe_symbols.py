#!/usr/bin/env python3
"""Which spot exchanges on Tardis.dev have BTC and ETH order-book datasets (incremental_book_L2), under which
symbol ids and since when. Reads https://api.tardis.dev/v1/exchanges/<id> for a list of candidates and writes
research/spot/tardis_symbols.json; prints the items for research/tardis_spot_day.py (exchange:symbol,...).
Run on a GitHub runner (the workspace cannot reach tardis.dev)."""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "spot", "tardis_symbols.json")
EXCHANGES = ["binance", "coinbase", "okex", "bybit-spot", "kraken", "bitfinex", "bitstamp", "gate-io", "kucoin", "huobi",
             "crypto-com", "gemini", "upbit", "bitget", "mexc", "ascendex", "poloniex", "hitbtc", "whitebit", "binance-us"]
WANT = {"BTC": (["BTCUSDT", "BTC-USDT", "BTC_USDT", "BTC/USDT"], ["XBT/USD", "XBTUSD", "XBT-USD", "BTCUSD", "BTC-USD", "BTC/USD", "BTCUSDC", "BTC-USDC"]),
        "ETH": (["ETHUSDT", "ETH-USDT", "ETH_USDT", "ETH/USDT"], ["ETHUSD", "ETH-USD", "ETH/USD", "ETHUSDC", "ETH-USDC"])}
PREFER_USD = {"coinbase", "bitstamp", "kraken", "gemini", "bitfinex", "binance-us"}  # their main market quotes in US$
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
        return json.loads(r.read())


def main():
    out, items = {}, []
    for ex in EXCHANGES:
        try:
            d = get(f"https://api.tardis.dev/v1/exchanges/{ex}")
        except Exception as e:
            print(f"{ex}: {type(e).__name__}: {str(e)[:80]}", flush=True)
            continue
        syms = {s["id"].upper(): s for s in (d.get("datasets") or {}).get("symbols") or []}
        if not syms:
            print(f"{ex}: no datasets", flush=True)
            continue
        rec = {"supportsDatasets": d.get("supportsDatasets")}
        for coin, (usdt, usd) in WANT.items():
            pick = None
            for cand in (usd + usdt if ex in PREFER_USD else usdt + usd):
                s = syms.get(cand.upper())
                if s and "incremental_book_L2" in (s.get("dataTypes") or []) and s.get("type", "spot") in ("spot", None):
                    pick = s
                    break
            if pick:
                rec[coin] = {"id": pick["id"], "since": pick.get("availableSince"), "to": pick.get("availableTo"), "type": pick.get("type")}
                items.append(f"{ex}:{pick['id']}")
        out[ex] = rec
        print(f"{ex}: " + ", ".join(f"{c} {rec[c]['id']} since {str(rec[c]['since'])[:10]}" for c in ("BTC", "ETH") if c in rec) or "nothing", flush=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    print("ITEMS=" + ",".join(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
