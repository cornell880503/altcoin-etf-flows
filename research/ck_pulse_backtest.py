#!/usr/bin/env python3
"""CoinKarma's own indicator values (research/ck_pulse/pulse.csv, read from their Telegram tables three times a day)
against what BTC and the altcoins did next.

1. Checks of the reading: BTC's printed price against Binance's close at the update time; each status against its
   value and CoinKarma's thresholds; Overall LIQ against the hourly chart we digitized (data/ck/ck_charts.json).
2. Signals: for every indicator, the first table of each Buy (or Sell / Caution) spell (none in the 24 hours before),
   and the arrows (Overall LIQ >= 2 and BTC LIQ >= 3; <= -2 and <= -3): BTC's return over the next 24 and 72 hours
   and 7 days, and the altcoin basket's (23 coins, market-cap weighted) over 72 hours, against all tables, with a
   permutation p (random tables, the same number).
3. Lead or lag: each value against BTC's return over the 24 hours before and after (Spearman, one table a day).
4. How close our own LIQ (Binance perpetual proxy) comes to theirs at the same hours.
Output: research/ck_pulse/backtest.json and a printout.
"""
import csv
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ck_liq as L  # noqa: E402

PULSE = os.path.join(ROOT, "research", "ck_pulse", "pulse.csv")
OUT = os.path.join(ROOT, "research", "ck_pulse", "backtest.json")
# key: (label, buy test, sell test) with CoinKarma's thresholds as printed (Bottom Ref = Buy, Top Ref = Sell/Caution)
IND = {
    "overall_liq": ("Overall LIQ", lambda v: v >= 2, lambda v: v <= -2),
    "btc_liq": ("BTC LIQ", lambda v: v >= 3, lambda v: v <= -3),
    "eth_liq": ("ETH LIQ", lambda v: v >= 3, lambda v: v <= -3),
    "acc_btc_liq": ("Accumulated BTC LIQ", lambda v: v >= 60, lambda v: v <= -60),
    "overall_liquidity_btc": ("Overall Liquidity in BTC", lambda v: v >= 3000, lambda v: v <= -3000),
    "lsur_z": ("LSUR Z-Index", lambda v: v >= 2, lambda v: v <= -2),
    "ari": ("ALT Resilient Index", lambda v: v >= 2, lambda v: v <= -0.3),
    "bitfinex_premium": ("Bitfinex Premium", lambda v: v >= 0.25, lambda v: v <= -0.15),
    "usdc_usdt_premium": ("USDC/USDT Premium", lambda v: v >= 0.05, lambda v: v <= -0.05),
    "binance_usdt_rate": ("Binance USDT Interest Rate", lambda v: v <= 10, lambda v: v >= 20),
    "oi_funding_apy": ("OI Weighted Funding APY", lambda v: v <= 5, lambda v: v >= 20),
    "pulse_index": ("Pulse Index", lambda v: v <= 20, lambda v: v >= 80),
}
HORIZONS = (24, 72, 168)
COOLDOWN = 24
rng = np.random.default_rng(7)


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


CAUTION = {  # CoinKarma's warning band before Sell, as their statuses show it (Overall LIQ -2..-1.5: Caution)
    "overall_liq": lambda v: -2 < v <= -1.5, "btc_liq": lambda v: -3 < v <= -2, "eth_liq": lambda v: -3 < v <= -2,
    "acc_btc_liq": lambda v: -60 < v <= -45, "overall_liquidity_btc": lambda v: -3000 < v <= -2000, "lsur_z": lambda v: -2 < v <= -1.5,
    "ari": lambda v: -0.3 < v <= -0.2, "pulse_index": lambda v: 70 <= v < 80}


def expected(k, v):
    _, buy, sell = IND[k]
    if buy(v):
        return "Buy"
    if sell(v):
        return "Sell"
    return "Caution" if k in CAUTION and CAUTION[k](v) else "Hold"


