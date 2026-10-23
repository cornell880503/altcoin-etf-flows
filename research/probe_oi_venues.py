#!/usr/bin/env python3
"""Beyond Binance: which exchanges give free daily open interest (OI) for altcoin perpetuals, how far
back, and how much of the altcoin perp market Binance holds today.

Runs on a GitHub runner (workflow oi-venues), no keys; every check is independent. Output in
research/probe_oi/:
  venues.json          what each source returned (status, depth, alignment, samples)
  cg_derivatives.json  today's perpetual OI per exchange and coin (CoinGecko /derivatives), with the
                       share of altcoin OI per exchange
Earlier probe (research/probe_oi.py): Bybit blocks US cloud IPs (CloudFront 403), OKX has per-contract
daily OI from 2023-12-31, Coinalyze needs a key, Coin Metrics' free tier has no OI.
"""
import datetime as dt
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import alt_radar as R  # noqa: E402  (contract lists only; stdlib)

OUT = os.path.join(ROOT, "research", "probe_oi")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
NOW = dt.datetime.now(dt.timezone.utc)
REPORT = {"run_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {}}
UTC = dt.timezone.utc


class HTTPFail(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status, self.body = status, body


def http(url, data=None, timeout=60, tries=2):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
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


def iso(ts):
    ts = int(ts)
    if ts > 10 ** 11:
        ts //= 1000
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def secs(day):
    return int(dt.datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())


def save(name, obj):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


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
            print(f"== {name}: {json.dumps(out, ensure_ascii=False)[:2500]}", flush=True)
        SOURCES.append(run)
        return run
    return wrap


NOT_ALT = {"BTC", "ETH", "XBT", "WBTC", "WETH", "STETH", "USDT", "USDC", "DAI", "FDUSD", "USDE", "TUSD", "BUSD"}


def is_alt(base):
    b = (base or "").upper()
    for p in ("1000000", "1000", "1M"):
        if b.startswith(p) and len(b) > len(p) + 1:
            b = b[len(p):]
            break
    return bool(b) and b not in NOT_ALT and b not in R.EQUITY and b not in R.METALS_COMMODITIES \
        and b not in R.INDEX_STABLE


# ---------------------------------------------------------------- CoinGecko: who holds the OI today
@source("coingecko_derivatives")
def coingecko_derivatives():
    j = jget("https://api.coingecko.com/api/v3/derivatives", timeout=120)
    rows, tot = [], defaultdict(lambda: [0.0, 0.0, 0.0, 0])  # market -> total, major, alt, alt contracts
    for t in j:
        if (t.get("contract_type") or "").lower() != "perpetual":
            continue
        try:
            oi = float(t.get("open_interest") or 0)
        except (TypeError, ValueError):
            continue
        if not oi > 0:
            continue
        m, base = t.get("market") or "?", (t.get("index_id") or "").upper()
        rows.append([m, base, t.get("symbol"), round(oi)])
        tot[m][0] += oi
        if base in ("BTC", "ETH", "XBT"):
            tot[m][1] += oi
        elif is_alt(base):
            tot[m][2] += oi
            tot[m][3] += 1
    alt_all = sum(v[2] for v in tot.values())
    maj_all = sum(v[1] for v in tot.values())
    top = sorted(tot.items(), key=lambda kv: -kv[1][2])[:30]
    share = [[m, round(v[2] / 1e9, 2), round(v[2] / alt_all * 100, 1) if alt_all else None,
              round(v[1] / 1e9, 2), v[3]] for m, v in top]
    save("cg_derivatives.json", {"run_at": REPORT["run_at"], "fields": ["market", "base", "symbol", "oi_usd"],
                                 "rows": rows, "alt_share": share})
    return {"tickers": len(j), "perp_rows": len(rows), "alt_oi_bn": round(alt_all / 1e9, 1),
            "major_oi_bn": round(maj_all / 1e9, 1),
            "alt_share_top": share[:15]}


@source("coingecko_derivative_exchanges")
def coingecko_derivative_exchanges():
    time.sleep(10)
    j = jget("https://api.coingecko.com/api/v3/derivatives/exchanges?order=open_interest_btc_desc&per_page=30")
    return {"exchanges": [[e.get("name"), e.get("open_interest_btc"), e.get("trade_volume_24h_btc"),
                           e.get("number_of_perpetual_pairs")] for e in j]}


# ---------------------------------------------------------------- OKX: alignment of daily rows, delisted contracts
@source("okx")
def okx():
    base, out = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history", {}
    for per in ("1D", "1Dutc"):
        try:
            j = jget(f"{base}?instId=BTC-USDT-SWAP&period={per}&limit=4")
            out[per] = {"code": j.get("code"), "msg": j.get("msg"),
                        "rows": [[iso(r[0])] + r[1:] for r in (j.get("data") or [])]}
        except Exception as e:
            out[per] = err(e)
        time.sleep(0.5)
    try:  # hourly rows of the last ~3 days: which hour does a daily row's value equal?
        j = jget(f"{base}?instId=BTC-USDT-SWAP&period=1H&limit=80")
        out["1H"] = [[iso(r[0]), r[3]] for r in (j.get("data") or [])]
    except Exception as e:
        out["1H"] = err(e)
    for per in ("1Dutc",):  # depth with the UTC-aligned bar
        n, earliest, end = 0, None, None
        try:
            for _ in range(15):
                j = jget(f"{base}?instId=SOL-USDT-SWAP&period={per}&limit=100" + (f"&end={end}" if end else ""))
                data = j.get("data") or []
                if not data:
                    break
                n += len(data)
                earliest = min(int(r[0]) for r in data)
                end = earliest - 1
                time.sleep(0.3)
            out[f"depth_{per}"] = {"rows": n, "earliest": iso(earliest) if earliest else None}
        except Exception as e:
            out[f"depth_{per}"] = {**err(e), "rows": n}
    delisted = {}  # is history still served for contracts OKX no longer lists?
    for inst in ("MATIC-USDT-SWAP", "FTM-USDT-SWAP", "OMG-USDT-SWAP", "RNDR-USDT-SWAP"):
        try:
            j = jget(f"{base}?instId={inst}&period=1Dutc&limit=100&end={secs('2026-01-01') * 1000}")
            d = j.get("data") or []
            delisted[inst] = {"code": j.get("code"), "msg": j.get("msg")[:80] if j.get("msg") else "",
                              "rows": len(d), "range": [iso(min(int(r[0]) for r in d)), iso(max(int(r[0]) for r in d))] if d else None}
        except Exception as e:
            delisted[inst] = err(e)
        time.sleep(0.4)
    out["delisted"] = delisted
    try:
        j = jget("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
        sw = [x for x in j.get("data", []) if x.get("settleCcy") == "USDT"]
        out["usdt_swaps"] = len(sw)
        out["usdt_swaps_alt"] = sum(1 for x in sw if is_alt(x.get("ctValCcy") or x["instId"].split("-")[0]))
        out["sample_inst"] = {k: sw[0].get(k) for k in ("instId", "ctVal", "ctValCcy", "ctMult", "listTime", "state")} if sw else None
    except Exception as e:
        out["usdt_swaps"] = err(e)
    return out


# ---------------------------------------------------------------- Gate: contract_stats depth
@source("gate")
def gate():
    base, out = "https://api.gateio.ws/api/v4/futures/usdt", {}
    j = jget(f"{base}/contract_stats?contract=BTC_USDT&interval=1d&limit=3")
    out["latest"] = [{k: (iso(v) if k == "time" else v) for k, v in r.items()} for r in j[:2]]
    depth = {}
    for c in ("BTC_USDT", "SOL_USDT", "DOGE_USDT"):
        for start in ("2020-06-01", "2021-06-01", "2022-08-01", "2023-06-01", "2024-06-01", "2025-06-01"):
            try:
                r = jget(f"{base}/contract_stats?contract={c}&interval=1d&from={secs(start)}&limit=5")
                depth[f"{c} {start}"] = {"n": len(r), "first": iso(r[0]["time"]) if r else None,
                                         "oi_usd": r[0].get("open_interest_usd") if r else None}
            except Exception as e:
                depth[f"{c} {start}"] = err(e)
            time.sleep(0.25)
    out["depth"] = depth
    for lim in (100, 1000, 2000):
        try:
            r = jget(f"{base}/contract_stats?contract=SOL_USDT&interval=1d&from={secs('2023-01-01')}&limit={lim}")
            out[f"limit_{lim}"] = {"n": len(r), "first": iso(r[0]["time"]) if r else None, "last": iso(r[-1]["time"]) if r else None}
        except Exception as e:
            out[f"limit_{lim}"] = err(e)
        time.sleep(0.3)
    try:
        cs = jget(f"{base}/contracts")
        out["usdt_contracts"] = len(cs)
        out["usdt_contracts_alt"] = sum(1 for c in cs if is_alt(c.get("name", "").split("_")[0]))
        out["delisting_flag"] = sum(1 for c in cs if c.get("in_delisting"))
    except Exception as e:
        out["usdt_contracts"] = err(e)
    try:  # a contract Gate delisted: is its history still served?
        r = jget(f"{base}/contract_stats?contract=MATIC_USDT&interval=1d&from={secs('2024-06-01')}&limit=5")
        out["delisted_MATIC"] = {"n": len(r), "first": iso(r[0]["time"]) if r else None}
    except Exception as e:
        out["delisted_MATIC"] = err(e)
    return out


# ---------------------------------------------------------------- Bitget (current only), Hyperliquid, Bybit domains
@source("bitget")
def bitget():
    j = jget("https://api.bitget.com/api/v2/mix/market/open-interest?symbol=SOLUSDT&productType=USDT-FUTURES")
    return {"current": j.get("data"), "note": "no public OI history endpoint in the v2 API (ccxt endpoint list)"}


@source("hyperliquid")
def hyperliquid():
    j = jget("https://api.hyperliquid.xyz/info", data={"type": "metaAndAssetCtxs"})
    meta, ctx = j[0], j[1]
    tot = alt = 0.0
    n = 0
    for a, c in zip(meta.get("universe", []), ctx):
        try:
            v = float(c.get("openInterest") or 0) * float(c.get("markPx") or 0)
        except (TypeError, ValueError):
            continue
        tot += v
        if is_alt(a.get("name")):
            alt += v
            n += 1
    out = {"assets": len(ctx), "oi_bn": round(tot / 1e9, 2), "alt_oi_bn": round(alt / 1e9, 2), "alt_assets": n}
    live = {}
    for a, c in zip(meta.get("universe", []), ctx):
        try:
            live[a["name"]] = (float(c.get("openInterest") or 0), float(c.get("markPx") or 0))
        except (TypeError, ValueError):
            pass
    # the data behind stats.hyperliquid.xyz: daily OI per coin since 2023-06 (US$ or coins? up to date?)
    try:
        r = jget("https://d2v1fiwobg9w6.cloudfront.net/open_interest", timeout=120)
        rows = r.get("chart_data", []) if isinstance(r, dict) else []
        days = sorted({x["time"][:10] for x in rows})
        coins = {x["coin"] for x in rows}
        last = days[-1] if days else None
        on_last = {x["coin"]: x["open_interest"] for x in rows if x["time"][:10] == last}
        times = sorted({x["time"][11:19] for x in rows})[:5]
        per_day = {d: 0 for d in days}
        for x in rows:
            per_day[x["time"][:10]] += 1
        gaps = [d for d in (dt.date.fromisoformat(days[0]) + dt.timedelta(days=k)
                            for k in range((dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days + 1))
                if d.isoformat() not in per_day] if days else []
        cmp = {}
        for c in ("BTC", "ETH", "SOL", "HYPE", "DOGE", "ZEC"):
            if c in on_last and c in live:
                q, px = live[c]
                cmp[c] = {"stats_last": round(on_last[c]), "live_coins": round(q), "live_usd": round(q * px)}
        out["stats_open_interest"] = {"rows": len(rows), "coins": len(coins), "days": len(days), "first": days[0] if days else None,
                                      "last": last, "times_of_day": times, "missing_days": len(gaps),
                                      "missing_sample": [g.isoformat() for g in gaps[:10]],
                                      "coins_per_day_recent": [[d, per_day[d]] for d in days[-5:]],
                                      "coins_per_day_2024": [[d, per_day[d]] for d in days if d in ("2024-01-01", "2024-06-01", "2025-01-01")],
                                      "keys": sorted(rows[0].keys()) if rows else None, "vs_live": cmp,
                                      "top_keys": sorted(r.keys()) if isinstance(r, dict) else None}
    except Exception as e:
        out["stats_open_interest"] = err(e)
    return out


@source("gate_alignment")
def gate_alignment():
    base, out = "https://api.gateio.ws/api/v4/futures/usdt/contract_stats", {}
    for per, lim in (("1d", 3), ("1h", 30)):
        r = jget(f"{base}?contract=BTC_USDT&interval={per}&limit={lim}")
        out[per] = [[iso(x["time"]), x.get("open_interest_usd"), x.get("open_interest")] for x in r]
        time.sleep(0.3)
    start = int(time.time()) - 179 * 86400
    r = jget(f"{base}?contract=SOL_USDT&interval=1d&from={start}&limit=200")
    out["last_179d"] = {"n": len(r), "first": iso(r[0]["time"]) if r else None, "last": iso(r[-1]["time"]) if r else None}
    return out


@source("bybit_domains")
def bybit_domains():
    out = {}
    for host in ("api.bybit.com", "api.bytick.com", "api2.bybit.com"):
        try:
            j = jget(f"https://{host}/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1d&limit=2", tries=1)
            out[host] = {"retCode": j.get("retCode"), "list": (j.get("result") or {}).get("list", [])[:2]}
        except Exception as e:
            out[host] = err(e)
    return out


def main():
    for run in SOURCES:
        run()
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "venues.json"), "w") as f:
        json.dump(REPORT, f, indent=1, sort_keys=True, ensure_ascii=False)
    print("done:", {k: v.get("ok") for k, v in REPORT["sources"].items()})


if __name__ == "__main__":
    main()
