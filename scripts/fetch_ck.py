#!/usr/bin/env python3
"""Raw data for the indicator library (open copies of CoinKarma's indicators,
docs.coinkarma.co/english/indicators-guide). Every source is free, keyless and answers a GitHub
runner (research/probe_ck.py); each part is optional: a failure is recorded and the rest go on.
Output: data/ck/raw/<part>.json as {"fields": [...], "rows": [[date, ...], ...]} (UTC days), plus
data/ck/raw/_report.json. Parts are incremental: each refetches only its last stretch.

  stablecoins   USDT and USDC circulating supply, daily (DefiLlama, from 2017/2018)
  usdc_usdt     Binance spot USDCUSDT, daily mean / low / high / last of the 1h closes (from 2018-12)
  basis         Binance COIN-M BTCUSD quarterly vs spot BTCUSDT daily closes (from 2020-08)
  funding       BTC perpetual funding per day (8h-equivalent mean): Binance BTCUSDT (monthly files,
                from 2020), Hyperliquid BTC (hourly x 8, from 2023-05), OKX BTC-USDT-SWAP (its API keeps
                about 3 months; stored from the first run on)
  cftc          CFTC Traders in Financial Futures, CME Bitcoin (5 BTC a contract), weekly, from 2018
  bitfinex      Bitfinex BTCUSD and ETHBTC margin longs, USD/USDT funding used, daily close (from 2021);
                funding rates fUSD/fUST (30-day period candles, daily); BTCUSD 1h candles
  coinbase      Coinbase BTC-USD 1h candles (for the Bitfinex premium), from 2021
  okx_lending   OKX USDT lending rate, daily mean (its API history, kept from the first run on)
  fred          WALCL, WTREGEN, RRPONTSYD (net USD liquidity) from 2019
  mstr          MSTR daily close (Yahoo), Strategy's BTC holdings and sats per share (snapshot a day)
  appstore      US App Store top free Finance chart: Coinbase's rank (a snapshot a day, from 2026-10)
  deribit       DVOL daily (from 2021-03), and a daily option-chain snapshot: 30-day 25-delta skew and
                ATM implied vol at 30 / 90 / 180 days
Usage: fetch_ck.py [part ...] [--budget MINUTES]
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "ck", "raw")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC)
TODAY = NOW.date()
REPORT = {"run_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "parts": {}}
DEADLINE = [time.time() + 60 * 60]


def http(url, data=None, timeout=60, tries=3, headers=None):
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
            last = e
            if e.code == 429:
                time.sleep(20 * (k + 1))
                continue
            if e.code in (400, 401, 403, 404, 410, 451):
                raise
        except Exception as e:
            last = e
        time.sleep(3 * (k + 1))
    raise last


def jget(url, **kw):
    return json.loads(http(url, **kw))


def ms(day):
    return int(dt.datetime.combine(day if isinstance(day, dt.date) else dt.date.fromisoformat(day), dt.time(), UTC).timestamp() * 1000)


def day_of(t):
    t = int(t)
    if t > 10 ** 11:
        t //= 1000
    return dt.datetime.fromtimestamp(t, UTC).date().isoformat()


def g(v, k=6):
    return None if v is None or v != v else float(f"{v:.{k}g}")


def load(name):
    p = os.path.join(OUT, f"{name}.json")
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return {}


def save(name, fields, rows, **extra):
    os.makedirs(OUT, exist_ok=True)
    rows = sorted({r[0]: r for r in rows}.values())
    with open(os.path.join(OUT, f"{name}.json"), "w") as f:
        json.dump({"fields": fields, "rows": rows, **extra}, f, separators=(",", ":"))
    return {"rows": len(rows), "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None}


def rows_of(name):
    return {r[0]: r for r in load(name).get("rows", [])}


PARTS = {}


def part(fn):
    PARTS[fn.__name__] = fn
    return fn


def months(first, last):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def bv_klines(path, sym, interval, first, last=None):
    """Klines [open_ms, o, h, l, c] from data.binance.vision: monthly files, then daily files for
    the running month (and the last month while its monthly file is not out yet)."""
    last = last or TODAY - dt.timedelta(days=1)
    out = {}
    for y, m in months(first, last):
        ym = f"{y:04d}-{m:02d}"
        got = False
        if (y, m) < (TODAY.year, TODAY.month):
            try:
                out.update(_kl(f"https://data.binance.vision/data/{path}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{ym}.zip"))
                got = True
            except Exception:
                pass
        if not got:
            d = dt.date(y, m, 1)
            while d.month == m and d <= last:
                if d >= first:
                    try:
                        out.update(_kl(f"https://data.binance.vision/data/{path}/daily/klines/{sym}/{interval}/{sym}-{interval}-{d.isoformat()}.zip"))
                    except Exception:
                        pass
                d += dt.timedelta(days=1)
    return out


def _kl(url):
    z = zipfile.ZipFile(io.BytesIO(http(url, timeout=90, tries=2)))
    out = {}
    for row in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())):
        if not row or not row[0][:1].isdigit():
            continue
        t = int(row[0])
        t = t // 1000 if t > 10 ** 14 else t  # spot files moved to microseconds in 2025
        out[t] = [t, float(row[1]), float(row[2]), float(row[3]), float(row[4])]
    return out


# ---------------------------------------------------------------- parts
@part
def stablecoins():
    rows = {}
    for sid, name in (("1", "usdt"), ("2", "usdc")):
        j = jget(f"https://stablecoins.llama.fi/stablecoin/{sid}")
        for r in j.get("tokens", []):
            d = day_of(int(r["date"]))
            v = (r.get("circulating") or {}).get("peggedUSD")
            if v:
                rows.setdefault(d, [d, None, None])[1 if name == "usdt" else 2] = round(v)
        time.sleep(1)
    return save("stablecoins", ["date", "usdt_usd", "usdc_usd"], list(rows.values()))


@part
def usdc_usdt():
    have = rows_of("usdc_usdt")
    first = dt.date(2018, 12, 1) if not have else dt.date.fromisoformat(max(have)) - dt.timedelta(days=3)
    k = bv_klines("spot", "USDCUSDT", "1h", first)
    days = {}
    for t, o, h, lo, c in k.values():
        days.setdefault(day_of(t), []).append((t, c, lo, h))
    for d, xs in days.items():
        xs.sort()
        cs = [x[1] for x in xs]
        if len(cs) >= 12:
            have[d] = [d, g(sum(cs) / len(cs), 7), g(min(x[2] for x in xs), 7), g(max(x[3] for x in xs), 7), g(cs[-1], 7), len(cs)]
    return save("usdc_usdt", ["date", "mean", "low", "high", "last", "hours"], list(have.values()))


QUARTERLIES = ["200925", "201225", "210326", "210625", "210924", "211231", "220325", "220624", "220930", "221230",
               "230331", "230630", "230929", "231229", "240329", "240628", "240927", "241227", "250328", "250627",
               "250926", "251226", "260327", "260626", "260925", "261225", "270326", "270625"]


@part
def basis():
    have = rows_of("basis")
    first = dt.date(2020, 8, 1) if not have else dt.date.fromisoformat(max(have)) - dt.timedelta(days=5)
    spot = {day_of(t): c for t, o, h, lo, c in bv_klines("spot", "BTCUSDT", "1d", first).values()}
    fut = {}
    for q in QUARTERLIES:
        exp = dt.date(2000 + int(q[:2]), int(q[2:4]), int(q[4:]))
        if exp < first or exp - dt.timedelta(days=200) > TODAY:
            continue
        lo = max(first, exp - dt.timedelta(days=200))
        k = bv_klines("futures/cm", f"BTCUSD_{q}", "1d", lo, min(exp, TODAY - dt.timedelta(days=1)))
        fut[q] = (exp, {day_of(t): c for t, o, h, l2, c in k.values()})
    for d in sorted(spot):
        day = dt.date.fromisoformat(d)
        best = None
        for q, (exp, ser) in fut.items():
            left = (exp - day).days
            if left >= 14 and d in ser and (best is None or left < best[2]):  # the front quarter, rolled 2 weeks before expiry
                best = (q, ser[d], left)
        if best and spot[d] > 0:
            q, f, left = best
            prem = f / spot[d] - 1
            have[d] = [d, g(prem * 100, 5), g(prem * 360 / left * 100, 5), q, left, g(spot[d]), g(f)]
    return save("basis", ["date", "basis_pct", "apy_pct", "contract", "days_left", "spot", "future"], list(have.values()))


@part
def funding():
    have = rows_of("funding")
    # Binance: monthly files (8h settlements)
    first = dt.date(2020, 1, 1) if not have else dt.date.fromisoformat(max(have)) - dt.timedelta(days=40)
    bn = {}
    for y, m in months(first, TODAY):
        if (y, m) >= (TODAY.year, TODAY.month):
            break
        try:
            raw = http(f"https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{y:04d}-{m:02d}.zip", timeout=60, tries=2)
            z = zipfile.ZipFile(io.BytesIO(raw))
            for row in csv.reader(io.StringIO(z.read(z.namelist()[0]).decode())):
                if row and row[0][:1].isdigit():
                    bn.setdefault(day_of(int(row[0])), []).append(float(row[2]))
        except Exception:
            pass
    # Hyperliquid: hourly rates (x 8 = 8h-equivalent)
    hl = {}
    start = ms(dt.date(2023, 5, 10) if not have else dt.date.fromisoformat(max(have)) - dt.timedelta(days=5))
    while start < NOW.timestamp() * 1000 and time.time() < DEADLINE[0]:
        j = jget("https://api.hyperliquid.xyz/info", data={"type": "fundingHistory", "coin": "BTC", "startTime": start})
        if not j:
            break
        for r in j:
            hl.setdefault(day_of(r["time"]), []).append(float(r["fundingRate"]) * 8)
        nxt = max(int(r["time"]) for r in j) + 1
        if nxt <= start or len(j) < 2:
            break
        start = nxt
        time.sleep(0.4)
    # OKX: 8h settlements, about three months of history
    okx, after = {}, None
    for _ in range(40):
        j = jget("https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100" + (f"&after={after}" if after else ""))
        d = j.get("data") or []
        if not d:
            break
        for r in d:
            okx.setdefault(day_of(int(r["fundingTime"])), []).append(float(r.get("realizedRate") or r["fundingRate"]))
        after = min(int(r["fundingTime"]) for r in d)
        time.sleep(0.3)
    mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    for d in set(bn) | set(hl) | set(okx):
        row = have.get(d, [d, None, None, None])
        if d in bn and len(bn[d]) >= 2:
            row[1] = g(mean(bn[d]) * 100, 5)
        if d in hl and len(hl[d]) >= 12:
            row[2] = g(mean(hl[d]) * 100, 5)
        if d in okx and len(okx[d]) >= 2:
            row[3] = g(mean(okx[d]) * 100, 5)
        have[d] = row
    return save("funding", ["date", "binance_pct8h", "hyperliquid_pct8h", "okx_pct8h"], [r for r in have.values() if r[0] < TODAY.isoformat()])


@part
def cftc():
    keep = ["report_date_as_yyyy_mm_dd", "open_interest_all", "asset_mgr_positions_long", "asset_mgr_positions_short",
            "lev_money_positions_long", "lev_money_positions_short", "dealer_positions_long_all", "dealer_positions_short_all",
            "other_rept_positions_long", "other_rept_positions_short", "nonrept_positions_long_all", "nonrept_positions_short_all"]
    q = urllib.parse.urlencode({"$where": "cftc_contract_market_code='133741'", "$limit": 5000, "$order": "report_date_as_yyyy_mm_dd",
                                "$select": ",".join(keep)})
    rows = []
    for r in jget(f"https://publicreporting.cftc.gov/resource/gpe5-46if.json?{q}"):
        def n(k):
            try:
                return int(float(r.get(k) or 0))
            except ValueError:
                return 0
        rows.append([r["report_date_as_yyyy_mm_dd"][:10]] + [n(k) for k in keep[1:]])
    return save("cftc", ["date", "oi", "am_long", "am_short", "lev_long", "lev_short", "dealer_long", "dealer_short",
                         "other_long", "other_short", "nonrept_long", "nonrept_short"], rows)


def bfx_daily(key, have_last, first):
    """Daily close (last minute of the UTC day) of a Bitfinex stats1 1m series."""
    out = {}
    start = ms(first if not have_last else dt.date.fromisoformat(have_last) - dt.timedelta(days=2))
    while start < NOW.timestamp() * 1000 and time.time() < DEADLINE[0]:
        j = jget(f"https://api-pub.bitfinex.com/v2/stats1/{key}/hist?limit=10000&sort=1&start={start}")
        if not j:
            break
        for t, v in j:
            out[day_of(t)] = v  # sorted ascending: the last minute of each day wins
        nxt = max(int(r[0]) for r in j) + 60000
        if nxt <= start:
            break
        start = nxt
        time.sleep(2.2)
    return out


@part
def bitfinex():
    have = rows_of("bitfinex")
    last = max(have) if have else None
    first = dt.date(2021, 1, 1)
    series = {}
    for name, key in (("btcusd_long", "pos.size:1m:tBTCUSD:long"), ("ethbtc_long", "pos.size:1m:tETHBTC:long"),
                      ("fusd_used", "credits.size:1m:fUSD"), ("fust_used", "credits.size:1m:fUST")):
        series[name] = bfx_daily(key, last, first)
    rates = {}
    for name, key in (("fusd_rate", "trade:1D:fUSD:p30"), ("fust_rate", "trade:1D:fUST:p30")):
        j = jget(f"https://api-pub.bitfinex.com/v2/candles/{key}/hist?limit=10000&sort=1&start={ms(first)}")
        rates[name] = {day_of(r[0]): r[2] for r in j}  # close: rate per day
        time.sleep(2.2)
    names = ["btcusd_long", "ethbtc_long", "fusd_used", "fust_used", "fusd_rate", "fust_rate"]
    for d in set().union(*[set(v) for v in series.values()], *[set(v) for v in rates.values()]):
        row = have.get(d, [d] + [None] * len(names))
        for i, n in enumerate(names):
            src = series.get(n) or rates.get(n)
            if src and d in src:
                row[1 + i] = g(src[d])
        have[d] = row
    # BTCUSD 1h closes for the premium versus Coinbase
    hb = load("bitfinex_1h")
    hrows = {r[0]: r for r in hb.get("rows", [])}
    start = ms(first) if not hrows else (max(hrows) - 3 * 86400) * 1000
    while start < NOW.timestamp() * 1000 and time.time() < DEADLINE[0]:
        j = jget(f"https://api-pub.bitfinex.com/v2/candles/trade:1h:tBTCUSD/hist?limit=10000&sort=1&start={start}")
        if not j:
            break
        for r in j:
            hrows[int(r[0]) // 1000] = [int(r[0]) // 1000, r[2]]
        nxt = max(int(r[0]) for r in j) + 3600000
        if nxt <= start:
            break
        start = nxt
        time.sleep(2.2)
    save("bitfinex_1h", ["t", "close"], list(hrows.values()))
    return save("bitfinex", ["date"] + names, list(have.values()))


@part
def coinbase():
    hb = load("coinbase_1h")
    rows = {r[0]: r for r in hb.get("rows", [])}
    t0 = dt.datetime(2021, 1, 1, tzinfo=UTC) if not rows else dt.datetime.fromtimestamp(max(rows) - 3 * 86400, UTC)
    while t0 < NOW and time.time() < DEADLINE[0]:
        t1 = min(t0 + dt.timedelta(hours=300), NOW)
        q = urllib.parse.urlencode({"granularity": 3600, "start": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": t1.strftime("%Y-%m-%dT%H:%M:%SZ")})
        for r in jget(f"https://api.exchange.coinbase.com/products/BTC-USD/candles?{q}", headers={"Accept": "application/json"}):
            rows[int(r[0])] = [int(r[0]), r[4]]  # [time, low, high, open, close, volume]
        t0 = t1
        time.sleep(0.35)
    return save("coinbase_1h", ["t", "close"], list(rows.values()))


@part
def okx_lending():
    have = rows_of("okx_lending")
    acc, after = {}, None
    for _ in range(200):
        j = jget("https://www.okx.com/api/v5/finance/savings/lending-rate-history?ccy=USDT&limit=100" + (f"&after={after}" if after else ""))
        d = j.get("data") or []
        if not d:
            break
        for r in d:
            acc.setdefault(day_of(int(r["ts"])), []).append(float(r["rate"]))
        oldest = min(int(r["ts"]) for r in d)
        if after and oldest >= after:
            break
        after = oldest
        if have and day_of(oldest) < max(have):  # reached the stored days
            break
        time.sleep(0.35)
    for d, xs in acc.items():
        if d < TODAY.isoformat():
            have[d] = [d, g(sum(xs) / len(xs) * 100, 5), len(xs)]
    return save("okx_lending", ["date", "usdt_rate_pct_apr", "n"], list(have.values()))


@part
def fred():
    out = {}
    for sid in ("WALCL", "WTREGEN", "RRPONTSYD"):
        txt = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=2019-01-01", timeout=60).decode()
        for row in csv.reader(io.StringIO(txt)):
            if len(row) >= 2 and row[0][:2] == "20":
                try:
                    out.setdefault(row[0], {})[sid] = float(row[1])
                except ValueError:
                    pass
        time.sleep(1)
    return save("fred", ["date", "WALCL", "WTREGEN", "RRPONTSYD"],
                [[d, v.get("WALCL"), v.get("WTREGEN"), v.get("RRPONTSYD")] for d, v in out.items()])


@part
def mstr():
    have = rows_of("mstr")
    j = jget("https://query1.finance.yahoo.com/v8/finance/chart/MSTR?range=max&interval=1d")
    res = j["chart"]["result"][0]
    for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c:
            d = day_of(t)
            row = have.get(d, [d, None, None, None])
            row[1] = g(c)
            have[d] = row
    try:  # Strategy's own KPIs: BTC holdings and sats per share (today only)
        k = jget("https://api.strategy.com/btc/bitcoinKpis").get("results", {})
        d = TODAY.isoformat()
        row = have.get(d, [d, None, None, None])
        row[2] = float(str(k.get("btcHoldings", "")).replace(",", "") or 0) or None
        row[3] = g(k.get("satsPerShare"))
        have[d] = row
    except Exception as e:
        REPORT.setdefault("notes", {})["strategy_kpis"] = str(e)[:120]
    return save("mstr", ["date", "close", "btc_holdings", "sats_per_share"], list(have.values()))


@part
def appstore():
    have = rows_of("appstore")
    j = jget("https://itunes.apple.com/us/rss/topfreeapplications/limit=200/genre=6015/json")
    names = [e["im:name"]["label"] for e in j["feed"]["entry"]]
    rank = lambda s: next((i + 1 for i, n in enumerate(names) if s in n), None)  # noqa: E731
    d = TODAY.isoformat()
    have[d] = [d, rank("Coinbase"), rank("Robinhood: Trading"), rank("Kraken"), rank("Crypto.com"), rank("Binance.US"),
               rank("Interactive Brokers") or rank("IBKR"), rank("Yahoo Finance"), len(names)]
    return save("appstore", ["date", "coinbase", "robinhood", "kraken", "cryptocom", "binanceus", "ibkr", "yahoo", "listed"],
                list(have.values()))


def _bs_delta(S, K, T, iv, call):
    if T <= 0 or iv <= 0:
        return None
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    nd = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return nd if call else nd - 1


@part
def deribit():
    b = "https://www.deribit.com/api/v2/public"
    dv = rows_of("deribit_dvol")
    start = ms(dt.date(2021, 3, 1) if not dv else dt.date.fromisoformat(max(dv)) - dt.timedelta(days=5))
    while start < NOW.timestamp() * 1000:
        end = min(start + 900 * 86400000, int(NOW.timestamp() * 1000))
        q = urllib.parse.urlencode({"currency": "BTC", "start_timestamp": start, "end_timestamp": end, "resolution": "1D"})
        r = jget(f"{b}/get_volatility_index_data?{q}").get("result", {})
        for t, o, h, lo, c in r.get("data", []):
            d = day_of(t)
            dv[d] = [d, g(c, 5)]
        start = end + 1
        time.sleep(0.5)
    save("deribit_dvol", ["date", "dvol"], list(dv.values()))
    # option chain snapshot: 25-delta skew and ATM IV by tenor (forward-only history)
    chain = jget(f"{b}/get_book_summary_by_currency?currency=BTC&kind=option").get("result", [])
    now_ms = NOW.timestamp() * 1000
    exps = {}
    for o in chain:
        name = o["instrument_name"]  # BTC-31OCT26-87000-C
        try:
            _, ex, k, cp = name.split("-")
            exp = dt.datetime.strptime(ex, "%d%b%y").replace(hour=8, tzinfo=UTC)
        except ValueError:
            continue
        T = (exp.timestamp() * 1000 - now_ms) / (365.25 * 86400000)
        S, iv = o.get("underlying_price"), o.get("mark_iv")
        if not S or not iv or T <= 2 / 365:
            continue
        dlt = _bs_delta(S, float(k), T, iv / 100, cp == "C")
        exps.setdefault(round(T, 5), []).append((float(k), cp, iv, dlt, S))
    tenors = {}
    for T, opts in exps.items():
        S = opts[0][4]
        calls = [(abs(x[3] - 0.25), x[2]) for x in opts if x[1] == "C" and x[3] is not None]
        puts = [(abs(x[3] + 0.25), x[2]) for x in opts if x[1] == "P" and x[3] is not None]
        atm = sorted(opts, key=lambda x: abs(x[0] - S))[:2]
        if calls and puts and atm:
            tenors[T] = {"skew25": min(puts)[1] - min(calls)[1], "atm": sum(x[2] for x in atm) / len(atm)}
    def at(days, key):
        ts = sorted(tenors)
        x = days / 365.25
        lo = [t for t in ts if t <= x]
        hi = [t for t in ts if t >= x]
        if not lo or not hi:
            return None
        a, b2 = lo[-1], hi[0]
        if a == b2:
            return tenors[a][key]
        w = (x - a) / (b2 - a)
        return tenors[a][key] * (1 - w) + tenors[b2][key] * w
    snap = rows_of("deribit_snap")
    d = TODAY.isoformat()
    snap[d] = [d, g(at(30, "skew25"), 4), g(at(30, "atm"), 4), g(at(90, "atm"), 4), g(at(180, "atm"), 4), len(chain)]
    return save("deribit_snap", ["date", "skew25_30d", "atm_30d", "atm_90d", "atm_180d", "options"], list(snap.values()),
                dvol_rows=len(dv))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parts", nargs="*")
    ap.add_argument("--budget", type=float, default=60, help="minutes for the long backfills (Bitfinex, Coinbase)")
    a = ap.parse_args()
    DEADLINE[0] = time.time() + a.budget * 60
    want = a.parts or list(PARTS)
    for name in want:
        t0 = time.time()
        try:
            out = {"ok": True, **(PARTS[name]() or {})}
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "trace": traceback.format_exc()[-400:]}
        out["secs"] = round(time.time() - t0, 1)
        REPORT["parts"][name] = out
        print(f"== {name}: {json.dumps(out)[:600]}", flush=True)
    os.makedirs(OUT, exist_ok=True)
    rp = os.path.join(OUT, "_report.json")
    try:
        old = json.load(open(rp))
    except (OSError, ValueError):
        old = {}
    old.setdefault("parts", {}).update(REPORT["parts"])
    old["run_at"] = REPORT["run_at"]
    with open(rp, "w") as f:
        json.dump(old, f, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
