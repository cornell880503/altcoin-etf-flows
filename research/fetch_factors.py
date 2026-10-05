#!/usr/bin/env python3
"""Research datasets for the BTC flow study (runs in GitHub Actions; every source is public and
free, and each one is optional: a failure is recorded and the rest continue).

  research/data/coinmetrics_<asset>.json   daily price, market cap, supply, ... (community API)
  research/data/coingecko_<id>.json        daily price / market cap / volume, last 365 days
  research/data/stablecoins.json           total stablecoin supply (DefiLlama)
  research/data/funding_*.json             perpetual funding (Hyperliquid, BitMEX, Binance)
  research/data/binance_BTCUSDT_*.json     Binance spot klines (1d, 1h) from data.binance.vision
  research/data/coinbase_BTC-USD_*.json    Coinbase spot candles (1d, 1h)
  research/data/deribit_btc_futures.json   Deribit dated futures + perpetual daily closes (basis)
  research/data/cftc_tff_crypto.json       CFTC Traders in Financial Futures, CME bitcoin/ether
  research/data/fred.json                  rates, dollar, VIX, equity indexes, Fed liquidity
  research/data/_report.json               what worked
"""
import csv
import datetime as dt
import io
import json
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "data")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
START = dt.date(2023, 12, 1)
NOW = dt.datetime.now(dt.timezone.utc)
REPORT = {"run_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {}}


def http(url, data=None, headers=None, timeout=60, tries=3):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(30 * (k + 1))
                continue
            if e.code in (400, 401, 403, 404, 410, 451):
                raise
            time.sleep(3 * (k + 1))
        except Exception as e:  # timeouts, resets
            last = e
            time.sleep(3 * (k + 1))
    raise last


def jget(url, **kw):
    return json.loads(http(url, **kw).decode("utf-8"))


def save(name, obj):
    json.dump(obj, open(os.path.join(OUT, name), "w"), separators=(",", ":"))


def source(name):
    def deco(fn):
        def run():
            t0 = time.time()
            try:
                info = fn() or {}
                REPORT["sources"][name] = {"ok": True, "secs": round(time.time() - t0, 1), **info}
            except Exception as e:
                REPORT["sources"][name] = {"ok": False, "secs": round(time.time() - t0, 1),
                                           "error": f"{type(e).__name__}: {str(e)[:300]}",
                                           "trace": traceback.format_exc()[-800:]}
            print(name, json.dumps(REPORT["sources"][name])[:400], flush=True)
        return run
    return deco


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


CM_ASSETS = ["btc", "eth", "sol", "xrp", "doge", "ltc", "link", "avax", "dot", "hbar", "trx", "bnb", "sui", "near", "zec", "hype"]
CM_WISH = ["PriceUSD", "ReferenceRateUSD", "CapMrktCurUSD", "CapMrktEstUSD", "CapMrktFFUSD", "SplyCur", "SplyFF", "CapRealUSD",
           "CapMVRVCur", "FlowInExUSD", "FlowOutExUSD", "SplyExNtv", "TxTfrValAdjUSD", "AdrActCnt", "HashRate", "IssTotNtv",
           "volume_reported_spot_usd_1d", "volume_trusted_spot_usd_1d", "SplyAct1yr", "SplyAct180d", "VtyDayRet30d", "NVTAdj"]


@source("coinmetrics")
def coinmetrics():
    base = "https://community-api.coinmetrics.io/v4"
    avail = {}
    try:
        cat = jget(f"{base}/catalog-v2/asset-metrics?assets={','.join(CM_ASSETS)}&page_size=10000")
        for a in cat.get("data", []):
            avail[a["asset"]] = sorted(m["metric"] for m in a.get("metrics", [])
                                       if any(f.get("frequency") == "1d" for f in m.get("frequencies", [])))
    except Exception as e:
        avail["_catalog_error"] = str(e)[:200]
    save("coinmetrics_catalog.json", avail)
    got = {}
    for a in CM_ASSETS:
        mets = [m for m in CM_WISH if m in avail.get(a, CM_WISH)]
        if not mets:
            continue
        q = urllib.parse.urlencode({"assets": a, "metrics": ",".join(mets), "frequency": "1d", "start_time": START.isoformat(),
                                    "page_size": 10000, "ignore_forbidden_errors": "true", "ignore_unsupported_errors": "true"})
        url, rows = f"{base}/timeseries/asset-metrics?{q}", []
        try:
            while url:
                j = jget(url)
                rows += j.get("data", [])
                url = j.get("next_page_url")
                time.sleep(0.7)
        except Exception as e:
            got[a] = f"ERR {str(e)[:150]}"
            continue
        out = {}
        for r in rows:
            d = r["time"][:10]
            rec = {}
            for k, v in r.items():
                if k in ("asset", "time") or v in (None, ""):
                    continue
                try:
                    rec[k] = float(v)
                except (TypeError, ValueError):
                    pass  # status flags such as "flash" / "reviewed"
            out[d] = rec
        save(f"coinmetrics_{a}.json", out)
        got[a] = {"days": len(out), "metrics": sorted({k for v in out.values() for k in v})}
    return {"assets": got}


CG_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple", "HYPE": "hyperliquid", "DOGE": "dogecoin",
          "LINK": "chainlink", "AVAX": "avalanche-2", "HBAR": "hedera-hashgraph", "LTC": "litecoin", "BNB": "binancecoin",
          "DOT": "polkadot", "SUI": "sui", "NEAR": "near", "TRX": "tron", "ZEC": "zcash"}


