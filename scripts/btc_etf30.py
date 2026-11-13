"""The US spot bitcoin ETFs' 30-day net flow in BTC, as CoinKarma charts it ("BTC ETF 30D Net Flow", their tweet
of 2026-11-13): each US session's net flow of all the funds (data/flows/BTC.json, US$ millions) at that day's
16:00 New York BTC price (data/coin_px/BTC.json, Coinbase), summed over the sessions in [D-30, D] (both ends).
Checked against the panel of their chart (digitized, 2025-10 .. 2026-11): r 0.98, slope 0.995, intercept +0.2K;
2026-11-12 -27.0K here, -27.1K on their label. Their colors: red below -20,000 BTC ("sustained net outflows of a
meaningful size"). A session's flows are in our data by 01:00-09:00 UTC the next day."""
import datetime as dt
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOWS = os.path.join(ROOT, "data", "flows", "BTC.json")
PRICES = os.path.join(ROOT, "data", "coin_px", "BTC.json")
WINDOW_D = 30  # sessions in [D - 30 days, D]
RED = -20.0  # thousand BTC: their red level
HYST = 3.0  # thousand BTC: a zone is left only this far past its threshold (the watch's notices; 0 / -20K themselves on entry)
SETTLED_UTC_H = 9  # session D counts from D+1 09:00 UTC: BlackRock's figure lands 04:00-08:00 UTC, the total before is partial
ZONE_TW = {"in": "淨流入", "out": "淨流出", "red": "紅區（淨流出 < 2 萬 BTC）"}


def settled_at(d):
    """The UTC datetime from which session d's total is final."""
    t = dt.date.fromisoformat(d) + dt.timedelta(days=1)
    return dt.datetime(t.year, t.month, t.day, SETTLED_UTC_H, tzinfo=dt.timezone.utc)


def zone_step(z, v):
    """The zone after a session with 30D value v (thousand BTC), from zone z (None: no history)."""
    if z is None:
        return "red" if v < RED else "out" if v < 0 else "in"
    if v >= 0 and (z != "in"):
        return "in"
    if z == "in":
        return z if v >= -HYST else ("red" if v < RED else "out")
    if z == "out":
        return "red" if v < RED else z
    return "out" if v >= RED + HYST else z  # red


def zones(f30, dates):
    """[(date, zone)] replayed over the sessions with the hysteresis (stateless: the same answer on every run)."""
    out, z = [], None
    for d in dates:
        z = zone_step(z, f30[d])
        out.append((d, z))
    return out


def load():
    """[(date, flow US$ millions, flow thousand BTC, price)] for every session, oldest first."""
    rows = json.load(open(FLOWS))["rows"]
    px = json.load(open(PRICES))["rows"]
    out, last_px = [], None
    for r in sorted(rows, key=lambda r: r["date"]):
        d = r["date"]
        p = (px.get(d) or [None, None])[1] or r.get("btcPrice") or last_px
        if not p or r.get("total") is None:
            continue
        last_px = p
        out.append((d, float(r["total"]), float(r["total"]) / p, p))  # $M / $ = million BTC / 1e6 -> x 1e3 = thousand BTC
    return [(d, f, fb * 1e3, p) for d, f, fb, p in out]


def flow30(rows):
    """{date: 30D net flow in thousand BTC} for every session date D."""
    dates = [r[0] for r in rows]
    vals = [r[2] for r in rows]
    out, lo, s = {}, 0, 0.0
    for i, d in enumerate(dates):
        s += vals[i]
        start = (dt.date.fromisoformat(d) - dt.timedelta(days=WINDOW_D)).isoformat()
        while dates[lo] < start:
            s -= vals[lo]
            lo += 1
        out[d] = s
    return out


def leaving(rows, days=7):
    """For the latest session D: [(calendar date t, thousand BTC that leave the window by t)] for t = D+1 .. D+days:
    on day t the window is [t-30, t], so the sessions before t-30 have left it."""
    D = dt.date.fromisoformat(rows[-1][0])
    out = []
    for k in range(1, days + 1):
        t = D + dt.timedelta(days=k)
        start = (t - dt.timedelta(days=WINDOW_D)).isoformat()
        first = (D - dt.timedelta(days=WINDOW_D)).isoformat()
        out.append((t.isoformat(), sum(r[2] for r in rows if first <= r[0] < start)))
    return out
