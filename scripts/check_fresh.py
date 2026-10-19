#!/usr/bin/env python3
"""Exit 1 (so the workflow run shows red) only when the flows are actually behind.

A US trading day counts as due from 18:00 UTC of the next calendar day, when the source has
usually published every fund. It is missing when data/flows/<SYM>.json (BTC ETH SOL XRP HYPE, the
coins kept fund by fund) has no row for it while the asset totals (data/flows_api, data/flows_mcp)
show money moving that day or know nothing of it; a day the totals put at zero is not missing.
One source failing (say, the site's flow pages) is not an alarm as long as another one delivered.

While data stay missing (a source outage nobody here can fix), the alarm fires once per UTC day,
plus whenever another day goes missing; the runs in between pass with a warning. The state is
kept in data/freshness.json (written only when it changes).

Usage: python3 scripts/check_fresh.py [--now 2026-10-20T19:00:00Z]"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calendar_us import is_trading_day, trading_days  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)  # noqa: E731
COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
GRACE_HOURS = 18  # after midnight UTC following the session


def rows_of(path):
    try:
        d = json.load(open(path))
    except (OSError, ValueError):
        return []
    return d.get("rows") or d.get("days") or []


def totals(sym):
    """date -> asset total (US$m) from the API and MCP stores (the API wins where both have it)."""
    out = {}
    for folder in ("flows_mcp", "flows_api"):
        for r in rows_of(P("data", folder, f"{sym}.json")):
            v = r.get("flow", r.get("netFlowUsdM"))
            if r.get("date") and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[str(r["date"])[:10]] = float(v)
    return out


def due(day, now):
    d = dt.date.fromisoformat(day)
    return now >= dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=1, hours=GRACE_HOURS)


STATE = P("data", "freshness.json")


def check(now):
    problems, missing_keys = [], []
    for sym in COINS:
        have = sorted(str(r.get("date", ""))[:10] for r in rows_of(P("data", "flows", f"{sym}.json")) if r.get("date"))
        sessions = [d for d in have if is_trading_day(d)]
        if not sessions:
            problems.append(f"{sym}: no per-fund history")
            continue
        last = sessions[-1]
        start = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()
        today = now.date().isoformat()
        tot = totals(sym)
        missing = [d for d in (trading_days(start, today) if start <= today else [])
                   if due(d, now) and (d not in tot or abs(tot[d]) > 1e-9)]
        missing_keys += [f"{sym} {d}" for d in missing]
        if missing:
            problems.append(f"{sym}: per-fund flows end {last}; missing {', '.join(missing)}"
                            + (" (totals: " + ", ".join(f"{d} {tot[d]:+.1f}M" for d in missing if d in tot) + ")"
                               if any(d in tot for d in missing) else ""))
    return problems, missing_keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="pretend it is this UTC time (for testing)")
    a = ap.parse_args()
    now = (dt.datetime.strptime(a.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) if a.now
           else dt.datetime.now(dt.timezone.utc))
    problems, keys = check(now)
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        state = {}
    today = now.date().isoformat()
    if not problems:
        new = {"missing": [], "alerted_on": None}
        alert = False
        print("flows are current for", ", ".join(COINS))
    else:
        alert = state.get("alerted_on") != today or bool(set(keys) - set(state.get("missing") or []))
        new = {"missing": keys, "alerted_on": today if alert else state.get("alerted_on")}
        for p in problems:
            print(("::error::" if alert else "::warning::") + p + ("" if alert else " (already reported today)"))
    if new != {k: state.get(k) for k in new}:
        json.dump(new, open(STATE, "w"), indent=1)
    if alert:
        sys.exit(1)


if __name__ == "__main__":
    main()
