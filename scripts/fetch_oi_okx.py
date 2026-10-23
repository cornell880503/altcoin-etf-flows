#!/usr/bin/env python3
"""Daily open interest (OI) of every OKX USDT perpetual swap, for the altcoin leverage radar.

OKX's rubik statistics serve daily OI per contract (UTC days from 2024-02-04), free and without a key,
and answer US cloud IPs (Bybit and Binance's live API do not; research/probe_oi_venues.py). Rows in
data/oi/okx/ (scripts/oi_store.py):
  date    UTC day. OKX's UTC daily row stamped D 00:00 holds the OI at the end of day D (it equals the
          last hourly row of the day, and the row of the running day holds the latest value), the
          same moment as Binance's end-of-day value (~23:55 UTC). The running day is not stored.
  symbol  instId, e.g. SOL-USDT-SWAP
  oi_usd  OI in US$
  oi_ccy  OI in coins (oi_usd / oi_ccy = the price then; the radar's weekend test uses it)
Only contracts OKX lists today: it no longer serves the history of delisted ones ("Instrument ID
doesn't exist"), so OKX's part of the series is built from survivors (Binance's is not).
Modes:
  full (default)  every live USDT swap, paged back to the start of the data
  --daily         the newest rows of each live swap and of every contract with a row in the last 10 days
Time-boxed (--budget minutes) and rate-limited (--rps), so the workflow's commit step always runs.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import http.client
import json
import os
import threading
import time

import oi_store as store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "oi", "okx")
HOST = "www.okx.com"
OI_PATH = "/api/v5/rubik/stat/contracts/open-interest-history"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
FIELDS = ["date", "symbol", "oi_usd", "oi_ccy"]
PERIOD = "1Dutc"
SHIFT = 0  # days from the row's UTC stamp to the day whose end it holds (see the docstring)
_local = threading.local()


class Limiter:
    """At most rps requests a second across threads; a 429 halves the rate for a while."""

    def __init__(self, rps):
        self.gap, self.next, self.lock = 1.0 / rps, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)

    def slow(self):
        with self.lock:
            self.gap = min(self.gap * 2, 2.0)
            self.next = time.time() + 2.0


LIM = Limiter(6)


def get(path, tries=6):
    """GET over a per-thread keep-alive connection; returns the JSON body or None."""
    for k in range(tries):
        LIM.wait()
        c = getattr(_local, "conn", None) or http.client.HTTPSConnection(HOST, timeout=40)
        _local.conn = c
        try:
            c.request("GET", path, headers={"User-Agent": UA, "Accept": "application/json"})
            r = c.getresponse()
            body = r.read()
            if r.status == 200:
                j = json.loads(body)
                if j.get("code") == "0":
                    return j
                if j.get("code") == "50011":  # too many requests
                    LIM.slow()
                    continue
                return j  # other API errors (e.g. unknown instId) are answers, not failures
            if r.status == 429:
                LIM.slow()
                continue
            if r.status in (400, 404):
                return {"code": str(r.status), "data": []}
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass
        _local.conn = None
        time.sleep(1.5 * (k + 1))
    return None


def day_of(ts):
    d = dt.datetime.fromtimestamp(int(ts) / 1000, dt.timezone.utc).date()
    return (d + dt.timedelta(days=SHIFT)).isoformat()


def g6(s):
    try:
        v = float(s)
        return f"{v:.6g}" if v == v else ""
    except (TypeError, ValueError):
        return ""


def rows_of(inst, data, today):
    out = []
    for r in data:
        if len(r) < 4:
            continue
        d = day_of(r[0])
        usd = g6(r[3])
        if d < today and usd and float(usd) > 0:  # only finished UTC days
            out.append([d, inst, usd, g6(r[2])])
    return out


def history(inst, stop, today, pages=40, limit=100):
    """Rows of one contract from the newest back to `stop` (a date), newest page first."""
    rows, end = [], None
    for _ in range(pages):
        j = get(f"{OI_PATH}?instId={inst}&period={PERIOD}&limit={limit}" + (f"&end={end}" if end else ""))
        if j is None:
            return inst, rows, "fail"
        data = j.get("data") or []
        if not data:
            break
        rows += rows_of(inst, data, today)
        earliest = min(int(r[0]) for r in data)
        if day_of(earliest) <= stop or len(data) < limit:
            break
        end = earliest - 1
    return inst, rows, "ok"


def live_swaps():
    j = get("/api/v5/public/instruments?instType=SWAP")
    if not j or j.get("code") != "0":
        raise RuntimeError(f"instrument list failed: {str(j)[:200]}")
    return sorted(x["instId"] for x in j.get("data", []) if x.get("settleCcy") == "USDT")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=80, help="minutes after which no new contract starts")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rps", type=float, default=6)
    ap.add_argument("--since", default="2023-12-31")
    ap.add_argument("--daily", action="store_true")
    a = ap.parse_args(argv)
    LIM.gap = 1.0 / a.rps
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    store.fold(OUT, FIELDS)
    have = store.read_all(OUT)
    last = {}
    for r in have:
        if r[0] > last.get(r[1], ""):
            last[r[1]] = r[0]
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    live = set(live_swaps())
    if a.daily:
        recent = (dt.date.fromisoformat(today) - dt.timedelta(days=10)).isoformat()
        todo = sorted(live | {s for s, d in last.items() if d >= recent})
        jobs = [(s, last.get(s, a.since), 1, 100 if s not in last else 20) for s in todo]
    else:
        jobs = [(s, a.since, 40, 100) for s in sorted(live)]
    print(f"live USDT swaps: {len(live)}, stored contracts: {len(last)}, to ask: {len(jobs)} "
          f"({'daily' if a.daily else 'full'})", flush=True)

    new, stat, done = [], {}, 0
    deadline = t0 + a.budget * 60
    with cf.ThreadPoolExecutor(a.workers) as ex:
        it, pending = iter(jobs), set()
        while True:
            while len(pending) < a.workers * 2 and time.time() < deadline:
                nxt = next(it, None)
                if nxt is None:
                    break
                s, stop, pages, limit = nxt
                pending.add(ex.submit(history, s, stop, today, pages, limit))
            if not pending:
                break
            fin, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fu in fin:
                inst, rows, st = fu.result()
                done += 1
                key = st if st == "fail" else ("rows" if rows else "empty")
                stat[key] = stat.get(key, 0) + 1
                new += rows
                if done % 100 == 0:
                    print(f"  {done}/{len(jobs)} contracts, {len(new)} rows ({time.time() - t0:.0f}s) {stat}", flush=True)
    if new:
        store.write_rows(OUT, FIELDS, new)
    contracts = {}
    for r in store.read_all(OUT):
        c = contracts.setdefault(r[1], [r[0], r[0], 0])
        c[0], c[1], c[2] = min(c[0], r[0]), max(c[1], r[0]), c[2] + 1
    meta = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": "daily" if a.daily else "full", "period": PERIOD, "shift_days": SHIFT,
            "live": len(live), "asked": done, "remaining": len(jobs) - done, "stat": stat, "rows_new": len(new),
            "latest_day": max((c[1] for c in contracts.values()), default=None), "contracts": contracts}
    with open(os.path.join(OUT, "_meta.json"), "w") as f:
        json.dump(meta, f, separators=(",", ":"), sort_keys=True)
    print("meta:", {k: v for k, v in meta.items() if k != "contracts"}, f"({time.time() - t0:.0f}s)", flush=True)
    return len(new)


if __name__ == "__main__":
    main()
