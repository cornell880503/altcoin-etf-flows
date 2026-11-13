#!/usr/bin/env python3
"""CoinKarma's "BTC ETF 30D Net Flow" as a price-reversal signal (their tweet of 2026-11-13): when the 30-day net
flow of the US spot bitcoin ETFs flips from net outflow to net inflow while their Overall LIQ improves, "price has
a better chance of putting in a short-term bottom"; sustained net outflows of a meaningful size (their red, below
-20,000 BTC) have "typically" been followed by a correction. Their chart shows two such bottoms (2026-03, 2026-08).

The series: scripts/btc_etf30.py (our daily flows in BTC, summed over [D-30, D]; reproduces their chart). Every
event uses session D's 30D value and is entered at D+1 09:00 UTC: a session's total is in our data from 02:00-03:00
UTC the next day but incomplete until BlackRock's figure lands, 04:00-08:00 UTC (2026-10-30: -26 -> -155 -> -643).
Events (a repeat of the same kind within 10 days of the previous one is not counted again):
- flip up: from < 0 to >= 0; "after red": with the 30D flow below -20K within the 60 days before;
- the same, entered only once CoinKarma's Overall LIQ (their Telegram tables, from 2025-02-05) is Buy (>= 2):
  at the first table within 7 days after the flip is known that says Buy while the 30D flow is still >= 0;
- into red: from >= -20K to < -20K; out of red: back to >= -20K; flip down: from >= 0 to < 0.
Measured: BTC's return 3 / 7 / 14 / 30 days later, the lowest and highest price within 14 days, against every
session (entered the same way) as the baseline.
Output: research/etf_flow_backtest.json and a printout."""
import datetime as dt
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import btc_etf30 as E  # noqa: E402
import ck_liq as L  # noqa: E402

HORIZONS = {"3d": 72, "7d": 168, "14d": 336, "30d": 720}
ENTRY_UTC_H = 9  # D+1 09:00 UTC
DEDUP_D = 10
RED_LOOKBACK_D = 60
LIQ_WAIT_H = 7 * 24


def entry_hour(d):
    t = dt.date.fromisoformat(d) + dt.timedelta(days=1)
    return L.hidx(f"{t.isoformat()}T{ENTRY_UTC_H:02d}")


def measure(btc, h0, h):
    i = h - h0
    out = {}
    if not (0 <= i < len(btc)) or not np.isfinite(btc[i]):
        return None
    p0 = btc[i]
    for k, H in HORIZONS.items():
        j = i + H
        out[k] = float(btc[j] / p0 - 1) if j < len(btc) and np.isfinite(btc[j]) else None
    seg = btc[i:i + 337]
    seg = seg[np.isfinite(seg)]
    full = i + 336 < len(btc)
    out["low14"] = float(seg.min() / p0 - 1) if full else None
    out["high14"] = float(seg.max() / p0 - 1) if full else None
    prior = btc[max(0, i - 720):i + 1]
    prior = prior[np.isfinite(prior)]
    out["vs_30d_low"] = float(p0 / prior.min() - 1)
    out["vs_30d_high"] = float(p0 / prior.max() - 1)
    out["price"] = float(p0)
    return out


def summary(evs):
    s = {"n": len(evs)}
    for k in list(HORIZONS) + ["low14", "high14"]:
        v = np.array([e[k] for e in evs if e.get(k) is not None])
        if len(v):
            s[k] = {"n": int(len(v)), "median": round(float(np.median(v)), 4), "mean": round(float(v.mean()), 4),
                    "up": round(float((v > 0).mean()), 3)}
    return s


def liq_tables():
    P = L.ck_pulse()
    hs = np.array([int(h) for h in P["hours"]])
    return hs, P["v"]["overall_liq"], P["st"]["overall_liq"]


def liq_at(T, h):
    """The latest table at or before hour h: (update hour, value, status), or None."""
    hs, v, st = T
    k = int(np.searchsorted(hs, h, side="right")) - 1
    return (int(hs[k]), float(v[k]), st[k]) if k >= 0 else None


