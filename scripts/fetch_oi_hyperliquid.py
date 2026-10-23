#!/usr/bin/env python3
"""Daily open interest (OI) per coin on Hyperliquid, the largest on-chain perpetuals venue, for the
altcoin leverage radar. Two sources, because Hyperliquid's own API only gives the current OI:

  history   the data behind stats.hyperliquid.xyz: every coin's daily OI in US$ from 2023-06-13; it
            stopped updating on 2026-04-03 (research/probe_oi_venues.py), so the history ends there
  snapshots from 2026-10 on, the current OI from Hyperliquid's API (metaAndAssetCtxs: coins x mark
            price) taken on the first run after midnight UTC and stored as the previous day's close.
            A run later than SNAP_HOURS after midnight takes none: that day stays missing rather than
            stamped hours late (the radar's chain-linked index skips a venue's missing days).
There is no history for 2026-04-04 to the first snapshot. Only the main exchange's coins (crypto,
plus PAXG); the builder-deployed markets (stocks, indexes, oil) are left out.
Rows in data/oi/hyperliquid/ (scripts/oi_store.py): date, symbol (coin, e.g. SOL, kPEPE), oi_usd,
oi_ccy (coins; empty in the history, which has US$ only).
Usage: fetch_oi_hyperliquid.py            history (full) and a snapshot if in the window
       fetch_oi_hyperliquid.py --daily    snapshot only
"""
import argparse
import datetime as dt
import json
import os
import time
import urllib.request

import oi_store as store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "oi", "hyperliquid")
STATS = "https://d2v1fiwobg9w6.cloudfront.net/open_interest"
API = "https://api.hyperliquid.xyz/info"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
FIELDS = ["date", "symbol", "oi_usd", "oi_ccy"]
SNAP_HOURS = 3  # a snapshot counts as the previous day's close only this soon after midnight UTC


def jget(url, data=None, tries=4):
    last = None
    for k in range(tries):
        try:
            h = {"User-Agent": UA, "Accept": "application/json"}
            body = None
            if data is not None:
                h["Content-Type"] = "application/json"
                body = json.dumps(data).encode()
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=120) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(5 * (k + 1))
    raise last


def history(today):
    rows = []
    for x in jget(STATS).get("chart_data", []):
        try:
            d, v = x["time"][:10], float(x["open_interest"])
        except (KeyError, TypeError, ValueError):
            continue
        if d < today and v > 0 and x.get("coin") and ":" not in x["coin"]:
            rows.append([d, x["coin"], f"{v:.6g}", ""])
    return rows


def snapshot(now):
    """Current OI of every coin as the close of the previous UTC day, or [] outside the window."""
    if now.hour >= SNAP_HOURS:
        return [], f"not taken: {now:%H:%M} UTC is more than {SNAP_HOURS}h after midnight"
    meta, ctx = jget(API, data={"type": "metaAndAssetCtxs"})
    day = (now.date() - dt.timedelta(days=1)).isoformat()
    rows = []
    for a, c in zip(meta.get("universe", []), ctx):
        try:
            q, px = float(c.get("openInterest") or 0), float(c.get("markPx") or 0)
        except (TypeError, ValueError):
            continue
        if q > 0 and px > 0 and not a.get("isDelisted") and ":" not in a.get("name", ":"):
            rows.append([day, a["name"], f"{q * px:.6g}", f"{q:.6g}"])
    return rows, f"taken {now:%Y-%m-%d %H:%M} UTC for {day}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true", help="snapshot only")
    ap.add_argument("--budget", type=float, default=5)  # accepted like the other fetchers
    a = ap.parse_args(argv)
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    store.fold(OUT, FIELDS)
    now = dt.datetime.now(dt.timezone.utc)
    rows = [] if a.daily else history(now.date().isoformat())
    snap, note = snapshot(now)
    have = store.keys(OUT)
    snap = [r for r in snap if (r[0], r[1]) not in have]  # the first snapshot of a day stands
    if a.daily and not snap:  # nothing stored: leave _meta.json alone (no commit every run)
        print(f"hyperliquid: no snapshot ({note})", flush=True)
        return 0
    if rows or snap:
        store.write_rows(OUT, FIELDS, rows + snap)
    days = sorted({r[0] for r in rows + snap})
    mpath = os.path.join(OUT, "_meta.json")
    try:
        meta = json.load(open(mpath))
    except (OSError, ValueError):
        meta = {}
    meta.update({"run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily" if a.daily else "full",
                 "history_rows": len(rows), "snapshot_rows": len(snap), "snapshot": note})
    if rows:
        meta["history_through"] = max(r[0] for r in rows)
    if snap:
        meta["latest_snapshot"] = snap[0][0]
    with open(mpath, "w") as f:
        json.dump(meta, f, separators=(",", ":"), sort_keys=True)
    print("meta:", meta, f"days {days[:1]}..{days[-1:]} ({time.time() - t0:.0f}s)", flush=True)
    return len(rows) + len(snap)


if __name__ == "__main__":
    main()