@source("coingecko")
def coingecko():
    got = {}
    for sym, cid in CG_IDS.items():
        try:
            j = jget(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?vs_currency=usd&days=365&interval=daily",
                     headers={"Accept": "application/json"})
            rows = {}
            for key in ("prices", "market_caps", "total_volumes"):
                for ts, v in j.get(key, []):
                    d = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date().isoformat()
                    rows.setdefault(d, {})[key] = v
            save(f"coingecko_{sym}.json", rows)
            got[sym] = len(rows)
        except Exception as e:
            got[sym] = f"ERR {str(e)[:120]}"
        time.sleep(9)
    return {"assets": got}


@source("stablecoins")
def stablecoins():
    j = jget("https://stablecoins.llama.fi/stablecoincharts/all")
    out = {}
    for r in j:
        d = dt.datetime.fromtimestamp(int(r["date"]), dt.timezone.utc).date().isoformat()
        if d >= START.isoformat():
            out[d] = {"usd": (r.get("totalCirculatingUSD") or {}).get("peggedUSD"),
                      "minted": (r.get("totalMintedUSD") or {}).get("peggedUSD") if isinstance(r.get("totalMintedUSD"), dict) else None}
    save("stablecoins.json", out)
    # the two largest, separately
    per = {}
    for sid, name in (("1", "USDT"), ("2", "USDC")):
        try:
            k = jget(f"https://stablecoins.llama.fi/stablecoin/{sid}")
            ser = {}
            for r in k.get("tokens", []):
                d = dt.datetime.fromtimestamp(int(r["date"]), dt.timezone.utc).date().isoformat()
                if d >= START.isoformat():
                    ser[d] = (r.get("circulating") or {}).get("peggedUSD")
            per[name] = ser
        except Exception as e:
            per[name] = f"ERR {str(e)[:100]}"
        time.sleep(1)
    save("stablecoins_by_coin.json", per)
    return {"days": len(out), "first": min(out), "last": max(out)}


