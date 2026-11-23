#!/usr/bin/env python3
"""CoinKarma's "Bitfinex whale" claim (their tweet of 2026-11-23), tested on 2021-01 .. now: when Bitfinex BTCUSD LONGS
jump while the Bitfinex premium is at an extreme, "it has often corresponded to a relative bottom in the short to
medium term". The series and the event are in scripts/bfx_whale.py (longs up 1 % or more over a UTC day with the
4-hour-mean premium reaching 0.40 that day; a repeat within 5 days is the same episode). Every event is entered at
the end of its UTC day on Bitfinex's own hourly closes.

What comes out (2026-11-23):
- the event, 9 earlier cases: higher 7 days later in 5 (median +2.2 %), 14 days later in 6 (+1.8 %), 30 days later
  in 2 (-1.8 %); 4 of the 9 first fell more than 10 % further. From 2023: 4 of 5 up at 7 and 14 days, 2 of 5 at
  30. So a bounce of days to two weeks more often than not, and not a lasting low;
- it is fragile: with the premium at 0.30-0.35 instead of 0.40 the 7-day median is negative and more than half
  fell another 10 % (the grid in "sensitivity");
- the premium alone: CoinKarma's own level (0.25) is common in 2024-25 and says little; the highest readings came
  in stress (LUNA 1.69, mid-2021 1.65, FTX 0.75, 2025-11 0.53), three of those five followed by another 11-16 %;
  in their Telegram tables (2025-02 on) a premium of 0.25 or more was followed by a lower price 14 days later
  58 % of the time;
- the longs as a trend: these traders add while the price falls and reduce into strength (CoinKarma's own guide
  says the accumulation "can take months"), so rising longs have gone with weaker months ahead, not stronger:
  weeks with the longs up 2 % or more in 7 days: 30 days later 46 % up (median -0.7 %); flat: 58 % (+1.6 %);
  within dips (10 % under the 30-day high): 50 % against 60 %.
Output: research/bfx_whale_backtest.json and a printout."""
import datetime as dt
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import bfx_whale as BW  # noqa: E402

HORIZONS = {"3d": 72, "7d": 168, "14d": 336, "30d": 720}


