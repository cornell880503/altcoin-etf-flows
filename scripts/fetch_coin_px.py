#!/usr/bin/env python3
"""Each coin's own USD price on every US trading day, at three times:
  10:00 New York   shortly after the flow figures are published ("after publication" returns)
  16:00 New York   when the spot ETFs strike NAV
  daily close      00:00 UTC of the next calendar day (08:00 Singapore): the close of that UTC
                   day's daily candle, the convention crypto venues use. Statistics use this one.
The UTC day of a US trading date contains its whole US session. Sources, the first one that has
the pair:
  1. Coinbase Exchange   <SYM>-USD   1h candles (public API)
  2. Binance spot        <SYM>USDT   1h klines (data.binance.vision archive)
  3. Hyperliquid         <SYM> perp  1h candles (public info API)
Hours the primary source misses are filled from the next one (counted in the report).

data/coin_px/<SYM>.json = {"source", "pair", "schema": 2, "rows": {date: [p10, p16, p_close]}}.
The first run (or a file in the old two-column layout) backfills from START; later runs refresh
the last REFRESH_DAYS days."""
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calendar_us import trading_days  # noqa: E402

COINS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "LINK", "AVAX", "HBAR", "LTC", "BNB", "DOT", "SUI", "NEAR", "TRX", "ZEC"]
START = {"BTC": "2023-12-01", "ETH": "2023-12-01"}
DEFAULT_START = "2024-06-01"
REFRESH_DAYS = 12
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "coin_px")
UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows)"


def http(url, data=None, headers=None, timeout=40, tries=3):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404, 410, 451):
                raise
            time.sleep(2 + 5 * k)
        except Exception as e:  # timeouts, resets
            last = e
            time.sleep(2 + 5 * k)
    raise last


def iso(ts):
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def coinbase(sym, t0, t1):
    """{candle start (epoch s): close} for 1h candles starting in [t0, t1)."""
    out, a, step = {}, t0, 300 * 3600
    while a < t1:
        b = min(a + step, t1)
        rows = json.loads(http(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles"
                               f"?granularity=3600&start={iso(a)}&end={iso(b)}"))
        for r in rows:  # [time, low, high, open, close, volume]
            if a <= int(r[0]) < b:
                out[int(r[0])] = float(r[4])
        a = b
        time.sleep(0.25)
    return out


def binance(sym, t0, t1):
    pair, out = sym + "USDT", {}
    d0, d1 = dt.datetime.fromtimestamp(t0, UTC).date(), dt.datetime.fromtimestamp(t1, UTC).date()
    today = dt.datetime.now(UTC).date()
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        urls = [f"https://data.binance.vision/data/spot/monthly/klines/{pair}/1h/{pair}-1h-{y}-{m:02d}.zip"]
        texts = []
        try:
            texts.append(unzip(http(urls[0], headers={"Accept": "*/*"})))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            d = dt.date(y, m, 1)
            while d.month == m and d < today:
                try:
                    texts.append(unzip(http(f"https://data.binance.vision/data/spot/daily/klines/{pair}/1h/{pair}-1h-{d.isoformat()}.zip",
                                            headers={"Accept": "*/*"})))
                except urllib.error.HTTPError:
                    pass
                d += dt.timedelta(days=1)
                time.sleep(0.05)
        for t in texts:
            for line in t.splitlines():
                f = line.split(",")
                if not f or not f[0].strip().isdigit():
                    continue
                ts = int(f[0])
                ts = ts // 1_000_000 if ts > 10 ** 14 else ts // 1000  # microseconds since 2025, ms before
                if t0 <= ts < t1:
                    out[ts] = float(f[4])
        m += 1
        if m == 13:
            y, m = y + 1, 1
        time.sleep(0.1)
    return out


def unzip(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    return z.read(z.namelist()[0]).decode("utf-8")


def hyperliquid(sym, t0, t1):
    out, a, step = {}, t0 * 1000, 4000 * 3600 * 1000
    while a < t1 * 1000:
        body = {"type": "candleSnapshot", "req": {"coin": sym, "interval": "1h", "startTime": a, "endTime": min(a + step, t1 * 1000)}}
        rows = json.loads(http("https://api.hyperliquid.xyz/info", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}))
        for r in rows or []:
            ts = int(r["t"]) // 1000
            if t0 <= ts < t1:
                out[ts] = float(r["c"])
        a += step
        time.sleep(0.5)
    return out