@source("funding_hyperliquid")
def funding_hyperliquid():
    got = {}
    for coin in ("BTC", "ETH"):
        start = int(dt.datetime(START.year, START.month, START.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
        rows = []
        for _ in range(200):
            j = jget("https://api.hyperliquid.xyz/info", data=json.dumps({"type": "fundingHistory", "coin": coin, "startTime": start}).encode(),
                     headers={"Content-Type": "application/json"})
            if not j:
                break
            rows += [[int(r["time"]), float(r["fundingRate"]), float(r.get("premium") or 0)] for r in j]
            nxt = int(j[-1]["time"]) + 1
            if nxt <= start or len(j) < 2:
                break
            start = nxt
            time.sleep(0.35)
        save(f"funding_hyperliquid_{coin}.json", rows)
        got[coin] = {"rows": len(rows), "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None}
    return got


@source("funding_bitmex")
def funding_bitmex():
    rows, off = [], 0
    while True:
        q = urllib.parse.urlencode({"symbol": "XBTUSD", "count": 500, "start": off, "reverse": "false", "startTime": START.isoformat()})
        j = jget(f"https://www.bitmex.com/api/v1/funding?{q}")
        rows += [[r["timestamp"], r["fundingRate"]] for r in j]
        if len(j) < 500:
            break
        off += 500
        time.sleep(1.5)
    save("funding_bitmex_XBTUSD.json", rows)
    return {"rows": len(rows)}


def binance_zip(url):
    raw = http(url, timeout=60, tries=2)
    z = zipfile.ZipFile(io.BytesIO(raw))
    return z.read(z.namelist()[0]).decode("utf-8")


@source("binance_vision")
def binance_vision():
    end = (NOW.date().replace(day=1) - dt.timedelta(days=1))
    fund, k1d, k1h, miss = [], [], [], []
    for y, m in months(START, end):
        ym = f"{y}-{m:02d}"
        for kind in ("fund", "1d", "1h"):
            if kind == "fund":
                url = f"https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{ym}.zip"
            else:
                url = f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/{kind}/BTCUSDT-{kind}-{ym}.zip"
            try:
                txt = binance_zip(url)
            except Exception as e:
                miss.append(f"{kind} {ym}: {str(e)[:60]}")
                continue
            for row in csv.reader(io.StringIO(txt)):
                if not row or not row[0].strip().lstrip("-").isdigit():
                    continue  # header
                if kind == "fund":
                    fund.append([int(row[0]), float(row[2])])
                else:
                    t = int(row[0])
                    t = t // 1000 if t > 10 ** 14 else t  # microseconds from 2025
                    rec = [t, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7]), float(row[10])]
                    (k1d if kind == "1d" else k1h).append(rec)
            time.sleep(0.2)
    save("funding_binance_BTCUSDT.json", fund)
    save("binance_BTCUSDT_1d.json", k1d)  # [open ms, o, h, l, c, base vol, quote vol, taker-buy quote vol]
    save("binance_BTCUSDT_1h.json", k1h)
    return {"funding": len(fund), "k1d": len(k1d), "k1h": len(k1h), "missing": miss[:12], "n_missing": len(miss)}


QUARTERLY = ["240329", "240628", "240927", "241227", "250328", "250627", "250926", "251226",
             "260327", "260626", "260925", "261225", "270326"]


def bv_rows(path, sym, interval, y, m):
    """Klines for one month from data.binance.vision: the monthly file, or the daily files when the
    monthly one is not published yet. Rows: [open ms, o, h, l, c, base vol, quote vol, taker-buy quote]."""
    ym = f"{y}-{m:02d}"
    try:
        txts = [binance_zip(f"https://data.binance.vision/data/{path}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{ym}.zip")]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        txts = []
        d = dt.date(y, m, 1)
        while d.month == m and d < NOW.date():
            try:
                txts.append(binance_zip(f"https://data.binance.vision/data/{path}/daily/klines/{sym}/{interval}/{sym}-{interval}-{d.isoformat()}.zip"))
            except urllib.error.HTTPError:
                pass
            d += dt.timedelta(days=1)
            time.sleep(0.1)
    out = []
    for txt in txts:
        for row in csv.reader(io.StringIO(txt)):
            if not row or not row[0].strip().isdigit():
                continue
            t = int(row[0])
            t = t // 1000 if t > 10 ** 14 else t
            out.append([t, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7]), float(row[10])])
    return out