def main():
    rows = E.load()
    f30 = E.flow30(rows)
    dates = [r[0] for r in rows]
    first = (dt.date.fromisoformat(dates[0]) + dt.timedelta(days=E.WINDOW_D)).isoformat()
    ds = [d for d in dates if d >= first]
    v = np.array([f30[d] for d in ds])
    D = L.Data()
    btc, h0 = D.close["BTC"], D.h0
    T = liq_tables()
    t_liq0 = int(T[0][0])
    base_all, base_liq = [], []
    for d in ds:
        m = measure(btc, h0, entry_hour(d))
        if m:
            base_all.append(m)
            if entry_hour(d) >= t_liq0:
                base_liq.append(m)
    kinds = {"flip_up": [], "flip_up_after_red": [], "flip_up_after_red_liq_buy": [], "flip_up_liq_buy": [],
             "into_red": [], "out_of_red": [], "flip_down": []}
    last = {}

    def add(kind, d, h, extra=None):
        if kind in last and (dt.date.fromisoformat(d) - dt.date.fromisoformat(last[kind])).days < DEDUP_D:
            return
        last[kind] = d
        m = measure(btc, h0, h)
        if not m:
            return
        e = {"date": d, "entry": L.hstr(h), "flow30_k": round(float(f30[d]), 2)}
        lq = liq_at(T, h) if h >= t_liq0 else None
        if lq:
            e["overall_liq"] = round(lq[1], 2)
            e["overall_liq_st"] = lq[2]
        e.update(extra or {})
        e.update({k: (round(x, 4) if isinstance(x, float) else x) for k, x in m.items()})
        kinds[kind].append(e)

    for i in range(1, len(ds)):
        d, a, b = ds[i], v[i - 1], v[i]
        h = entry_hour(d)
        lo60 = min(f30[q] for q in ds if (dt.date.fromisoformat(d) - dt.date.fromisoformat(q)).days <= RED_LOOKBACK_D and q <= d)
        if a < 0 <= b:
            add("flip_up", d, h, {"min60_k": round(lo60, 1)})
            if lo60 < E.RED:
                add("flip_up_after_red", d, h, {"min60_k": round(lo60, 1)})
            if h >= t_liq0:  # the first Buy table in force from the flip's entry on, within 7 days, with the 30D still >= 0
                hs, vv, st = T
                for k in np.nonzero((hs > h - 8) & (hs <= h + LIQ_WAIT_H))[0]:
                    if st[k] != "Buy":
                        continue
                    he = max(h, int(hs[k]))
                    known = [q for q in ds if entry_hour(q) <= he]
                    if known and f30[known[-1]] >= 0:
                        add("flip_up_liq_buy", d, he, {"min60_k": round(lo60, 1), "flip_entry": L.hstr(h), "wait_h": he - h})
                        if lo60 < E.RED:
                            add("flip_up_after_red_liq_buy", d, he, {"min60_k": round(lo60, 1), "flip_entry": L.hstr(h), "wait_h": he - h})
                        break
        if a >= E.RED > b:
            add("into_red", d, h)
        if a < E.RED <= b:
            add("out_of_red", d, h)
        if a >= 0 > b:
            add("flip_down", d, h)
    # the cycles: from each entry into red to the next flip up: the low in between, when, and the flip against it
    cycles = []
    ups = [e for e in kinds["flip_up"]]
    for e in kinds["into_red"]:
        h1 = L.hidx(e["entry"])
        nxt = [u for u in ups if L.hidx(u["entry"]) > h1]
        h2 = L.hidx(nxt[0]["entry"]) if nxt else h0 + len(btc) - 1
        seg = btc[h1 - h0:h2 - h0 + 1]
        k = int(np.nanargmin(seg))
        c = {"into_red": e["date"], "price": round(e["price"]), "low": round(float(seg[k])), "low_at": L.hstr(h1 + k),
             "low_pct": round(float(seg[k] / e["price"] - 1), 4), "days_to_low": round(k / 24, 1)}
        if nxt:
            c.update({"flip_up": nxt[0]["date"], "days_to_flip": round((h2 - h1) / 24, 1), "flip_price": round(nxt[0]["price"]),
                      "flip_vs_low": round(float(nxt[0]["price"] / seg[k] - 1), 4), "flip_overall_liq": nxt[0].get("overall_liq_st")})
        cycles.append(c)
    # flip ups split by CoinKarma's Overall LIQ at the flip (from 2025-02)
    split = {st: summary([e for e in kinds["flip_up"] if e.get("overall_liq_st") and (e["overall_liq_st"] == "Buy") == (st == "Buy")])
             for st in ("Buy", "not Buy")}
    # the watch's zones (with the hysteresis): every change, entered when the session is final
    zch = {}
    zs = E.zones(f30, ds)
    for (d0, z0), (d1, z1) in zip(zs, zs[1:]):
        if z1 != z0:
            m = measure(btc, h0, entry_hour(d1))
            if m:
                e = {"date": d1, "flow30_k": round(float(f30[d1]), 2)}
                e.update({k: (round(x, 4) if isinstance(x, float) else x) for k, x in m.items()})
                zch.setdefault(f"{z0}>{z1}", []).append(e)
    zone_changes = {k: {"summary": summary(e), "list": e} for k, e in zch.items()}
    into_in = [e for k in ("red>in", "out>in") for e in zch.get(k, [])]
    zone_changes["any>in"] = {"summary": summary(into_in), "list": sorted(into_in, key=lambda e: e["date"])}
    # CoinKarma's green arrow (Overall LIQ and BTC LIQ both Buy) split by the ETF zone known at the table
    P = L.ck_pulse()
    zone_of = dict(zs)
    green = {}
    prev_g = False
    for k, U in enumerate(int(h) for h in P["hours"]):
        g = P["st"]["overall_liq"][k] == "Buy" and P["st"]["btc_liq"][k] == "Buy"
        fresh, prev_g = g and not prev_g, g
        if not g:
            continue
        known = [q for q in ds if entry_hour(q) <= U]
        if not known:
            continue
        m = measure(btc, h0, U)
        if not m:
            continue
        for tag in (("all", "fresh") if fresh else ("all",)):
            green.setdefault(f"{tag}_{zone_of[known[-1]]}", []).append(m)
    green_by_zone = {k: summary(v) for k, v in green.items()}
    out = {"series": "scripts/btc_etf30.py", "sessions": len(ds), "from": ds[0], "to": ds[-1], "cycles": cycles, "flip_up_by_liq": split,
           "zone_changes": zone_changes, "green_by_zone": green_by_zone,
           "baseline_all": summary(base_all), "baseline_from_2025_02": summary(base_liq),
           "events": {k: {"summary": summary(e), "list": e} for k, e in kinds.items()},
           "now": {"date": ds[-1], "flow30_k": round(float(v[-1]), 2), "min60_k": round(min(f30[q] for q in ds[-45:]), 2)}}
    with open(os.path.join(ROOT, "research", "etf_flow_backtest.json"), "w") as f:
        json.dump(out, f, indent=1)
    pct = lambda x: "    —" if x is None else f"{x * 100:+5.1f}%"  # noqa: E731
    print(f"sessions {ds[0]} .. {ds[-1]} ({len(ds)}); now {ds[-1]}: 30D {v[-1]:+.1f}K BTC")
    for name, s in (("baseline, every session", out["baseline_all"]), ("baseline from 2025-02", out["baseline_from_2025_02"])):
        print(f"{name:34s} " + "  ".join(f"{k} med {pct(s[k]['median'])} up {s[k]['up']:.0%}" for k in HORIZONS))
    print("\n== cycles: into red -> low -> next flip up")
    for c in cycles:
        print(f"   {c['into_red']} px {c['price']:>7,} -> low {c['low']:>7,} ({pct(c['low_pct'])}) at {c['low_at']} after {c['days_to_low']:.0f} d"
              + (f" -> flip up {c['flip_up']} after {c['days_to_flip']:.0f} d at {c['flip_price']:,} ({pct(c['flip_vs_low'])} above the low; Overall LIQ {c['flip_overall_liq']})" if "flip_up" in c else " -> no flip up yet"))
    print("== flip ups by Overall LIQ at the flip (2025-02 on): " + "; ".join(
        f"{st}: n {s['n']}, " + ", ".join(f"{k} med {pct(s[k]['median'])} up {s[k]['up']:.0%}" for k in ("7d", "14d", "30d") if k in s) for st, s in split.items()))
    print(f"\n== the watch's zone changes (hysteresis {E.HYST:g}K)")
    for k, z in sorted(zone_changes.items()):
        s = z["summary"]
        print(f"   {k:8s} n {s['n']:2d}  " + "  ".join(f"{h} med {pct(s[h]['median'])} up {s[h]['up']:.0%} (n {s[h]['n']})" for h in HORIZONS if h in s))
    print("\n== CoinKarma's green arrow by ETF zone (tables since 2025-02; returns from the table)")
    for k, s in sorted(green_by_zone.items()):
        print(f"   {k:10s} n {s['n']:3d}  " + "  ".join(f"{h} med {pct(s[h]['median'])} up {s[h]['up']:.0%}" for h in HORIZONS if h in s))
    for kind, evs in kinds.items():
        s = summary(evs)
        print(f"\n== {kind}: {len(evs)} events; " + "  ".join(f"{k} med {pct(s[k]['median'])} up {s[k]['up']:.0%} (n {s[k]['n']})" for k in HORIZONS if k in s)
              + (f"  low14 med {pct(s['low14']['median'])}" if "low14" in s else ""))
        for e in evs:
            lq = f" LIQ {e['overall_liq']:+.2f} {e['overall_liq_st']}" if "overall_liq" in e else ""
            print(f"   {e['date']} entry {e['entry']} 30D {e['flow30_k']:+6.1f}K{lq} px {e['price']:>8,.0f} (vs 30d low {pct(e['vs_30d_low'])}, high {pct(e['vs_30d_high'])})"
                  + " | " + " ".join(f"{k} {pct(e.get(k))}" for k in HORIZONS) + f" | 14d low {pct(e.get('low14'))} high {pct(e.get('high14'))}"
                  + (f" | waited {e['wait_h']}h" if "wait_h" in e else ""))


if __name__ == "__main__":
    main()
