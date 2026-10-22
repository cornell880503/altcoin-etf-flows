#!/usr/bin/env python3
"""Step 1 of the altcoin leverage indicator: which FREE sources give daily perpetual open interest
(OI) far enough back (ideally from 2022-08), and an altcoin market-cap series (ex BTC, ETH, stables).

Runs on a GitHub runner (workflow probe-oi); no API keys. Every check is independent: a failure is
recorded and the rest continue. Output in research/probe_oi/:
  report.json                    what each source returned (status, depth, granularity, samples)
  binance_metrics_first.json     first day of Binance's daily OI file, per USD-M perpetual
  coinmetrics_mcap_catalog.json  assets with a free daily market cap at Coin Metrics, and since when
  coingecko_top250.json          today's top 250 coins by market cap (id, symbol, US$m)
"""
import concurrent.futures as cf
import csv
import datetime as dt
import io
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "probe_oi")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
NOW = dt.datetime.now(dt.timezone.utc)
REPORT = {"run_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {}}
CHECKPOINTS = ["2021-12-01", "2022-08-01", "2023-01-01", "2024-01-01", "2025-01-01", "2026-01-01"]


class HTTPFail(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status, self.body = status, body


def http(url, timeout=40, tries=2, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = HTTPFail(e.code, e.read()[:400].decode("utf-8", "replace"))
            if e.code in (400, 401, 403, 404, 451):
                raise last
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2 + 3 * k)
    raise last


def jget(url, **kw):
    return json.loads(http(url, **kw))


def err(e):
    if isinstance(e, HTTPFail):
        return {"status": e.status, "body": " ".join(e.body.split())[:200]}
    return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def save(name, obj):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def day_of(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).date().isoformat()


def ms_of(day):
    return int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


SOURCES = []


def source(name):
    def wrap(fn):
        def run():
            t0 = time.time()
            try:
                out = {"ok": True, **(fn() or {})}
            except Exception as e:
                out = {"ok": False, **err(e), "trace": traceback.format_exc()[-600:]}
            out["secs"] = round(time.time() - t0, 1)
            REPORT["sources"][name] = out
            print(f"== {name}: {json.dumps(out)[:2000]}", flush=True)
        SOURCES.append(run)
        return run
    return wrap


# ---------------------------------------------------------------- Binance public data (data.binance.vision)
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
BV = "https://data.binance.vision/"
METRICS = "data/futures/um/daily/metrics/"


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
    keys, prefixes, truncated, nxt = [], [], False, None
    for el in root:
        if tag(el) == "Contents":
            keys += [c.text for c in el if tag(c) == "Key"]
        elif tag(el) == "CommonPrefixes":
            prefixes += [c.text for c in el if tag(c) == "Prefix"]
        elif tag(el) == "IsTruncated":
            truncated = el.text == "true"
        elif tag(el) == "NextMarker":
            nxt = el.text
    return keys, prefixes, truncated, nxt


def s3_list_all(prefix, delimiter=None, pages=60):
    keys, prefixes, marker = [], [], None
    for _ in range(pages):
        k, p, trunc, nxt = s3_list(prefix, delimiter, marker)
        keys += k
        prefixes += p
        if not trunc or not (k or p):
            break
        marker = nxt or (k or p)[-1]
    return keys, prefixes


def metrics_rows(sym, day):
    raw = http(f"{BV}{METRICS}{sym}/{sym}-metrics-{day}.zip")
    z = zipfile.ZipFile(io.BytesIO(raw))
    return list(csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())))