@source("binance_quarterly")
def binance_quarterly():
    """USDT-margined quarterly futures daily klines (for the term basis versus spot BTCUSDT)."""
    got, series = {}, {}
    for q in QUARTERLY:
        sym = f"BTCUSDT_{q}"
        exp = dt.date(2000 + int(q[:2]), int(q[2:4]), int(q[4:]))
        first = max(START, (exp.replace(day=1) - dt.timedelta(days=280)).replace(day=1))
        last = min(exp, NOW.date())
        rows = []
        for y, m in months(first, last):
            try:
                rows += bv_rows("futures/um", sym, "1d", y, m)
            except Exception:
                pass
            time.sleep(0.15)
        series[sym] = {"expiry": exp.isoformat(), "rows": sorted({r[0]: r for r in rows}.values())}
        got[sym] = len(series[sym]["rows"])
    save("binance_btc_quarterly_1d.json", series)
    return got


@source("binance_spot_recent")
def binance_spot_recent():
    """Spot BTCUSDT 1d and 1h klines for the latest months (monthly files lag a few days)."""
    got = {}
    for interval in ("1d", "1h"):
        path = os.path.join(OUT, f"binance_BTCUSDT_{interval}.json")
        rows = json.load(open(path)) if os.path.exists(path) else []
        have = {r[0] for r in rows}
        start = (NOW.date().replace(day=1) - dt.timedelta(days=40)).replace(day=1)
        for y, m in months(start, NOW.date()):
            for r in bv_rows("spot", "BTCUSDT", interval, y, m):
                if r[0] not in have:
                    rows.append(r)
                    have.add(r[0])
        rows.sort()
        save(f"binance_BTCUSDT_{interval}.json", rows)
        got[interval] = len(rows)
    return got


