#!/usr/bin/env python3
"""CoinKarma's Karma Index (their tweet of 2026-11-26: "the cyclical indicator shows a buy signal again; such signals have
appeared several times near BTC's medium/long-term relative bottoms"), tested on the only history there is: the
index read off their own chart, 2022-01 .. 2026-11 (research/karma_index_digitize.py ->
research/ck_x/karma_index_2026-11-26.json; one value per 2.47 days, dates +-3 days). Below 20 is their buy zone,
above 80 their sell zone. BTC: Bitfinex hourly closes, as everywhere in this repo.

1. Episodes. A run of columns with some green (>= 15 % of the column), a new one after 10 days outside the zone.
   The first reading below 20 fell somewhere in the first such column or the next (about 5 days): every figure is
   given for each day of that window and reported as a range. Seven buy episodes (their seven yellow boxes), two
   sell episodes.
2. Days in the zone against other days, and against plain "the price is 30 % / 40 % below its one-year high" days:
   does the index say more than "the price has fallen a lot"? (Overlapping days from three market phases: a
   description, not independent cases.)
3. Can we rebuild it? The index against our own series (price position, momentum, basis, funding, DVOL, CFTC,
   stablecoins, Bitfinex), one at a time, as a ridge fit judged leave-one-year-out, and as buy-zone rules; and
   against CoinKarma's own Pulse Index (their Telegram table, 2025-02 on) smoothed over 7..60 days.

What comes out (2026-11-26):
- buy zone: after the first reading the price went lower by 11 % or more in 5 of 7 episodes even counted from the
  lowest close of the entry window (18, 15, 2, 0, 11, 24, 13 %; up to 27 % from the highest), reaching that low
  6 days to 9.5 months later; a year later the four 2022-23 entries were 30-130 % higher; the 2026 ones are not a
  year old (2026-02: 180 days later -16..+2 %, today -20..-4 %; 2026-05: today -19..-17 %);
- the zone is a state, not an event: 2026-02..08 had about 141 days below 20; the present run began about
  2026-11-13 (BTC 64-74K), eight days before the low so far (56.1K), and the level has crept up since (18 -> 19);
  the lowest readings on the chart are 2026-03/04 (a column mean of 12.5), below anything in 2022 (17-19);
- it does say more than a deep drawdown: of the days with the price 30 % or more below its one-year high, the ones
  below 20 were higher 90 days later 63 % of the time (median +17 %), the others 38 % (-10 %);
- sell zone: 2024-03 and 2024-11/12 only; a year later BTC was +28..+52 % and -8..+4 %; the 2025-10 high came
  with the index near 50;
- rebuilding: the level follows price position and basis (r 0.7-0.8; a 3-series fit r 0.88 out of year), but
  the buy zone itself is not reproduced (the fit's lowest readings and the best single rules overlap it about
  50-60 %); CoinKarma's Pulse Index averaged over 30-45 days tracks it at r 0.84-0.87 since 2025-02, its low
  stretches covering about 80 % of the zone with entries days to weeks off and one false week (2025-11).
  So: not something we can compute; the nearest thing we already read is their Pulse Index held low for weeks.
Output: research/karma_index_backtest.json and a printout."""
import csv
import datetime as dt
import json
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "ck", "raw")
SRC = os.path.join(ROOT, "research", "ck_x", "karma_index_2026-11-26.json")
UTC = dt.timezone.utc
D0 = dt.date(2021, 1, 1)  # the hourly closes start here
SOME, MOSTLY = 0.15, 0.5  # a column's green (or red) share: "the zone was touched" / "the column counts as in the zone"
GAP_D = 10  # days outside the zone that end an episode
HORIZONS = (30, 90, 180, 365)


def di(d):
    return ((dt.date.fromisoformat(d) if isinstance(d, str) else d) - D0).days


def ds(i):
    return (D0 + dt.timedelta(days=int(i))).isoformat()


def load_price(nd):
    close, low = np.full(nd, np.nan), np.full(nd, np.inf)
    for t, c in json.load(open(os.path.join(RAW, "bitfinex_1h.json")))["rows"]:
        if not c:
            continue
        i = (dt.datetime.fromtimestamp(t, UTC).date() - D0).days
        if 0 <= i < nd:
            close[i] = float(c)  # ends as the day's last hourly close
            low[i] = min(low[i], float(c))
    for i in range(1, nd):
        if not np.isfinite(close[i]):
            close[i] = close[i - 1]
        if not np.isfinite(low[i]):
            low[i] = close[i]
    return close, low


