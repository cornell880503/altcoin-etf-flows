#!/usr/bin/env python3
"""Daily update of the altcoin leverage radar for the website (a step of the fetch workflow).

1. When yesterday (UTC) is not stored yet and Binance has published it (about 7 hours after
   midnight UTC), fetch the new days: open interest (fetch_oi_binance --daily) and Coin Metrics
   market caps (fetch_cm_mcap). The other open-interest venues in alt_radar.VENUES serve yesterday
   right after midnight; each is fetched (fetch_oi_<venue> --daily) on any run while it lacks it.
2. When data/oi/radar.json is older than the newest day both sources have, rebuild it with
   alt_radar.payload(): ALR v2, status (alert / watch / none), every alert with its 30-day path,
   and the backtest statistics. The grid summary comes from research/alt_radar/backtest.json.
Exits 0 when there is nothing to do. --force rebuilds; --no-fetch skips step 1.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oi_store as store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OI = os.path.join(ROOT, "data", "oi")
OUT = os.path.join(OI, "radar.json")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"


def latest(folder):
    rows = store.read_all(os.path.join(OI, folder))
    return max((r[0] for r in rows), default="")


def published(day):
    url = (f"https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/"
           f"BTCUSDT-metrics-{day}.zip.CHECKSUM")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def other_venues(yday):
    """Daily update of the radar's other open-interest venues: each serves a finished UTC day right
    after midnight (hours before Binance), so each is fetched on any run while it lacks yesterday.
    One failing never stops the rest. Returns {venue: latest stored day}."""
    import alt_radar as R
    out = {}
    for venue in R.VENUES:
        if venue == "binance":
            continue
        if latest(venue) < yday:
            try:
                mod = __import__(f"fetch_oi_{venue}")
                mod.main(["--daily", "--budget", "4"])
            except SystemExit:
                pass
            except Exception as e:
                print(f"{venue} daily update failed: {type(e).__name__}: {e}", flush=True)
        out[venue] = latest(venue)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    yday = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    bn, cm = latest("binance"), latest("cm")
    print(f"stored: binance {bn}, coin metrics {cm}; yesterday {yday}", flush=True)
    if not a.no_fetch and (bn < yday or cm < yday):
        if bn < yday and not published(yday):
            print(f"Binance has not published {yday} yet", flush=True)
        else:
            import fetch_cm_mcap
            import fetch_oi_binance
            if bn < yday:
                fetch_oi_binance.main(["--daily", "--budget", "8", "--workers", "32"])
            fetch_cm_mcap.main()
            bn, cm = latest("binance"), latest("cm")
            print(f"after fetch: binance {bn}, coin metrics {cm} ({time.time() - t0:.0f}s)", flush=True)
    if not a.no_fetch:
        print("other venues:", other_venues(yday), flush=True)
    try:
        cur = json.load(open(OUT)).get("asof", "")
    except (OSError, ValueError):
        cur = ""
    if not a.force and cur >= min(bn, cm):
        print(f"radar.json is current ({cur})", flush=True)
        return
    import alt_radar as R
    S = R.build(R.load_oi(), R.load_cm())
    grid = None
    try:
        grid = json.load(open(os.path.join(ROOT, "research", "alt_radar", "backtest.json")))["v2"].get("grid")
    except (OSError, ValueError, KeyError):
        pass
    try:
        meta = json.load(open(os.path.join(OI, "binance", "_meta.json")))
    except (OSError, ValueError):
        meta = {}
    try:
        cmp = json.load(open(os.path.join(ROOT, "research", "alt_radar", "venues.json")))
    except (OSError, ValueError):
        cmp = None
    p = R.payload(S, grid_rows=grid, meta=meta, venues_cmp=cmp)
    p["built_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    os.replace(tmp, OUT)
    st = p["status"]
    print(f"radar.json: asof {p['asof']}, ALR {p['today']['alr']}, state {st['state']} "
          f"(last alert {st['alert']}, day {st['day']}), {len(p['alerts'])} alerts ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
