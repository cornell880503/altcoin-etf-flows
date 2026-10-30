#!/usr/bin/env python3
"""CoinKarma's own Overall LIQ and BTC LIQ, read off the chart in their X post of 2026-10-29 13:59 UTC
(https://x.com/coinkarma_/status/2115805844147442015; TradingView, BTC/USDT 1 h with the LIQ and Overall
LIQ panels, image 2360 x 1207). In the browser, each image column's bar height above or below the panel's
zero line was measured (research/ck_chart/ck_chart_1029_v2.txt: base-36, 3 characters a column,
(overall_px + 60) * 140 + (liq_px + 40)). Here the columns become hours:
  y scale   Overall 34 px a unit (the last bar, 81 px, is labelled 2.38; the axis' 4 sits 134 px up),
            LIQ 23.5 px a unit (64 px = 2.71; 5.00 = 117 px)
  x scale   the day ticks (UTC+8, the chart's time zone) fitted linearly: 26.68 px a day, residuals within
            2 px (about 2 hours); a column per 0.9 hour
  alignment our hour h (the candle that opens at h UTC) <- the bar at h - 1 h: the shift that correlates
            best with our series, and with it CoinKarma's posted wave times fall on the right hours
            (10/13 start 08:00, 10/14 start ~20:00, 10/21 end ~23:00, UTC+8)
  left edge the TradingView logo covers the Overall panel's first columns (to 9/12 01:00 UTC)
Output: data/ck/ck_charts.json, read by scripts/ck_liq.py (overlay and the fidelity note)."""
import datetime as dt
import json
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "research", "ck_chart", "ck_chart_1029_v2.txt")
OUT = os.path.join(ROOT, "data", "ck", "ck_charts.json")
X0 = 95
LABELS = [("2026-09-13", 188.0), ("2026-09-17", 294.0), ("2026-09-21", 398.5), ("2026-09-25", 505.5), ("2026-10-01", 665.0),
          ("2026-10-05", 773.0), ("2026-10-09", 881.0), ("2026-10-17", 1095.0), ("2026-10-21", 1198.5), ("2026-10-25", 1306.0),
          ("2026-11-01", 1492.0), ("2026-11-05", 1600.5), ("2026-11-13", 1815.5), ("2026-11-17", 1922.0), ("2026-11-25", 2133.5)]
OV_PX, LQ_PX, LAG_H, LOGO = 34.0, 23.5, -1, (108, 182)


def main():
    cols = "".join(ln for ln in open(SRC).read().split("\n")[1:] if ln)
    vals = [int(cols[i:i + 3], 36) for i in range(0, len(cols), 3)]
    ov = np.array([v // 140 - 60 for v in vals], float) / OV_PX
    lq = np.array([v % 140 - 40 for v in vals], float) / LQ_PX
    xs = np.arange(X0, X0 + len(vals))
    ov[(xs >= LOGO[0]) & (xs <= LOGO[1])] = np.nan
    d0 = dt.datetime(2026, 9, 13)
    days = np.array([(dt.datetime.fromisoformat(d) - d0).days for d, _ in LABELS], float)
    b, a = np.polyfit(days, np.array([x for _, x in LABELS]), 1)
    out_o, out_b = {}, {}
    t = dt.datetime(2026, 9, 9)
    while t <= dt.datetime(2026, 10, 30):
        x = int(round(a + b * ((t + dt.timedelta(hours=8 + LAG_H)) - d0).total_seconds() / 86400))
        if X0 <= x < X0 + len(vals):
            h = t.strftime("%Y-%m-%dT%H")
            if np.isfinite(ov[x - X0]):
                out_o[h] = round(float(ov[x - X0]), 2)
            out_b[h] = round(float(lq[x - X0]), 2)
        t += dt.timedelta(hours=1)
    chart = {"post": "https://x.com/coinkarma_/status/2115805844147442015", "posted": "2026-10-29T13:59Z",
             "what": "CoinKarma 在 X 貼文附的 TradingView 圖（BTC/USDT 1 小時，LIQ 與 Overall LIQ 兩格），逐欄量柱子高度換回數值",
             "precision": "數值約 ±0.05，時間約 ±1–2 小時", "overall": out_o, "btc": out_b}
    try:
        doc = json.load(open(OUT))
    except (OSError, ValueError):
        doc = {"charts": []}
    doc["charts"] = [c for c in doc["charts"] if c.get("post") != chart["post"]] + [chart]
    with open(OUT, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    print(f"x = {a:.1f} + {b:.3f} days; {len(out_o)} Overall hours ({min(out_o)} .. {max(out_o)}), {len(out_b)} BTC LIQ hours -> {OUT}")


if __name__ == "__main__":
    main()