@source("binance_vision")
def binance_vision():
    _, prefixes = s3_list_all(METRICS, delimiter="/")
    syms = sorted(p[len(METRICS):].strip("/") for p in prefixes)
    out = {"symbols": len(syms), "usdt_symbols": sum(1 for s in syms if s.endswith("USDT"))}

    def first_day(sym):
        try:
            k, _, _, _ = s3_list(f"{METRICS}{sym}/", max_keys=1)
            m = re.search(r"(\d{4}-\d{2}-\d{2})", k[0]) if k else None
            return sym, m.group(1) if m else None
        except Exception as e:
            return sym, f"ERR {str(e)[:80]}"

    with cf.ThreadPoolExecutor(8) as ex:
        firsts = dict(ex.map(first_day, syms))
    save("binance_metrics_first.json", firsts)
    good = {s: d for s, d in firsts.items() if d and not d.startswith("ERR") and s.endswith("USDT")}
    out["first_day_overall"] = min(good.values()) if good else None
    out["usdt_symbols_with_file_by"] = {c: sum(1 for d in good.values() if d <= c) for c in CHECKPOINTS}
    out["list_errors"] = sum(1 for d in firsts.values() if not d or d.startswith("ERR"))

    cont = {}  # are the daily files continuous?
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "1000PEPEUSDT", "ZECUSDT", "FTTUSDT"]:
        try:
            keys, _ = s3_list_all(f"{METRICS}{sym}/")
            days = sorted({m.group(1) for k in keys for m in [re.search(r"(\d{4}-\d{2}-\d{2})\.zip$", k)] if m})
            if not days:
                cont[sym] = "none"
                continue
            span = (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days + 1
            cont[sym] = {"first": days[0], "last": days[-1], "files": len(days), "missing_in_span": span - len(days)}
        except Exception as e:
            cont[sym] = err(e)
    out["continuity"] = cont

    samples = {}  # what one daily file holds (granularity, columns), old and fresh
    y1, y2 = (NOW.date() - dt.timedelta(days=1)).isoformat(), (NOW.date() - dt.timedelta(days=2)).isoformat()
    for sym, day in [("BTCUSDT", "2022-08-01"), ("SOLUSDT", "2022-08-01"), ("BTCUSDT", y2), ("BTCUSDT", y1)]:
        try:
            rows = metrics_rows(sym, day)
            samples[f"{sym} {day}"] = {"rows": len(rows) - 1, "header": rows[0], "first": rows[1], "last": rows[-1]}
        except Exception as e:
            samples[f"{sym} {day}"] = err(e)
    out["samples"] = samples

    for label, prefix in [("monthly_metrics", "data/futures/um/monthly/metrics/"),
                          ("um_monthly_klines_SOL_1d", "data/futures/um/monthly/klines/SOLUSDT/1d/"),
                          ("spot_monthly_klines_SOL_1d", "data/spot/monthly/klines/SOLUSDT/1d/")]:
        try:
            k, p, _, _ = s3_list(prefix, delimiter="/", max_keys=3)
            out[label] = (p + k)[:3]
        except Exception as e:
            out[label] = err(e)
    return out


# ---------------------------------------------------------------- Binance live API (expected to block US cloud IPs)
@source("binance_api")
def binance_api():
    out = {}
    for name, url in [("fapi_ping", "https://fapi.binance.com/fapi/v1/ping"),
                      ("oi_hist_1d", "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1d&limit=30"),
                      ("oi_hist_2022", "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1d"
                                       f"&limit=5&startTime={ms_of('2022-08-01')}")]:
        try:
            j = jget(url, tries=1)
            out[name] = {"status": 200, "n": len(j) if isinstance(j, list) else None,
                         "first": day_of(j[0]["timestamp"]) if isinstance(j, list) and j else None}
        except Exception as e:
            out[name] = err(e)
    return out


# ---------------------------------------------------------------- Bybit v5
@source("bybit")
def bybit():
    base, out = "https://api.bybit.com/v5/market", {}
    try:
        j = jget(f"{base}/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1d&limit=3", tries=1)
        out["latest"] = {"retCode": j.get("retCode"), "retMsg": j.get("retMsg"),
                         "list": (j.get("result") or {}).get("list", [])[:2]}
    except Exception as e:
        out["latest"] = err(e)
        return out
    depth = {}
    for sym in ["BTCUSDT", "SOLUSDT", "DOGEUSDT"]:
        for start in ["2020-06-01", "2021-06-01", "2022-08-01", "2023-06-01"]:
            s = ms_of(start)
            try:
                j = jget(f"{base}/open-interest?category=linear&symbol={sym}&intervalTime=1d"
                         f"&startTime={s}&endTime={s + 10 * 86400000}&limit=200", tries=1)
                ts = sorted(int(x["timestamp"]) for x in (j.get("result") or {}).get("list", []))
                depth[f"{sym} {start}"] = {"retCode": j.get("retCode"), "n": len(ts), "first": day_of(ts[0]) if ts else None}
            except Exception as e:
                depth[f"{sym} {start}"] = err(e)
            time.sleep(0.3)
    out["depth"] = depth
    try:
        n, cursor = 0, ""
        for _ in range(10):
            j = jget(f"{base}/instruments-info?category=linear&limit=1000"
                     + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else ""), tries=1)
            res = j.get("result") or {}
            n += sum(1 for x in res.get("list", []) if x.get("quoteCoin") == "USDT" and x.get("contractType") == "LinearPerpetual")
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        out["usdt_perps"] = n
    except Exception as e:
        out["usdt_perps"] = err(e)
    return out


# ---------------------------------------------------------------- OKX rubik statistics
@source("okx")
def okx():
    base, out = "https://www.okx.com/api/v5", {}
    oih = f"{base}/rubik/stat/contracts/open-interest-history"
    try:
        j = jget(f"{oih}?instId=BTC-USDT-SWAP&period=1D&limit=3", tries=1)
        out["latest"] = {"code": j.get("code"), "msg": j.get("msg"), "data": (j.get("data") or [])[:2]}
    except Exception as e:
        out["latest"] = err(e)
        return out
    depth = {}
    for inst in ["BTC-USDT-SWAP", "SOL-USDT-SWAP", "DOGE-USDT-SWAP"]:
        n, earliest, end = 0, None, None
        try:
            for _ in range(20):
                j = jget(f"{oih}?instId={inst}&period=1D&limit=100" + (f"&end={end}" if end else ""), tries=1)
                data = j.get("data") or []
                if not data:
                    break
                n += len(data)
                earliest = min(int(r[0]) for r in data)
                end = earliest - 1
                time.sleep(0.3)
            depth[inst] = {"rows": n, "earliest": day_of(earliest) if earliest else None}
        except Exception as e:
            depth[inst] = {**err(e), "rows_before_error": n, "earliest_before_error": day_of(earliest) if earliest else None}
    out["depth"] = depth
    try:
        j = jget(f"{base}/rubik/stat/contracts/open-interest-volume?ccy=SOL&period=1D", tries=1)
        d = j.get("data") or []
        out["oi_volume_ccy_SOL"] = {"code": j.get("code"), "rows": len(d),
                                    "earliest": day_of(min(int(r[0]) for r in d)) if d else None}
    except Exception as e:
        out["oi_volume_ccy_SOL"] = err(e)
    try:
        j = jget(f"{base}/public/instruments?instType=SWAP", tries=1)
        out["usdt_swaps"] = sum(1 for x in j.get("data", []) if x.get("settleCcy") == "USDT")
    except Exception as e:
        out["usdt_swaps"] = err(e)
    return out


