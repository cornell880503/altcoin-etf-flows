#!/usr/bin/env python3
"""Which free sources (reachable from a GitHub runner, no keys) can feed copies of CoinKarma's
indicators (docs.coinkarma.co/english/indicators-guide): depth, history, granularity, samples.
Output: research/probe_ck/report.json. Every check is independent.
"""
import datetime as dt
import io
import json
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "probe_ck")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
REPORT = {"run_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {}}


class HTTPFail(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status, self.body = status, body


def http(url, data=None, timeout=60, tries=2, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = HTTPFail(e.code, e.read()[:400].decode("utf-8", "replace"))
            if e.code in (400, 401, 403, 404, 451):
                raise last
        except Exception as e:
            last = e
        time.sleep(2 + 3 * k)
    raise last


def jget(url, **kw):
    return json.loads(http(url, **kw))


def err(e):
    if isinstance(e, HTTPFail):
        return {"status": e.status, "body": " ".join(e.body.split())[:240]}
    return {"error": f"{type(e).__name__}: {str(e)[:240]}"}


def ms(day):
    return int(dt.datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)


def iso(t):
    t = int(t)
    if t > 10 ** 14:
        t //= 1000
    if t > 10 ** 11:
        t //= 1000
    return dt.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M")


SOURCES = []


def source(name):
    def wrap(fn):
        def run():
            t0 = time.time()
            try:
                out = {"ok": True, **(fn() or {})}
            except Exception as e:
                out = {"ok": False, **err(e), "trace": traceback.format_exc()[-500:]}
            out["secs"] = round(time.time() - t0, 1)
            REPORT["sources"][name] = out
            print(f"== {name}: {json.dumps(out, ensure_ascii=False)[:3000]}", flush=True)
        SOURCES.append(run)
        return run
    return wrap


# ---------------------------------------------------------------- data.binance.vision
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"


def s3_list(prefix, delimiter=None, marker=None, max_keys=None):
    q = {"prefix": prefix}
    if delimiter:
        q["delimiter"] = delimiter
    if marker:
        q["marker"] = marker
    if max_keys:
        q["max-keys"] = str(max_keys)
    root = ET.fromstring(http(f"{S3}?{urllib.parse.urlencode(q)}"))
    tag = lambda el: el.tag.split("}", 1)[-1]  # noqa: E731
    keys, prefixes, trunc = [], [], False
    for el in root:
        if tag(el) == "Contents":
            keys += [c.text for c in el if tag(c) == "Key"]
        elif tag(el) == "CommonPrefixes":
            prefixes += [c.text for c in el if tag(c) == "Prefix"]
        elif tag(el) == "IsTruncated":
            trunc = el.text == "true"
    return keys, prefixes, trunc


def zip_rows(path, n=6):
    raw = http("https://data.binance.vision/" + path)
    z = zipfile.ZipFile(io.BytesIO(raw))
    txt = z.read(z.namelist()[0]).decode("utf-8", "replace").splitlines()
    return {"lines": len(txt), "head": txt[:n], "tail": txt[-3:]}


@source("binance_vision")
def binance_vision():
    out = {}
    checks = {
        "bookDepth_BTCUSDT": "data/futures/um/daily/bookDepth/BTCUSDT/",
        "bookDepth_SOLUSDT": "data/futures/um/daily/bookDepth/SOLUSDT/",
        "fundingRate_BTCUSDT_monthly": "data/futures/um/monthly/fundingRate/BTCUSDT/",
        "spot_USDCUSDT_1h_monthly": "data/spot/monthly/klines/USDCUSDT/1h/",
        "spot_BTCUSDC_1h_monthly": "data/spot/monthly/klines/BTCUSDC/1h/",
        "spot_BTCUSDT_1d_monthly": "data/spot/monthly/klines/BTCUSDT/1d/",
        "cm_BTCUSD_PERP_1d_monthly": "data/futures/cm/monthly/klines/BTCUSD_PERP/1d/",
        "um_metrics_BTCUSDT_daily": "data/futures/um/daily/metrics/BTCUSDT/",
    }
    for name, pre in checks.items():
        try:
            k, _, trunc = s3_list(pre, max_keys=3)
            out[name] = {"first": [x.rsplit("/", 1)[-1] for x in k[:2]], "more": trunc}
        except Exception as e:
            out[name] = err(e)
        time.sleep(0.3)
    try:  # quarterly delivery contracts (COIN-M and USD-M)
        _, p, _ = s3_list("data/futures/cm/monthly/klines/", delimiter="/")
        out["cm_quarterlies"] = sorted(x.split("/")[-2] for x in p if x.split("/")[-2].startswith("BTCUSD_") and "PERP" not in x)
        _, p, _ = s3_list("data/futures/um/monthly/klines/", delimiter="/")
        out["um_quarterlies"] = sorted(x.split("/")[-2] for x in p if x.split("/")[-2].startswith("BTCUSDT_"))
    except Exception as e:
        out["quarterlies"] = err(e)
    y = (NOW.date() - dt.timedelta(days=2)).isoformat()
    for name, path in (("bookDepth_sample", f"data/futures/um/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-{y}.zip"),
                       ("bookDepth_2023", "data/futures/um/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-2023-06-01.zip")):
        try:
            out[name] = zip_rows(path, 14)
        except Exception as e:
            out[name] = err(e)
    return out


# ---------------------------------------------------------------- Bitfinex
@source("bitfinex")
def bitfinex():
    b, out = "https://api-pub.bitfinex.com/v2", {}
    for name, path in [("pos_long_BTCUSD", "/stats1/pos.size:1m:tBTCUSD:long/hist?limit=3"),
                       ("pos_long_BTCUSD_2021", f"/stats1/pos.size:1m:tBTCUSD:long/hist?limit=3&start={ms('2021-01-01')}&sort=1"),
                       ("pos_long_ETHBTC", "/stats1/pos.size:1m:tETHBTC:long/hist?limit=3"),
                       ("pos_short_BTCUSD", "/stats1/pos.size:1m:tBTCUSD:short/hist?limit=3"),
                       ("credits_fUSD", "/stats1/credits.size:1m:fUSD/hist?limit=3"),
                       ("candles_BTCUSD_1D_2021", f"/candles/trade:1D:tBTCUSD/hist?limit=3&start={ms('2021-01-01')}&sort=1"),
                       ("candles_BTCUSD_1h", "/candles/trade:1h:tBTCUSD/hist?limit=3"),
                       ("funding_fUSD_1D_p30", "/candles/trade:1D:fUSD:p30/hist?limit=3"),
                       ("funding_fUST_1D_p30", "/candles/trade:1D:fUST:p30/hist?limit=3"),
                       ("funding_fUSD_a30p2p30", "/candles/trade:1D:fUSD:a30:p2:p30/hist?limit=3"),
                       ("funding_fUSD_2021", f"/candles/trade:1D:fUSD:p30/hist?limit=3&start={ms('2021-01-01')}&sort=1")]:
        try:
            j = jget(b + path)
            out[name] = {"n": len(j), "rows": [[iso(r[0])] + r[1:] for r in j[:3]] if j and isinstance(j[0], list) else j[:3]}
        except Exception as e:
            out[name] = err(e)
        time.sleep(1.2)
    return out


# ---------------------------------------------------------------- Deribit options
@source("deribit")
def deribit():
    b, out = "https://www.deribit.com/api/v2/public", {}
    try:
        q = urllib.parse.urlencode({"currency": "BTC", "start_timestamp": ms("2021-03-01"), "end_timestamp": ms("2021-03-10"), "resolution": "1D"})
        r = jget(f"{b}/get_volatility_index_data?{q}").get("result", {})
        out["dvol_2021"] = {"n": len(r.get("data", [])), "first": r.get("data", [[None]])[0]}
        q = urllib.parse.urlencode({"currency": "BTC", "start_timestamp": ms((NOW.date() - dt.timedelta(days=5)).isoformat()), "end_timestamp": int(NOW.timestamp() * 1000), "resolution": "1D"})
        r = jget(f"{b}/get_volatility_index_data?{q}").get("result", {})
        out["dvol_recent"] = r.get("data", [])[-3:]
    except Exception as e:
        out["dvol"] = err(e)
    try:
        r = jget(f"{b}/get_book_summary_by_currency?currency=BTC&kind=option").get("result", [])
        out["options"] = {"n": len(r), "keys": sorted(r[0].keys()) if r else None,
                          "sample": {k: r[0].get(k) for k in ("instrument_name", "mark_iv", "underlying_price", "open_interest", "bid_iv", "ask_iv")} if r else None}
    except Exception as e:
        out["options"] = err(e)
    try:
        r = jget(f"{b}/ticker?instrument_name=" + urllib.parse.quote(out.get("options", {}).get("sample", {}).get("instrument_name", "BTC-PERPETUAL")))
        res = r.get("result", {})
        out["ticker_greeks"] = {"greeks": res.get("greeks"), "mark_iv": res.get("mark_iv")}
    except Exception as e:
        out["ticker_greeks"] = err(e)
    try:  # historical option trades with IV (history.deribit.com)
        q = urllib.parse.urlencode({"currency": "BTC", "kind": "option", "start_timestamp": ms("2022-06-01"), "end_timestamp": ms("2022-06-01") + 3600000, "count": 5})
        r = jget(f"https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time?{q}").get("result", {})
        out["history_trades_2022"] = {"n": len(r.get("trades", [])), "sample": {k: r.get("trades", [{}])[0].get(k) for k in ("instrument_name", "iv", "index_price", "timestamp")} if r.get("trades") else None}
    except Exception as e:
        out["history_trades_2022"] = err(e)
    return out


# ---------------------------------------------------------------- OKX
@source("okx")
def okx():
    b, out = "https://www.okx.com/api/v5", {}
    for name, path in [("funding_hist", "/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100"),
                       ("lending_rate_hist", "/finance/savings/lending-rate-history?ccy=USDT&limit=100"),
                       ("insurance_fund", "/public/insurance-fund?instType=SWAP&instFamily=BTC-USDT&limit=100"),
                       ("insurance_fund_all", "/public/insurance-fund?instType=SWAP&type=regular_update&instFamily=BTC-USDT&limit=5"),
                       ("long_short_acct", "/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1D"),
                       ("taker_volume", "/rubik/stat/taker-volume?ccy=BTC&instType=CONTRACTS&period=1D")]:
        try:
            j = jget(b + path)
            d = j.get("data") or []
            if name.startswith("insurance") and d and isinstance(d[0], dict) and "details" in d[0]:
                det = d[0]["details"]
                out[name] = {"code": j.get("code"), "n": len(det), "first": det[-1] if det else None, "last": det[0] if det else None, "total": d[0].get("total")}
            else:
                times = [x.get("fundingTime") or x.get("ts") for x in d if isinstance(x, dict)] or [x[0] for x in d if isinstance(x, list)]
                out[name] = {"code": j.get("code"), "msg": j.get("msg", "")[:80], "n": len(d),
                             "range": [iso(min(map(int, times))), iso(max(map(int, times)))] if times else None,
                             "sample": d[0] if d else None}
        except Exception as e:
            out[name] = err(e)
        time.sleep(0.6)
    try:  # how far back funding history goes
        after, n, oldest = None, 0, None
        for _ in range(30):
            j = jget(b + "/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100" + (f"&after={after}" if after else ""))
            d = j.get("data") or []
            if not d:
                break
            n += len(d)
            oldest = min(int(x["fundingTime"]) for x in d)
            after = oldest
            time.sleep(0.3)
        out["funding_depth"] = {"rows": n, "oldest": iso(oldest) if oldest else None}
    except Exception as e:
        out["funding_depth"] = err(e)
    return out


# ---------------------------------------------------------------- Apple App Store (finance top charts)
@source("apple_rss")
def apple_rss():
    out = {}
    for name, url in [("itunes_finance_free", "https://itunes.apple.com/us/rss/topfreeapplications/limit=200/genre=6015/json"),
                      ("marketing_tools_top", "https://rss.applemarketingtools.com/api/v2/us/apps/top-free/50/apps.json")]:
        try:
            j = jget(url)
            if "feed" in j and "entry" in j["feed"]:
                names = [e["im:name"]["label"] for e in j["feed"]["entry"]]
            else:
                names = [r.get("name") for r in j.get("feed", {}).get("results", [])]
            ranks = {n: i + 1 for i, n in enumerate(names)}
            out[name] = {"n": len(names), "coinbase": next((v for k, v in ranks.items() if "Coinbase" in k), None),
                         "top": names[:5], "crypto": {k: v for k, v in ranks.items() if any(s in k for s in ("Coinbase", "Kraken", "Crypto.com", "Robinhood", "Binance", "Interactive Brokers", "Yahoo"))}}
        except Exception as e:
            out[name] = err(e)
    return out


# ---------------------------------------------------------------- MSTR price, CFTC depth, FRED depth, DefiLlama per coin
@source("equities_macro")
def equities_macro():
    out = {}
    for name, url in [("stooq_mstr", "https://stooq.com/q/d/l/?s=mstr.us&i=d"),
                      ("yahoo_mstr", "https://query1.finance.yahoo.com/v8/finance/chart/MSTR?range=5y&interval=1d")]:
        try:
            raw = http(url, timeout=40).decode("utf-8", "replace")
            if raw.lstrip().startswith("{"):
                j = json.loads(raw)
                res = j.get("chart", {}).get("result", [{}])[0]
                ts = res.get("timestamp", [])
                out[name] = {"n": len(ts), "first": iso(ts[0]) if ts else None, "last": iso(ts[-1]) if ts else None,
                             "keys": list(res.get("indicators", {}).get("quote", [{}])[0].keys())}
            else:
                lines = raw.splitlines()
                out[name] = {"n": len(lines), "head": lines[:2], "tail": lines[-1:]}
        except Exception as e:
            out[name] = err(e)
    for name, url in [("strategy_kpi", "https://api.strategy.com/btc/mstrKpiData"),
                      ("strategy_bitcoin_kpis", "https://api.strategy.com/btc/bitcoinKpis"),
                      ("strategy_purchases", "https://api.strategy.com/btc/purchases")]:
        try:
            raw = http(url, timeout=40).decode("utf-8", "replace")
            out[name] = {"len": len(raw), "head": raw[:400]}
        except Exception as e:
            out[name] = err(e)
    try:
        q = urllib.parse.urlencode({"$where": "cftc_contract_market_code='133741'", "$limit": 3, "$order": "report_date_as_yyyy_mm_dd"})
        rows = jget(f"https://publicreporting.cftc.gov/resource/gpe5-46if.json?{q}")
        out["cftc_btc_first"] = {"n": len(rows), "first": rows[0].get("report_date_as_yyyy_mm_dd") if rows else None,
                                 "fields": sorted(k for k in rows[0] if any(s in k for s in ("lev", "asset", "dealer", "other"))) [:40] if rows else None}
    except Exception as e:
        out["cftc_btc_first"] = err(e)
    try:
        txt = http("https://fred.stlouisfed.org/graph/fredgraph.csv?id=WALCL&cosd=2020-01-01", timeout=60).decode()
        out["fred_walcl"] = {"lines": len(txt.splitlines()), "head": txt.splitlines()[:2]}
    except Exception as e:
        out["fred_walcl"] = err(e)
    try:
        j = jget("https://stablecoins.llama.fi/stablecoin/2")
        toks = j.get("tokens", [])
        out["defillama_usdc"] = {"n": len(toks), "first": iso(toks[0]["date"]) if toks else None}
    except Exception as e:
        out["defillama_usdc"] = err(e)
    try:  # Binance insurance fund (web endpoint; Binance refuses US IPs on its API)
        raw = http("https://www.binance.com/bapi/futures/v1/public/future/common/get-insurance-fund-balance", timeout=30)
        out["binance_insurance"] = {"head": raw[:300].decode("utf-8", "replace")}
    except Exception as e:
        out["binance_insurance"] = err(e)
    try:
        j = jget("https://api.hyperliquid.xyz/info", data={"type": "fundingHistory", "coin": "BTC", "startTime": ms("2023-06-01")})
        out["hl_funding_2023"] = {"n": len(j), "first": iso(j[0]["time"]) if j else None}
    except Exception as e:
        out["hl_funding_2023"] = err(e)
    return out


def main():
    for run in SOURCES:
        run()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(REPORT, f, indent=1, sort_keys=True, ensure_ascii=False)
    print("done:", {k: v.get("ok") for k, v in REPORT["sources"].items()})


if __name__ == "__main__":
    main()