def clean(k, v, st):
    """The value, checked against the status word (read far more reliably): a lost minus sign is put back; a value its
    status cannot have (a digit misread) is dropped. Returns (value or None, how)."""
    if v is None or not st:
        return v, "unchecked"
    near = lambda a, b: a == b or {a, b} <= {"Hold", "Caution"} or {a, b} == {"Buy", "Hold"} or {a, b} == {"Caution", "Sell"}  # noqa: E731
    if near(expected(k, v), st):
        return v, "ok"
    if near(expected(k, -v), st):
        return -v, "sign"
    return None, "dropped"


def load():
    rows = {}
    for r in csv.DictReader(open(PULSE)):
        u = r.get("update_at") or ""
        if len(u) < 16 or not u[11:13].isdigit() or int(u[11:13]) > 23:
            p = r.get("posted_at") or ""
            if len(p) < 13:
                continue
            u = p[:13] + ":00"
        h = L.hidx(u[:13])
        rows[h] = r  # the latest post for an update hour wins
    return dict(sorted(rows.items()))


def spearman(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10:
        return None, int(m.sum())
    rx, ry = np.argsort(np.argsort(x[m])), np.argsort(np.argsort(y[m]))
    return round(float(np.corrcoef(rx, ry)[0, 1]), 3), int(m.sum())


def main():
    D = L.Data()
    R = L.compute(D, **L.PARAMS)
    rows = load()
    hs = np.array(list(rows))
    n = D.n
    btc = D.close["BTC"]
    # the altcoin basket: market-cap weighted hourly log returns of the 23 others, cumulated
    lr, ws = np.zeros(n), np.zeros(n)
    for c in L.COINS:
        if c == "BTC":
            continue
        cl, w = D.close[c], D.mcap[c]
        r = np.full(n, np.nan)
        r[1:] = np.log(cl[1:] / cl[:-1])
        ok = np.isfinite(r) & np.isfinite(w)
        lr += np.where(ok, w * np.nan_to_num(r), 0.0)
        ws += np.where(ok, w, 0.0)
    alt = np.exp(np.cumsum(np.where(ws > 0, lr / np.where(ws > 0, ws, 1), 0.0)))
    # a table updated at hour U describes the market up to U: enter at the close of the candle U-1 (the price at U)
    idx = hs - 1 - D.h0

    def ret(px, H, back=False):
        out = np.full(len(idx), np.nan)
        for k, i in enumerate(idx):
            j = i - H if back else i + H
            if 0 <= min(i, j) and max(i, j) < n and np.isfinite(px[i]) and np.isfinite(px[j]) and px[i] > 0:
                out[k] = px[j] / px[i] - 1 if not back else px[i] / px[j] - 1
        return out

    fwd = {H: ret(btc, H) for H in HORIZONS}
    fwd_alt = ret(alt, 72)
    past24 = ret(btc, 24, back=True)
    stat = {k: [rows[h].get(k + "_st") or "" for h in hs] for k in IND}
    vals, fixes = {}, {}
    for k in IND:
        out_v, cnt = [], {}
        for h, st in zip(hs, stat[k]):
            v, how = clean(k, f(rows[h].get(k)), st)
            cnt[how] = cnt.get(how, 0) + 1
            out_v.append(np.nan if v is None else v)
        vals[k], fixes[k] = np.array(out_v), cnt
    out = {"tables": len(hs), "from": L.hstr(int(hs[0])) if len(hs) else None, "to": L.hstr(int(hs[-1])) if len(hs) else None}

    # ---------------------------------------------------------------- 1. checks of the reading
    chk = {}
    pb = np.array([f(rows[h].get("btc")) or np.nan for h in hs])
    ref = np.array([btc[i] if 0 <= i < n else np.nan for i in idx])
    dev = np.abs(pb / ref - 1)
    ok = np.isfinite(dev)
    chk["btc_price"] = {"compared": int(ok.sum()), "within_1pct": round(float(np.mean(dev[ok] < 0.01)), 4) if ok.any() else None,
                        "median_dev_pct": round(float(np.median(dev[ok]) * 100), 3) if ok.any() else None,
                        "worst": [[L.hstr(int(hs[k])), rows[hs[k]].get("btc"), round(float(ref[k]), 0)] for k in np.argsort(-np.nan_to_num(dev))[:5]]}
    cons = {}
    for k, (lab, buy, sell) in IND.items():
        agree = disagree = 0
        bad = []
        for v, st, h in zip(vals[k], stat[k], hs):
            if not np.isfinite(v) or not st:
                continue
            want, got = expected(k, v), st
            if got == want:
                agree += 1
            else:
                disagree += 1
                if len(bad) < 3:
                    bad.append([L.hstr(int(h)), v, st])
        cons[k] = {"agree": agree, "disagree": disagree, "examples": bad}
    chk["status_vs_value"] = cons
    chk["cleaning"] = fixes
    try:
        ckc = json.load(open(os.path.join(ROOT, "data", "ck", "ck_charts.json")))["charts"][0]
        xs, ys = [], []
        for h, v in zip(hs, vals["overall_liq"]):
            c = ckc["overall"].get(L.hstr(int(h) - 1))  # the chart is stored per our candle hour: the candle U-1 closes at U
            if c is not None and np.isfinite(v):
                xs.append(v)
                ys.append(c)
        chk["vs_digitized_chart"] = {"n": len(xs), "corr": round(float(np.corrcoef(xs, ys)[0, 1]), 3) if len(xs) > 5 else None,
                                     "mean_abs_diff": round(float(np.mean(np.abs(np.array(xs) - np.array(ys)))), 3) if xs else None}
    except (OSError, KeyError, ValueError):
        pass
    out["checks"] = chk

    # ---------------------------------------------------------------- 2. signals
    def first_of_spells(flag):
        ev, last, prev = [], -10 ** 9, False
        for k, (h, fl) in enumerate(zip(hs, flag)):
            if fl and not prev and h - last >= COOLDOWN:
                ev.append(k)
                last = h
            prev = fl
        return ev

    def evstats(ev, label):
        res = {"label": label, "n": len(ev)}
        for H, arr, name in [(H, fwd[H], f"btc_{H}h") for H in HORIZONS] + [(72, fwd_alt, "alts_72h")]:
            e = [k for k in ev if np.isfinite(arr[k])]
            pool = np.flatnonzero(np.isfinite(arr))
            if len(e) < 3:
                res[name] = {"n": len(e)}
                continue
            m, up = float(np.mean(arr[e])), float(np.mean(arr[e] > 0))
            sims = np.array([np.mean(arr[rng.choice(pool, len(e), replace=False)]) for _ in range(4000)])
            ups = np.array([np.mean(arr[rng.choice(pool, len(e), replace=False)] > 0) for _ in range(2000)])
            p_m = float(np.mean(sims >= m) if m >= sims.mean() else np.mean(sims <= m))
            p_u = float(np.mean(ups >= up) if up >= ups.mean() else np.mean(ups <= up))
            res[name] = {"n": len(e), "mean_pct": round(m * 100, 2), "median_pct": round(float(np.median(arr[e])) * 100, 2),
                         "up": round(up, 3), "all_mean_pct": round(float(np.mean(arr[pool])) * 100, 2), "all_up": round(float(np.mean(arr[pool] > 0)), 3),
                         "p_mean": round(p_m, 3), "p_up": round(p_u, 3)}
        return res

    sig = []
    for k, (lab, buy, sell) in IND.items():
        st = np.array(stat[k])
        for name in ("Buy", "Caution", "Sell"):
            if (st == name).sum():
                sig.append(evstats(first_of_spells(st == name), f"{lab}: {name}"))
    so, sb = np.array(stat["overall_liq"]), np.array(stat["btc_liq"])
    sig.append(evstats(first_of_spells((so == "Buy") & (sb == "Buy")), "Green arrow (Overall LIQ and BTC LIQ both Buy)"))
    sig.append(evstats(first_of_spells((so == "Sell") & (sb == "Sell")), "Red arrow (Overall LIQ and BTC LIQ both Sell)"))
    out["signals"] = sig

    # ---------------------------------------------------------------- 3. lead or lag (one table a day, the 00 UTC one)
    day = np.array([L.hstr(int(h))[11:13] == "00" for h in hs])
    ll = {}
    for k in IND:
        x = vals[k][day]
        ll[k] = {"next_24h": spearman(x, fwd[24][day]), "next_72h": spearman(x, fwd[72][day]), "past_24h": spearman(x, past24[day]),
                 "alts_next_72h": spearman(x, fwd_alt[day])}
    out["lead_lag"] = ll

    # ---------------------------------------------------------------- 4. our proxy at the same hours
    prox = {}
    for k, arr in (("overall_liq", R["overall"]), ("btc_liq", R["liq"]["BTC"]), ("eth_liq", R["liq"]["ETH"])):
        ours = np.array([arr[i] if 0 <= i < n else np.nan for i in idx])
        m = np.isfinite(ours) & np.isfinite(vals[k])
        prox[k] = {"n": int(m.sum()), "corr": round(float(np.corrcoef(ours[m], vals[k][m])[0, 1]), 3) if m.sum() > 10 else None}
        q = {}
        for qn in sorted({L.hstr(int(h))[:4] + "Q" + str((int(L.hstr(int(h))[5:7]) - 1) // 3 + 1) for h in hs}):
            mm = m & np.array([(L.hstr(int(h))[:4] + "Q" + str((int(L.hstr(int(h))[5:7]) - 1) // 3 + 1)) == qn for h in hs])
            if mm.sum() > 30:
                q[qn] = round(float(np.corrcoef(ours[mm], vals[k][mm])[0, 1]), 2)
        prox[k]["by_quarter"] = q
    out["our_proxy_vs_theirs"] = prox
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1, default=lambda x: None if isinstance(x, float) and math.isnan(x) else x)

    # ---------------------------------------------------------------- printout
    print(f"{out['tables']} tables, {out['from']} .. {out['to']}")
    print("checks:", json.dumps({k: v for k, v in chk.items() if k != "status_vs_value"}, ensure_ascii=False))
    print("status vs value after cleaning (agree/disagree):", {k: f"{v['agree']}/{v['disagree']}" for k, v in cons.items()})
    print("cleaning:", fixes)
    print(f"\n{'signal':52s} {'n':>4}  {'BTC 24h':>16} {'BTC 72h':>16} {'BTC 7d':>16} {'alts 72h':>16}")
    for s in sig:
        cells = []
        for name in ("btc_24h", "btc_72h", "btc_168h", "alts_72h"):
            x = s.get(name, {})
            cells.append(f"{x['mean_pct']:+5.2f}% p{x['p_mean']:.2f}" if "mean_pct" in x else "      —       ")
        print(f"{s['label']:52s} {s['n']:4d}  " + "  ".join(f"{c:>16}" for c in cells))
    print("\nbaseline (all tables):", {name: (sig[0].get(name, {}).get("all_mean_pct"), sig[0].get(name, {}).get("all_up"))
                                       for name in ("btc_24h", "btc_72h", "btc_168h", "alts_72h")})
    print("\nlead/lag (Spearman, one table a day):")
    for k, v in ll.items():
        print(f"  {IND[k][0]:28s} past 24h {v['past_24h'][0]}  next 24h {v['next_24h'][0]}  next 72h {v['next_72h'][0]}  alts next 72h {v['alts_next_72h'][0]}  (n={v['next_24h'][1]})")
    print("\nour proxy vs theirs:", prox)


if __name__ == "__main__":
    main()