def daily(nd, name, field):
    j = json.load(open(os.path.join(RAW, name + ".json")))
    k = j["fields"].index(field)
    v = np.full(nd, np.nan)
    for r in j["rows"]:
        if r[k] is not None and 0 <= di(r[0]) < nd:
            v[di(r[0])] = float(r[k])
    for i in range(1, nd):
        if not np.isfinite(v[i]):
            v[i] = v[i - 1]
    return v


def roll(v, w, fn):
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        seg = v[max(0, i - w + 1):i + 1]
        seg = seg[np.isfinite(seg)]
        if len(seg) >= max(1, w // 2):
            out[i] = fn(seg)
    return out


def ema(v, span):
    out, a, e = np.full(len(v), np.nan), 2.0 / (span + 1), None
    for i in range(len(v)):
        if np.isfinite(v[i]):
            e = v[i] if e is None else a * v[i] + (1 - a) * e
        if e is not None:
            out[i] = e
    return out


def rsi(v, n):
    out, au, ad = np.full(len(v), np.nan), None, None
    for i in range(1, len(v)):
        d = v[i] - v[i - 1]
        if not np.isfinite(d):
            continue
        u, w = max(d, 0.0), max(-d, 0.0)
        au, ad = (u, w) if au is None else ((au * (n - 1) + u) / n, (ad * (n - 1) + w) / n)
        out[i] = 100 * au / (au + ad) if au + ad > 0 else 50.0
    return out


def rank(v, w):
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        seg = v[max(0, i - w + 1):i + 1]
        seg = seg[np.isfinite(seg)]
        if len(seg) >= w // 2 and np.isfinite(v[i]):
            out[i] = (seg <= v[i]).mean()
    return out


def corr(a, b):
    ok = np.isfinite(a) & np.isfinite(b)
    return float(np.corrcoef(a[ok], b[ok])[0, 1]) if ok.sum() >= 50 else float("nan")


def main():
    src = json.load(open(SRC))
    cols = src["cols"]
    last = dt.date.fromisoformat(src["fit"]["end"][:10]) - dt.timedelta(days=1)
    nd = di(last) + 1
    close, low = load_price(nd)
    par = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731

    def days_of(c):  # the days whose bar (centred at 12:00 UTC) lies in the column
        a, b = par(c["from"]), par(c["to"])
        return [i for i in range(di(a.date()) - 1, di(b.date()) + 2)
                if 0 <= i < nd and a <= dt.datetime.combine(D0 + dt.timedelta(days=i), dt.time(12), UTC) < b]

    width = 1.0 / src["fit"]["px_per_day"]
    K, G, R = np.full(nd, np.nan), np.full(nd, np.nan), np.full(nd, np.nan)
    for c in cols:
        if c["mean"] is None:
            continue
        lvl = c["g_mean"] if (c["green"] >= 0.9 and c["g_mean"]) else c["mean"]
        for i in days_of(c):
            K[i], G[i], R[i] = lvl, c["green"], c["red"]
    have = np.isfinite(G)
    green, red = have & (G >= MOSTLY), have & (R >= MOSTLY)

    # ---------------------------------------------------------------- 1. episodes
    def episodes(key):
        runs = []
        for k, c in enumerate(cols):
            if c[key] >= SOME:
                if runs and (c["x"] - cols[runs[-1][-1]]["x"]) * width <= GAP_D:
                    runs[-1].append(k)
                else:
                    runs.append([k])
        out = []
        for r in runs:
            win = sorted(set(days_of(cols[r[0]]) + (days_of(cols[r[0] + 1]) if r[0] + 1 < len(cols) else [])))
            if not win:
                continue
            px = [float(close[i]) for i in win]
            e = {"first_reading": [ds(win[0]), ds(win[-1])], "px": [round(min(px)), round(float(np.median(px))), round(max(px))],
                 "last_reading": cols[r[-1]]["to"][:10], "days_in_zone": round(sum(cols[k][key] for k in r) * width),
                 "span_days": round((par(cols[r[-1]]["to"]) - par(cols[r[0]]["from"])).total_seconds() / 86400)}
            lv = [cols[k]["g_mean"] for k in r if cols[k].get("g_mean") and cols[k]["green"] >= 0.9]
            if key == "green" and lv:
                e["level_mean_lowest_column"] = round(min(lv), 1)
            for h in HORIZONS:
                v = [float(close[i + h] / close[i] - 1) for i in win if i + h < nd]
                e[f"r{h}"] = [round(min(v), 4), round(float(np.median(v)), 4), round(max(v), 4)] if len(v) == len(win) else None
            j1 = min(win[-1] + 365, nd - 1)
            seg_lo, seg_hi = low[win[-1] + 1:j1 + 1], close[win[-1] + 1:j1 + 1]
            if len(seg_lo):
                k = int(np.argmin(seg_lo))
                e["low_after"] = {"px": round(float(seg_lo[k])), "date": ds(win[-1] + 1 + k), "days": k + 1,
                                  "vs_entry": [round(float(seg_lo[k] / max(px) - 1), 4), round(float(seg_lo[k] / min(px) - 1), 4)]}
                k = int(np.argmax(seg_hi))
                e["high_after"] = {"px": round(float(seg_hi[k])), "date": ds(win[-1] + 1 + k), "days": k + 1,
                                   "vs_entry": [round(float(seg_hi[k] / max(px) - 1), 4), round(float(seg_hi[k] / min(px) - 1), 4)]}
            e["now_vs_entry"] = [round(float(close[-1] / max(px) - 1), 4), round(float(close[-1] / min(px) - 1), 4)]
            out.append(e)
        return out

    buy, sell = episodes("green"), episodes("red")
    pc = lambda v: "—" if v is None else f"{v * 100:+.0f}%"  # noqa: E731
    rng = lambda v: "—" if v is None else f"{pc(v[0])}..{pc(v[-1])}"  # noqa: E731
    for name, eps in (("buy zone (< 20)", buy), ("sell zone (> 80)", sell)):
        print(f"\n== {name}: {len(eps)} episodes (first reading within the window; ranges over its days)")
        for e in eps:
            la = e.get("low_after")
            print(f"  {e['first_reading'][0]}..{e['first_reading'][1][5:]} BTC {e['px'][0]:,}-{e['px'][2]:,} | in zone {e['days_in_zone']:3d} of {e['span_days']:3d} d (to {e['last_reading']})"
                  + (f", lowest column {e['level_mean_lowest_column']}" if "level_mean_lowest_column" in e else "")
                  + " | " + " ".join(f"{h}d {rng(e[f'r{h}'])}" for h in HORIZONS)
                  + (f" | low after: {la['px']:,} on {la['date']} (+{la['days']} d) {rng(la['vs_entry'])}" if la else "") + f" | now {rng(e['now_vs_entry'])}")

    # ---------------------------------------------------------------- 2. zone days against other days
    mx365 = np.array([np.nanmax(close[max(0, i - 364):i + 1]) for i in range(nd)])
    dd = close / mx365 - 1

    def stats(mask):
        out = {"days": int(mask.sum())}
        for h in HORIZONS:
            idx = [i for i in np.nonzero(mask)[0] if i + h < nd]
            v = np.array([close[i + h] / close[i] - 1 for i in idx])
            out[str(h)] = {"n": len(v), "up": round(float((v > 0).mean()), 3), "median": round(float(np.median(v)), 4)} if len(v) else None
        return out

    y26 = np.arange(nd) >= di("2026-01-01")
    by_day = {"all": stats(have), "below20": stats(green), "not_below20": stats(have & ~green), "above80": stats(red),
              "dd30": stats(have & (dd <= -0.30)), "dd30_below20": stats(green & (dd <= -0.30)), "dd30_not": stats(have & ~green & (dd <= -0.30)),
              "dd40": stats(have & (dd <= -0.40)), "dd40_below20": stats(green & (dd <= -0.40)), "dd40_not": stats(have & ~green & (dd <= -0.40)),
              "below20_2022_23": stats(green & ~y26), "below20_2026": stats(green & y26)}
    print("\n== BTC after days in / out of the zone (overlapping days; a description)")
    for k, s in by_day.items():
        print(f"  {k:16s} days {s['days']:4d} | " + " | ".join(f"{h}d " + (f"{s[str(h)]['up'] * 100:3.0f}% up {s[str(h)]['median'] * 100:+6.1f}% (n {s[str(h)]['n']})" if s[str(h)] else "—") for h in HORIZONS))

    # ---------------------------------------------------------------- 3. can we rebuild it?
    lp = np.log(close)
    chg = lambda v, w: np.concatenate([np.full(w, np.nan), v[w:] - v[:-w]])  # noqa: E731
    pct = lambda v, w: np.concatenate([np.full(w, np.nan), v[w:] / v[:-w] - 1])  # noqa: E731
    sma = lambda v, w: roll(v, w, np.mean)  # noqa: E731
    F = {}
    for w in (90, 180, 365):
        hi, lo_ = np.log(roll(close, w, np.max)), np.log(roll(close, w, np.min))
        F[f"pos{w}"] = (lp - lo_) / (hi - lo_)
    F["dd365"] = dd
    F["mayer200"], F["px_sma100"], F["px_sma50"] = close / sma(close, 200), close / sma(close, 100), close / sma(close, 50)
    for w in (30, 60, 90, 180, 365):
        F[f"roc{w}"] = chg(lp, w)
    F["rsi14"], F["rsi30"], F["rsi90"] = rsi(lp, 14), rsi(lp, 30), rsi(lp, 90)
    F["rank_px365"], F["rank_px730"] = rank(close, 365), rank(close, 730)
    fund = daily(nd, "funding", "binance_pct8h")
    F["fund7"], F["fund30"], F["fund90"] = sma(fund, 7), sma(fund, 30), sma(fund, 90)
    bas = daily(nd, "basis", "apy_pct")
    F["basis7"], F["basis30"] = sma(bas, 7), sma(bas, 30)
    F["dvol"] = daily(nd, "deribit_dvol", "dvol")
    oi = daily(nd, "cftc", "oi")
    F["cftc_lev_net"] = (daily(nd, "cftc", "lev_long") - daily(nd, "cftc", "lev_short")) / oi
    F["cftc_am_net"] = (daily(nd, "cftc", "am_long") - daily(nd, "cftc", "am_short")) / oi
    st = daily(nd, "stablecoins", "usdt_usd") + np.nan_to_num(daily(nd, "stablecoins", "usdc_usd"))
    F["stable30"], F["stable90"] = pct(st, 30), pct(st, 90)
    F["usdc_usdt"] = sma(daily(nd, "usdc_usdt", "mean"), 7)
    bl = daily(nd, "bitfinex", "btcusd_long")
    F["bfx_long30"], F["bfx_long90"] = pct(bl, 30), pct(bl, 90)
    F["bfx_fusd_rate30"] = sma(daily(nd, "bitfinex", "fusd_rate"), 30)
    # one row per column (the index's own resolution)
    use = [c for c in cols if c["mean"] is not None and days_of(c)]
    idx = [days_of(c) for c in use]
    kc = np.array([c["g_mean"] if (c["green"] >= 0.9 and c["g_mean"]) else c["mean"] for c in use])
    gc = np.array([c["green"] >= MOSTLY for c in use])
    yr = np.array([max(2022, (D0 + dt.timedelta(days=i[0])).year) for i in idx])
    X = {k: np.array([np.nanmean(v[i]) if np.isfinite(v[i]).any() else np.nan for i in idx]) for k, v in F.items()}
    single = sorted(((round(corr(X[k], kc), 3), k) for k in X), key=lambda t: -abs(t[0]))
    print("\n== the index against our own series, one at a time (r):", ", ".join(f"{k} {r:+.2f}" for r, k in single[:12]))
    feats = [k for k in X if np.isfinite(X[k]).sum() >= len(kc) - 12]
    ok = np.all([np.isfinite(X[k]) for k in feats], axis=0)

    def loyo(keys):  # ridge on standardized series, each year predicted from the other years
        pred = np.full(len(kc), np.nan)
        for y in sorted(set(yr)):
            tr, te = ok & (yr != y), ok & (yr == y)
            A = np.column_stack([X[k][tr] for k in keys])
            mu, sd = A.mean(0), A.std(0) + 1e-12
            A = np.column_stack([np.ones(tr.sum()), (A - mu) / sd])
            coef = np.linalg.solve(A.T @ A + np.diag([0.0] + [1.0] * len(keys)), A.T @ kc[tr])
            pred[te] = np.column_stack([np.ones(te.sum()), (np.column_stack([X[k][te] for k in keys]) - mu) / sd]) @ coef
        return float(np.corrcoef(pred[ok], kc[ok])[0, 1]), float(np.sqrt(np.mean((pred[ok] - kc[ok]) ** 2))), pred

    chosen, steps = [], []
    for _ in range(4):
        rmse, r, k = min((loyo(chosen + [k])[1], loyo(chosen + [k])[0], k) for k in feats if k not in chosen)
        chosen.append(k)
        _, _, pred = loyo(chosen)
        z = ok & (pred <= np.nanquantile(pred[ok], gc[ok].mean()))  # the fit's lowest readings, as often as the zone
        steps.append({"series": list(chosen), "r_out_of_year": round(r, 3), "rmse": round(rmse, 2),
                      "zone_overlap": round(float((z & gc).sum() / max(1, z.sum())), 3),
                      "r_by_year": {int(y): round(float(np.corrcoef(pred[ok & (yr == y)], kc[ok & (yr == y)])[0, 1]), 2) for y in sorted(set(yr))}})
    print("== a fit judged leave-one-year-out:")
    for s in steps:
        print(f"  {' + '.join(s['series'])}: r {s['r_out_of_year']:.2f}, rmse {s['rmse']}; its lowest readings overlap the buy zone {s['zone_overlap'] * 100:.0f}%; by year {s['r_by_year']}")
    rules = []
    for k, v in X.items():
        okk = np.isfinite(v)
        for sign in (1, -1):
            thr = np.nanquantile(sign * v[okk], gc[okk].mean())
            z = okk & (sign * v <= thr)
            rules.append((round(float((z & gc).sum() / max(1, z.sum())), 3), k, "<=" if sign == 1 else ">=", round(float(sign * thr), 4)))
    rules.sort(reverse=True)
    print("== single rules that are on as often as the buy zone, share of their time inside it:", ", ".join(f"{k} {op} {t:g}: {p * 100:.0f}%" for p, k, op, t in rules[:6]))

    # their own Pulse Index (the Telegram table, three a day from 2025-02-05), smoothed
    p0 = di("2025-02-05")
    acc = [[] for _ in range(nd)]
    for r in csv.DictReader(open(os.path.join(ROOT, "data", "ck", "pulse", "pulse.csv"))):
        try:
            i, v = di(r["update_at"][:10]), float(r["pulse_index"])
        except ValueError:
            continue
        if p0 <= i < nd and np.isfinite(v):
            acc[i].append(v)
    pulse = np.array([np.mean(a) if a else np.nan for a in acc])
    for i in range(p0 + 1, nd):
        if not np.isfinite(pulse[i]):
            pulse[i] = pulse[i - 1]
    win = have & (np.arange(nd) >= p0)

    def spans(z):
        out, s = [], None
        for i in range(nd):
            if z[i] and s is None:
                s = i
            if s is not None and (not z[i] or i == nd - 1):
                e = i if z[i] else i - 1
                if out and s - out[-1][1] <= GAP_D:
                    out[-1][1] = e
                else:
                    out.append([s, e])
                s = None
        return [f"{ds(a)}..{ds(b)}" for a, b in out]

    slow = []
    for name, v in [(f"mean{w}", sma(pulse, w)) for w in (7, 14, 30, 45, 60)] + [(f"ema{w}", ema(pulse, w)) for w in (30, 45)]:
        m = win & np.isfinite(v)
        best = max(((2 * p * q / max(1e-9, p + q), float(t), p, q) for t in np.arange(15, 40.1, 0.5)
                    for p, q in [(((v <= t) & green & m).sum() / max(1, ((v <= t) & m).sum()), ((v <= t) & green & m).sum() / max(1, (green & m).sum()))]))
        slow.append({"smoothing": name, "r": round(corr(np.where(m, v, np.nan), K), 3), "best_level": best[1], "precision": round(float(best[2]), 3), "recall": round(float(best[3]), 3),
                     "now": round(float(v[-1]), 1), "spans": spans((v <= best[1]) & m)})
    print(f"== CoinKarma's own Pulse Index, smoothed, against the Karma Index ({int(win.sum())} days from 2025-02-05; Karma buy zone: {'; '.join(spans(green & win))})")
    for s in slow:
        print(f"  {s['smoothing']:7s} r {s['r']:+.2f} | at or below {s['best_level']:.1f}: {s['precision'] * 100:.0f}% of that time in the zone, {s['recall'] * 100:.0f}% of the zone covered | now {s['now']} | {'; '.join(s['spans'])}")

    out = {"source": os.path.relpath(SRC, ROOT), "asof": ds(nd - 1), "btc": round(float(close[-1])), "zones": {"buy_below": 20, "sell_above": 80},
           "level_now": cols[-1]["g_mean"] or cols[-1]["mean"], "buy": buy, "sell": sell, "by_day": by_day,
           "rebuild": {"single_r": [{"series": k, "r": r} for r, k in single], "fit_leave_one_year_out": steps,
                       "single_rules": [{"series": k, "op": op, "level": t, "share_in_zone": p} for p, k, op, t in rules[:12]], "pulse_smoothed": slow}}
    with open(os.path.join(ROOT, "research", "karma_index_backtest.json"), "w") as f:
        json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