def main():
    D = BW.Data()
    n, px = D.n, D.px

    def fwd(i, H):
        return float(px[i + H] / px[i] - 1) if 0 <= i and i + H < n else None

    def low30(i):
        return float(np.nanmin(px[i:i + 721]) / px[i] - 1) if i + 720 < n else None

    def dd30(i):
        return float(px[i] / np.nanmax(px[max(0, i - 720):i + 1]) - 1)

    def rec(d):
        i = min(D.end(d), n - 1)
        k = D.k[d]
        r = {"date": d, "price": round(float(px[i])), "longs": round(float(D.longs[k])), "d1": round(float(D.longs[k] - D.longs[k - 1])),
             "r1": round(float(D.chg(d)), 4), "premium": round(D.pmax(d), 3), "from_30d_high": round(dd30(i), 3), "low30": low30(D.end(d))}
        r.update({h: fwd(D.end(d), H) for h, H in HORIZONS.items()})
        return {k_: (round(v, 4) if isinstance(v, float) else v) for k_, v in r.items()}

    def summary(recs):
        s = {"n": len(recs)}
        for h in HORIZONS:
            a = np.array([r[h] for r in recs if r[h] is not None])
            if len(a):
                s[h] = {"n": int(len(a)), "up": int((a > 0).sum()), "median": round(float(np.median(a)), 4)}
        lows = [r["low30"] for r in recs if r["low30"] is not None]
        s["fell10"] = {"n": len(lows), "count": int(sum(x <= -0.10 for x in lows))}
        return s

    def dedupe(ds, cool=BW.COOL_D):
        out, last = [], None
        for d in ds:
            if last is None or (dt.date.fromisoformat(d) - dt.date.fromisoformat(last)).days >= cool:
                out.append(d)
                last = d
        return out

    valid = [d for i, d in enumerate(D.days) if i >= 35 and D.end(d) >= 720]
    ev = [rec(d) for d in D.events()]
    grid = {}
    for x in (0.0075, 0.01, 0.015, 0.02):
        for y in (0.30, 0.35, 0.40, 0.45):
            ds = dedupe([d for d in valid if D.chg(d) >= x and D.pmax(d) >= y])
            grid[f"longs>={x:.4f},premium>={y:.2f}"] = summary([rec(d) for d in ds])
    # the premium's highest days
    top, seen = [], []
    for p, d in sorted(((D.pmax(d), d) for d in valid if np.isfinite(D.pmax(d))), reverse=True):
        if any(abs((dt.date.fromisoformat(d) - dt.date.fromisoformat(s)).days) < BW.COOL_D for s in seen):
            continue
        seen.append(d)
        top.append(rec(d))
        if len(top) >= 10:
            break
    # the longs' 7-day change as a state, one day a week
    wk = valid[::7]
    dip = [d for d in valid if dd30(min(D.end(d), n - 1)) <= -0.10][::7]

    def state(ds):
        a = np.array([x for x in (fwd(D.end(d), 720) for d in ds) if x is not None])
        return {"n": int(len(a)), "up": round(float(np.mean(a > 0)), 3), "median": round(float(np.median(a)), 4)} if len(a) else {"n": 0}
    trend = {"all": state(wk), "up2": state([d for d in wk if D.chg(d, 7) >= BW.TREND_UP]), "flat": state([d for d in wk if abs(D.chg(d, 7)) < 0.005]),
             "down": state([d for d in wk if D.chg(d, 7) <= -0.005]),
             "dip_all": state(dip), "dip_up2": state([d for d in dip if D.chg(d, 7) >= BW.TREND_UP]), "dip_not_up": state([d for d in dip if D.chg(d, 7) < 0.005])}
    out = {"module": "scripts/bfx_whale.py", "from": D.days[0], "to": D.days[-1], "rule": {"longs_jump": BW.LONGS_JUMP, "premium": BW.PREM_HI, "cool_days": BW.COOL_D},
           "events": {"list": ev, "summary": summary(ev), "summary_from_2023": summary([e for e in ev if e["date"] >= "2023"])},
           "sensitivity": grid, "premium_top": top, "longs_trend_30d": trend}
    with open(os.path.join(ROOT, "research", "bfx_whale_backtest.json"), "w") as f:
        json.dump(out, f, indent=1)
    pc = lambda x: "    —" if x is None else f"{x * 100:+5.1f}%"  # noqa: E731

    def line(s):
        return f"n {s['n']:2d} | " + " | ".join(f"{h} {s[h]['up']}/{s[h]['n']} {pc(s[h]['median'])}" for h in HORIZONS if h in s) + f" | fell 10%+ more: {s['fell10']['count']}/{s['fell10']['n']}"
    print(f"longs {D.days[0]} .. {D.days[-1]}; the event (longs +{BW.LONGS_JUMP:.0%} in a day, premium >= {BW.PREM_HI}): {line(out['events']['summary'])}")
    print(f"   from 2023: {line(out['events']['summary_from_2023'])}")
    for e in ev:
        print(f"   {e['date']} px {e['price']:>7,} longs {e['d1']:+6,} ({pc(e['r1'])}) premium {e['premium']:.2f} | from 30d high {pc(e['from_30d_high'])} | "
              + " ".join(f"{h} {pc(e[h])}" for h in HORIZONS) + f" | low in 30d {pc(e['low30'])}")
    print("sensitivity:")
    for k, s in grid.items():
        print(f"   {k:32s} {line(s)}")
    print("the premium's highest days: " + "; ".join(f"{e['date']} {e['premium']:.2f} (30d {pc(e['30d'])}, low {pc(e['low30'])})" for e in top))
    print("longs' 7-day change, 30 days later (weekly): " + "; ".join(f"{k} n {s['n']} up {s['up']:.0%} med {pc(s['median'])}" for k, s in trend.items() if s["n"]))


if __name__ == "__main__":
    main()
