#!/usr/bin/env python3
"""CoinKarma's Karma Index, read off the chart in their tweet of 2026-11-26 ("the cyclical indicator shows a buy signal
again"): BTC/USDT daily candles from 2022 with the Karma Index below as a histogram, grey between 20 and 80, green
below 20 (their buy zone), red above 80 (their sell zone).

They publish no formula (five groups of inputs: spot liquidity, on-chain, off-exchange sentiment, exchange flows,
derivatives) and the value is not in their Telegram table, so the chart is the only history there is. The chart
itself is not in the repo (their image, on Kevin's phone screenshot, 1206 x 2622); this script needs its path:

    python3 research/karma_index_digitize.py /path/to/screenshot.png

What it does, in the screenshot's own pixel coordinates (the constants below belong to that one image):
1. the time axis: the candles are digitized column by column and fitted against our own hourly BTC closes
   (y = a + b ln price; the pixel of the last day and the pixels per day by grid search). 2026-11-26: residual
   1.8 px (about 1.8 % in price), 0.4055 px per day (one pixel column = 2.47 days), every section of the chart
   within a quarter of a pixel of the same fit;
2. the index panel: for each pixel column, how green and how red the bars are (the share of the column's days below
   20 / above 80), and the bar heights (mean, lowest and highest within the column; index 0 at y 2276.75,
   0.93 px per index unit, from the panel's own 0/40/60/80 lines). Inside the yellow boxes they drew over the buy
   zones the grey-based height reads 1-2 units high; `g_mean` (from the green channel only) is the one to use there.

One pixel column is 2.5 days and the screenshot is slightly blurred sideways, so a date read from it is good to
about +-3 days and a level to about +-1.5 units. Output: research/ck_x/karma_index_2026-11-26.json."""
import datetime as dt
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "ck_x", "karma_index_2026-11-26.json")
UTC = dt.timezone.utc
END = dt.datetime(2026, 11, 27, tzinfo=UTC)  # the end of the chart's last (still open) daily candle
PRICE_BOX = (215, 1100, 1941, 2172)  # x0, x1, y0, y1 of the candles (below the toolbar's own price text)
PANEL_X = (221, 958)  # the index panel's columns
PANEL_TOP, PANEL_BASE = 2190, 2276  # rows scanned; the bars end inside row 2276
ZERO, PXU = 2276.75, 0.93  # y of index 0, pixels per index unit