SOURCES = [("coinbase", coinbase, lambda s: f"{s}-USD"), ("binance", binance, lambda s: f"{s}USDT"),
           ("hyperliquid", hyperliquid, lambda s: f"{s} perp")]


def et_ts(day, hour):
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, hour, tzinfo=NY).timestamp())


def utc_close_ts(day):
    """End of the UTC calendar day `day` (00:00 UTC next day = 08:00 Singapore)."""
    d = dt.date.fromisoformat(day) + dt.timedelta(days=1)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def points(day):
    return [et_ts(day, 10), et_ts(day, 16), utc_close_ts(day)]


def main():
    os.makedirs(OUT, exist_ok=True)
    now = int(time.time())
    today = dt.datetime.now(NY).date().isoformat()
    report = {"run_at": iso(now), "coins": {}}
    for sym in COINS:
        path = os.path.join(OUT, f"{sym}.json")
        try:
            store = json.load(open(path))
        except (OSError, ValueError):
            store = {"rows": {}}
        backfill = len(store["rows"]) < 30 or store.get("schema") != 2
        start = START.get(sym, DEFAULT_START) if backfill else \
            (dt.date.fromisoformat(today) - dt.timedelta(days=REFRESH_DAYS)).isoformat()
        days = [d for d in trading_days(start, today) if et_ts(d, 16) <= now - 120]
        if not days:
            continue
        need = [ts for d in days for ts in points(d) if ts <= now - 120]  # candles ending at these times
        order = SOURCES[:]
        if store.get("source"):  # keep the venue a coin already uses as its primary
            order.sort(key=lambda s: s[0] != store["source"])
        hours, used, filled, errors = {}, None, 0, {}
        for name, fn, pair in order:
            missing = [ts for ts in need if (ts - 3600) not in hours]
            if not missing:
                break
            try:
                got = fn(sym, min(missing) - 3600, max(missing))
            except Exception as e:  # pair not listed there, or the venue is down
                errors[name] = f"{type(e).__name__}: {str(e)[:100]}"
                continue
            if not got:
                errors[name] = "no candles"
                continue
            new = {k: v for k, v in got.items() if k not in hours}
            if used is None:
                used = (name, pair(sym))
            else:
                filled += sum(1 for ts in missing if (ts - 3600) in new)
            hours.update(new)
        if used is None:
            report["coins"][sym] = {"error": errors}
            continue
        rows = {} if backfill else store["rows"]
        if backfill:  # keep earlier values the venues no longer serve (e.g. Hyperliquid's short history)
            for d, v in store["rows"].items():
                rows[d] = (list(v) + [None, None, None])[:3]
        for d in days:
            got3 = [hours.get(ts - 3600) for ts in points(d)]
            if any(v is not None for v in got3):
                old = (list(rows.get(d) or []) + [None, None, None])[:3]
                rows[d] = [g if g is not None else o for g, o in zip(got3, old)]
        store["rows"] = {k: (list(rows[k]) + [None, None, None])[:3] for k in sorted(rows)}
        store.update(source=store.get("source") or used[0], pair=store.get("pair") or used[1], unit="USD (USDT on Binance)",
                     schema=2, clock=["10:00 America/New_York", "16:00 America/New_York",
                                      "daily close: 00:00 UTC of the next day (08:00 Singapore)"],
                     how="close of the 1h candle ending at each time")
        json.dump(store, open(path, "w"), separators=(",", ":"))
        have = [d for d in days if store["rows"].get(d, [None] * 3)[2] is not None]
        report["coins"][sym] = {"source": store["source"], "pair": store["pair"], "backfill": backfill, "days": len(days),
                                "with_close": len(have), "filled_hours": filled, "first": next(iter(store["rows"]), None),
                                "last": have[-1] if have else None, "errors": errors or None}
    json.dump(report, open(os.path.join(OUT, "_report.json"), "w"), indent=1, sort_keys=True)
    print(json.dumps({k: {kk: v.get(kk) for kk in ("source", "with_close", "days", "filled_hours", "error")} for k, v in report["coins"].items()}))


if __name__ == "__main__":
    main()
