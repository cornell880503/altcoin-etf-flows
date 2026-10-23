#!/usr/bin/env python3
"""Daily open interest (OI) of every Gate USDT perpetual, for the altcoin leverage radar.

Gate's contract_stats serve daily OI per contract for the last 180 days only (research/
probe_oi_venues.py), free, no key. A full run takes those 180 days; daily runs add the new days, so
the stored history grows from there. Rows in data/oi/gate/ (scripts/oi_store.py):
  date    UTC day whose end the value holds (see SHIFT)
  symbol  contract, e.g. SOL_USDT
  oi_usd  OI in US$ (open_interest_usd)
  oi_ccy  OI in coins (oi_usd / mark price; the radar's weekend test uses the price)
Time-boxed (--budget minutes) and rate-limited (--rps).
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
OUT = os.path.join(ROOT, "data", "oi", "gate")
HOST = "api.gateio.ws"
BASE = "/api/v4/futures/usdt"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
FIELDS = ["date", "symbol", "oi_usd", "oi_ccy"]
SHIFT = 0  # days from a daily row's UTC stamp to the day whose end it holds (probe: gate_alignment)
_local = threading.local()


class Limiter:
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


LIM = Limiter(8)


def get(path, tries=6):
    for k in range(tries):
        LIM.wait()
        c = getattr(_local, "conn", None) or http.client.HTTPSConnection(HOST, timeout=40)
        _local.conn = c
        try:
            c.request("GET", path, headers={"User-Agent": UA, "Accept": "application/json"})
            r = c.getresponse()
            body = r.read()
            if r.status == 200:
                return json.loads(body)
            if r.status == 429:
                LIM.slow()
                continue
            if r.status in (400, 404):
                return []
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass
        _local.conn = None
        time.sleep(1.5 * (k + 1))
    return None


def g6(v):
    try:
        v = float(v)
        return f"{v:.6g}" if v == v else ""
    except (TypeError, ValueError):
        return ""


def stats(contract, start, today):
    j = get(f"{BASE}/contract_stats?contract={contract}&interval=1d&from={start}&limit=200")
    if j is None:
        return contract, None
    rows = []
    for x in j:
        try:
            d = (dt.datetime.fromtimestamp(int(x["time"]), dt.timezone.utc).date() + dt.timedelta(days=SHIFT)).isoformat()
            usd, px = float(x.get("open_interest_usd") or 0), float(x.get("mark_price") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if d < today and usd > 0:  # finished UTC days only
            rows.append([d, contract, g6(usd), g6(usd / px) if px > 0 else ""])
    return contract, rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=30)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rps", type=float, default=8)
    ap.add_argument("--daily", action="store_true", help="only the last 10 days")
    a = ap.parse_args(argv)
    LIM.gap = 1.0 / a.rps
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    store.fold(OUT, FIELDS)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    cs = get(f"{BASE}/contracts")
    if not cs:
        raise RuntimeError("contract list failed")
    names = sorted(c["name"] for c in cs if c.get("name", "").endswith("_USDT"))
    start = int(time.time()) - (10 if a.daily else 179) * 86400
    print(f"contracts: {len(names)} ({'daily' if a.daily else 'full'})", flush=True)
    new, fails, done = [], 0, 0
    deadline = t0 + a.budget * 60
    with cf.ThreadPoolExecutor(a.workers) as ex:
        it, pending = iter(names), set()
        while True:
            while len(pending) < a.workers * 2 and time.time() < deadline:
                nxt = next(it, None)
                if nxt is None:
                    break
                pending.add(ex.submit(stats, nxt, start, today))
            if not pending:
                break
            fin, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fu in fin:
                c, rows = fu.result()
                done += 1
                if rows is None:
                    fails += 1
                else:
                    new += rows
    if new:
        store.write_rows(OUT, FIELDS, new)
    days = sorted({r[0] for r in new})
    meta = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": "daily" if a.daily else "full", "contracts": len(names), "asked": done, "fails": fails,
            "rows_new": len(new), "first_day": days[0] if days else None, "latest_day": days[-1] if days else None,
            "shift_days": SHIFT}
    with open(os.path.join(OUT, "_meta.json"), "w") as f:
        json.dump(meta, f, separators=(",", ":"), sort_keys=True)
    print("meta:", meta, f"({time.time() - t0:.0f}s)", flush=True)
    return len(new)


if __name__ == "__main__":
    main()