def hourly_log_price():
    rows = json.load(open(os.path.join(ROOT, "data", "ck", "raw", "bitfinex_1h.json")))["rows"]
    t = np.array([r[0] for r in rows])
    p = np.array([float(r[1]) if r[1] else np.nan for r in rows])
    t0 = int(t[0])
    n = int((t[-1] - t0) // 3600 + 1)
    lp = np.full(n, np.nan)
    lp[(t - t0) // 3600] = np.log(p)
    for i in range(1, n):
        if not np.isfinite(lp[i]):
            lp[i] = lp[i - 1]
    return t0, n, np.concatenate([[0.0], np.cumsum(lp)])


def fit_time_axis(a):
    """The pixel whose right edge is END, the pixels per day, and y = c0 + c1 ln(price)."""
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    x0, x1, y0, y1 = PRICE_BOX
    cand = ((G - R > 25) & (B - R > 10) & (G > 70)) | ((R - G > 45) & (R - B > 35) & (R > 100))  # teal or red candle pixels
    xs, ymid = [], []
    for x in range(x0, x1):
        ys = np.nonzero(cand[y0:y1, x])[0]
        if len(ys):
            xs.append(x)
            ymid.append((ys.min() + ys.max()) / 2 + y0)
    xs, ymid = np.array(xs), np.array(ymid)
    t0, n, C = hourly_log_price()
    tmax = t0 + n * 3600
    end = END.timestamp()

    def cum(tt):
        h = np.clip((tt - t0) / 3600.0, 0, n)
        i = np.floor(h).astype(int)
        return C[i] + (h - i) * (C[np.minimum(i + 1, n)] - C[i])

    def series(x_end, s):  # the mean log price over the time each column covers
        t1 = end + (xs - x_end) / s * 86400.0
        t_0 = t1 - 86400.0 / s
        den = (np.clip(t1, t0, tmax) - np.clip(t_0, t0, tmax)) / 3600.0
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0, (cum(t1) - cum(t_0)) / den, np.nan)

    best = None
    for x_end in np.arange(945, 966.01, 0.25):
        for s in np.arange(0.380, 0.4301, 0.00025):
            v = series(x_end, s)
            ok = np.isfinite(v)
            A = np.vstack([np.ones(ok.sum()), v[ok]]).T
            coef, *_ = np.linalg.lstsq(A, ymid[ok], rcond=None)
            rmse = float(np.sqrt(np.mean((ymid[ok] - A @ coef) ** 2)))
            if best is None or rmse < best[0]:
                best = (rmse, float(x_end), float(s), float(coef[0]), float(coef[1]), int(ok.sum()))
    return best


def main():
    from PIL import Image
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    a = np.asarray(Image.open(sys.argv[1]).convert("RGB")).astype(float)
    rmse, x_end, s, c0, c1, n = fit_time_axis(a.astype(int))
    u_end = x_end + 1.0  # continuous x (pixel x covers [x, x+1)) of END

    def t_of(u):
        return END + dt.timedelta(days=(u - u_end) / s)

    print(f"time axis: {n} candle columns, residual {rmse:.2f} px ({abs(rmse / c1) * 100:.1f} % in price); {s:.4f} px per day ({1 / s:.2f} days per pixel); "
          f"2022-01-01 at x {u_end - (END - dt.datetime(2022, 1, 1, tzinfo=UTC)).days * s:.1f}")
    L = 0.3 * a[..., 0] + 0.59 * a[..., 1] + 0.11 * a[..., 2]

    def border(p):  # the bright yellow line of one of their boxes
        return p[0] > 60 and p[1] > 55 and p[2] < 0.62 * p[0] and p[0] - p[2] > 35 and abs(p[0] - p[1]) < 25

    cols = []
    for x in range(*PANEL_X):
        cr, cg, cb = np.median(a[2266:2275, x], axis=0)  # rows every bar fills
        lbar = float(np.median(L[2266:2275, x]))
        lbg = float(np.median(L[2278:2281, x]))  # below the bars: the local background (tinted inside a box)
        tinted = bool(np.median(a[2278:2281, x, 0]) - np.median(a[2278:2281, x, 2]) > 10)
        rec = {"x": x, "from": t_of(x).strftime("%Y-%m-%dT%H:%M"), "to": t_of(x + 1).strftime("%Y-%m-%dT%H:%M"), "box": tinted,
               "green": round(float(np.clip((cg - max(cr, cb)) / 118.0, 0, 1)), 3), "red": round(float(np.clip((cr - max(cg, cb)) / 168.0, 0, 1)), 3),
               "mean": None, "lo": None, "hi": None, "g_mean": None, "g_lo": None}
        if lbar - lbg >= 12:
            fs, miss = {}, 0
            for y in range(PANEL_BASE - 1, PANEL_TOP, -1):
                f = 0.0 if (tinted and border(a[y, x])) else float(np.clip((L[y, x] - lbg) / (lbar - lbg), 0, 1.0))
                if f < 0.10:
                    miss += 1
                    if miss >= 3:
                        break
                    f = 0.0
                else:
                    miss = 0
                fs[y] = f
            lo_top = PANEL_BASE
            for y in range(PANEL_BASE - 1, PANEL_TOP, -1):
                if fs.get(y, 0) >= 0.93:
                    lo_top = y
                else:
                    break
            anyf = [y for y in fs if fs[y] >= 0.12]
            rec.update({"mean": round((sum(fs.values()) + 0.75) / PXU, 2), "lo": round((ZERO - lo_top) / PXU, 2),
                        "hi": round((ZERO - (min(anyf) if anyf else lo_top)) / PXU, 2)})
            if rec["green"] >= 0.3:  # the green part's own height, from the green channel only (the box tint has none)
                den = cg - max(cr, cb)
                seg = a[2236:2276, x]
                fg = np.clip((seg[:, 1] - np.maximum(seg[:, 0], seg[:, 2])) / den, 0, 1)
                g_top = PANEL_BASE
                for y in range(PANEL_BASE - 1, 2235, -1):
                    if fg[y - 2236] >= 0.93:
                        g_top = y
                    else:
                        break
                rec.update({"g_mean": round(float(fg.sum() + 0.75) / PXU, 2), "g_lo": round((ZERO - g_top) / PXU, 2)})
        cols.append(rec)
    out = {"source": "CoinKarma (@coinkarma_), chart in their post of 2026-11-26 (Karma Index, levels 20 / 80, last value 19.00); digitized from a phone screenshot",
           "read_me": "one record per pixel column (2.47 days): green / red = the share of the column below 20 / above 80; mean, lo, hi = the bar heights in index units "
                      "(grey-based; 1-2 units high inside their yellow boxes); g_mean, g_lo = the green part's own height. Dates +-3 days, levels +-1.5.",
           "fit": {"px_per_day": s, "x_of_end": u_end, "end": END.isoformat(), "rmse_px": round(rmse, 3), "y_of_ln_price": [round(c0, 2), round(c1, 3)], "zero_y": ZERO, "px_per_unit": PXU},
           "cols": cols}
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    g = [c for c in cols if c["green"] >= 0.5]
    r = [c for c in cols if c["red"] >= 0.5]
    print(f"{len(cols)} columns {cols[0]['from'][:10]} .. {cols[-1]['to'][:10]}; mostly green {len(g)} (about {len(g) / s:.0f} days), mostly red {len(r)} (about {len(r) / s:.0f} days)")
    print("last columns:", "; ".join(f"{c['from'][5:10]} green {c['green']:.2f} level {c['g_mean'] or c['mean']}" for c in cols[-7:]))


if __name__ == "__main__":
    main()