@source("coinbase")
def coinbase():
    got = {}
    for gran, name, span in ((86400, "1d", 300), (3600, "1h", 300)):
        rows = {}
        t0 = dt.datetime(START.year, START.month, START.day, tzinfo=dt.timezone.utc)
        while t0 < NOW:
            t1 = min(t0 + dt.timedelta(seconds=gran * span), NOW)
            q = urllib.parse.urlencode({"granularity": gran, "start": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": t1.strftime("%Y-%m-%dT%H:%M:%SZ")})
            j = jget(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?{q}", headers={"Accept": "application/json"})
            for r in j:  # [time, low, high, open, close, volume]
                rows[int(r[0])] = [int(r[0]), r[3], r[2], r[1], r[4], r[5]]
            t0 = t1
            time.sleep(0.35)
        out = [rows[k] for k in sorted(rows)]
        save(f"coinbase_BTC-USD_{name}.json", out)  # [time s, o, h, l, c, base vol]
        got[name] = len(out)
    return got


@source("deribit")
def deribit():
    base = "https://www.deribit.com/api/v2/public"
    inst = []
    for expired in ("true", "false"):
        j = jget(f"{base}/get_instruments?currency=BTC&kind=future&expired={expired}")
        inst += j.get("result", [])
    start_ms = int(dt.datetime(START.year, START.month, START.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    now_ms = int(NOW.timestamp() * 1000)
    keep = []
    for i in inst:
        exp = i.get("expiration_timestamp") or 0
        if i.get("settlement_period") == "perpetual" or (i.get("settlement_period") == "month" and exp >= start_ms):
            keep.append(i)
    series = {}
    for i in keep:
        name = i["instrument_name"]
        s = max(start_ms, int(i.get("creation_timestamp") or start_ms))
        e = min(now_ms, int(i.get("expiration_timestamp") or now_ms))
        if i.get("settlement_period") == "perpetual":
            s = start_ms
        if e <= s:
            continue
        try:
            q = urllib.parse.urlencode({"instrument_name": name, "start_timestamp": s, "end_timestamp": e, "resolution": "1D"})
            r = jget(f"{base}/get_tradingview_chart_data?{q}").get("result", {})
            series[name] = {"exp": i.get("expiration_timestamp"), "period": i.get("settlement_period"),
                            "t": r.get("ticks", []), "c": r.get("close", []), "v": r.get("volume", [])}
        except Exception as ex:
            series[name] = {"error": str(ex)[:120]}
        time.sleep(0.3)
    save("deribit_btc_futures.json", series)
    return {"instruments": len(keep), "with_data": sum(1 for v in series.values() if v.get("t"))}


@source("cftc_tff")
def cftc_tff():
    where = "upper(market_and_exchange_names) like '%BITCOIN%' OR upper(market_and_exchange_names) like '%ETHER%'"
    tried = {}
    for ds in ("gpe5-46if", "yw9f-hn96"):
        q = urllib.parse.urlencode({"$where": f"({where}) AND report_date_as_yyyy_mm_dd >= '{START.isoformat()}'",
                                    "$limit": 50000, "$order": "report_date_as_yyyy_mm_dd"})
        try:
            rows = jget(f"https://publicreporting.cftc.gov/resource/{ds}.json?{q}")
            if rows:
                save(f"cftc_tff_crypto_{ds}.json", rows)
                tried[ds] = {"rows": len(rows), "markets": sorted({r.get("market_and_exchange_names") for r in rows})}
            else:
                tried[ds] = "empty"
        except Exception as e:
            tried[ds] = f"ERR {str(e)[:120]}"
        time.sleep(1)
    return tried


FRED = ["DGS10", "DFII10", "DGS2", "T10YIE", "DTWEXBGS", "VIXCLS", "NASDAQCOM", "SP500", "BAMLH0A0HYM2",
        "WALCL", "WTREGEN", "RRPONTSYD", "DCOILWTICO"]


@source("fred")
def fred():
    out, got = {}, {}
    for sid in FRED:
        try:
            txt = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={START.isoformat()}", timeout=60).decode("utf-8")
            ser = {}
            for row in csv.reader(io.StringIO(txt)):
                if len(row) < 2 or not row[0][:2].isdigit():
                    continue
                try:
                    ser[row[0]] = float(row[1])
                except ValueError:
                    pass
            out[sid] = ser
            got[sid] = len(ser)
        except Exception as e:
            got[sid] = f"ERR {str(e)[:100]}"
        time.sleep(1)
    save("fred.json", out)
    return got


ALL = {"coinmetrics": coinmetrics, "stablecoins": stablecoins, "funding_hyperliquid": funding_hyperliquid,
       "funding_bitmex": funding_bitmex, "binance_vision": binance_vision, "binance_quarterly": binance_quarterly,
       "binance_spot_recent": binance_spot_recent, "coinbase": coinbase, "deribit": deribit, "cftc_tff": cftc_tff,
       "fred": fred, "coingecko": coingecko}


def main():
    os.makedirs(OUT, exist_ok=True)
    want = None
    lst = os.path.join(ROOT, "research", "fetch_sources.txt")
    if os.path.exists(lst):
        want = [l.strip() for l in open(lst) if l.strip() and not l.startswith("#")]
    rep_path = os.path.join(OUT, "_report.json")
    try:
        old = json.load(open(rep_path)).get("sources", {})
    except (OSError, ValueError):
        old = {}
    for name, fn in ALL.items():
        if want is None or name in want:
            fn()
    REPORT["sources"] = {**old, **REPORT["sources"]}
    json.dump(REPORT, open(rep_path, "w"), indent=1)


if __name__ == "__main__":
    main()