# ---------------------------------------------------------------- CoinGecko (keyless public API)
@source("coingecko")
def coingecko():
    cg, out = "https://api.coingecko.com/api/v3", {}

    def call(name, path):
        try:
            return jget(cg + path, tries=1)
        except Exception as e:
            out[name] = err(e)
            return None
        finally:
            time.sleep(8)

    j = call("global", "/global")
    if j:
        d = j.get("data", {})
        out["global"] = {"total_mcap_usd": d.get("total_market_cap", {}).get("usd"),
                         "pct": dict(sorted(((k, round(v, 2)) for k, v in d.get("market_cap_percentage", {}).items()),
                                            key=lambda x: -x[1])[:8])}
    j = call("chart_max", "/coins/solana/market_chart?vs_currency=usd&days=max")
    if j:
        mc = j.get("market_caps") or []
        out["chart_max"] = {"points": len(mc), "first": day_of(mc[0][0]) if mc else None}
    j = call("global_chart", "/global/market_cap_chart?days=365")
    if j:
        out["global_chart"] = {"keys": list(j)[:5]}
    j = call("markets", "/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1")
    if j:
        save("coingecko_top250.json", [[c["id"], c["symbol"].upper(), round((c.get("market_cap") or 0) / 1e6)] for c in j])
        out["markets"] = {"n": len(j)}
    return out


# ---------------------------------------------------------------- Coin Metrics community API
@source("coinmetrics")
def coinmetrics():
    base, out = "https://community-api.coinmetrics.io/v4", {}
    try:
        j = jget(f"{base}/catalog-v2/asset-metrics?metrics=CapMrktEstUSD,CapMrktCurUSD&page_size=10000")
        assets = {}
        for a in j.get("data", []):
            for m in a.get("metrics", []):
                for f in m.get("frequencies", []):
                    if f.get("frequency") == "1d":
                        assets.setdefault(a["asset"], {})[m["metric"]] = [str(f.get("min_time", ""))[:10],
                                                                         str(f.get("max_time", ""))[:10]]
        save("coinmetrics_mcap_catalog.json", assets)
        first = {a: min(v[0] for v in m.values()) for a, m in assets.items()}
        out["assets_with_mcap"] = len(assets)
        out["assets_by"] = {c: sum(1 for d in first.values() if d and d <= c) for c in CHECKPOINTS}
        out["current_through"] = max((v[1] for m in assets.values() for v in m.values()), default=None)
    except Exception as e:
        out["catalog"] = err(e)
    time.sleep(1)
    for name, url in [("oi_catalog", f"{base}/catalog-v2/market-openinterest?exchanges=binance&page_size=5"),
                      ("oi_timeseries", f"{base}/timeseries/market-openinterest?markets=binance-BTCUSDT-future"
                                        "&start_time=2022-08-01&end_time=2022-08-03&page_size=5")]:
        try:
            j = jget(url, tries=1)
            out[name] = {"rows": len(j.get("data", [])), "sample": j.get("data", [])[:1]}
        except Exception as e:
            out[name] = err(e)
        time.sleep(1)
    return out


# ---------------------------------------------------------------- DefiLlama stablecoins, Coinalyze (needs a key)
@source("defillama_stables")
def defillama_stables():
    j = jget("https://stablecoins.llama.fi/stablecoincharts/all")
    rows = [(day_of(int(r["date"]) * 1000), (r.get("totalCirculatingUSD") or {}).get("peggedUSD")) for r in j]
    return {"days": len(rows), "first": rows[0][0], "last": rows[-1][0], "last_usd_bn": round((rows[-1][1] or 0) / 1e9, 1)}


@source("coinalyze_nokey")
def coinalyze_nokey():
    try:
        jget("https://api.coinalyze.net/v1/exchanges", tries=1)
        return {"status": 200}
    except Exception as e:
        return {"result": err(e)}


def main():
    for run in SOURCES:
        run()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(REPORT, f, indent=1, sort_keys=True)
    print("done:", {k: v.get("ok") for k, v in REPORT["sources"].items()})


if __name__ == "__main__":
    main()
