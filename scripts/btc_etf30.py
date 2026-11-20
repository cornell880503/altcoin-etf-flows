"""The US spot bitcoin ETFs' 30-day net flow in BTC, as CoinKarma charts it ("BTC ETF 30D Net Flow", their tweet
of 2026-11-13): each US session's net flow of all the funds (data/flows/BTC.json, US$ millions) at that day's
16:00 New York BTC price (data/coin_px/BTC.json, Coinbase), summed over the sessions in [D-30, D] (both ends).
Checked against the panel of their chart (digitized, 2025-10 .. 2026-11): r 0.98, slope 0.995, intercept +0.2K;
2026-11-12 -27.0K here, -27.1K on their label. Their colors: red below -20,000 BTC ("sustained net outflows of a
meaningful size"). A session's flows are in our data by 01:00-09:00 UTC the next day, BlackRock's last; now and
then BlackRock's comes later (2026-11-16: at 11:02 UTC; 2026-11-12: a day late, -48 -> -284), and until then
its field is 0.0 and the total is wrong, so a session with BlackRock at 0.0 is not final before D+2 12:00 UTC
(BlackRock was truly 0 on 2 sessions of 2026)."""
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
LATE_H = 60  # ... or, while BlackRock's field is still 0.0, from D+2 12:00 UTC (hours after D 00:00 UTC)
ZONE_TW = {"in": "淨流入", "out": "淨流出", "red": "紅區（淨流出 < 2 萬 BTC）"}
# inside a red episode (from the first entry into red after a net-inflow spell until the next flip to net inflow):
STOP_REBOUND, STOP_AGE_H = 0.05, 48  # "the fall has stopped": the price 5 % above the episode's low, the low at least 48 h old
BIG_OUT = -500.0  # US$ millions: a heavy single-day outflow
LIQ_HI, OVLIQ_HI = 5.0, 5000.0  # CoinKarma's BTC LIQ and Overall Liquidity (BTC) at the levels seen on the cycle lows


def settled_at(d):
    """The UTC datetime from which session d's total is final."""
    t = dt.date.fromisoformat(d) + dt.timedelta(days=1)
    return dt.datetime(t.year, t.month, t.day, SETTLED_UTC_H, tzinfo=dt.timezone.utc)


def final_rows(rows, now):
    """The sessions whose totals are final at `now` (UTC datetime): the leading run of rows that have passed
    settled_at and have BlackRock's figure (or are old enough that a 0.0 is taken as a real zero)."""
    out = []
    for r in rows:
        d0 = dt.datetime.combine(dt.date.fromisoformat(r[0]), dt.time(0), dt.timezone.utc)
        if now < settled_at(r[0]) or (not r[4] and now < d0 + dt.timedelta(hours=LATE_H)):
            break
        out.append(r)
    return out


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
    """[(date, flow US$ millions, flow thousand BTC, price, BlackRock's flow US$ millions)] for every session, oldest first."""
    rows = json.load(open(FLOWS))["rows"]
    px = json.load(open(PRICES))["rows"]
    out, last_px = [], None
    for r in sorted(rows, key=lambda r: r["date"]):
        d = r["date"]
        p = (px.get(d) or [None, None])[1] or r.get("btcPrice") or last_px
        if not p or r.get("total") is None:
            continue
        last_px = p
        out.append((d, float(r["total"]), float(r["total"]) / p * 1e3, p, float(r.get("blackrock") or 0)))  # $M / $ x 1e3 = thousand BTC
    return out


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
