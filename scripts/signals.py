#!/usr/bin/env python3
"""Can ETF flows be a trading signal? Every signal here is checked the way a trader would use it.

Timing. The flow of US trade date t is public the next trading day, so a decision taken at the
daily close of d = t + lag (lag 1; 2 for the funds that publish a day late) may use flows up to
t and prices up to d, and earns the return from that close over the next h trading days
(h = 1, 5, 10, 20). Prices are the coin's own daily close, 00:00 UTC (08:00 Singapore). Flows
are in basis points of the coin's market cap. Nothing is used before it was public.

Signals (each a yes/no state at the decision close; the "on" days are compared with the rest):
  CUMTOP x     the last x days of flows add up to the coin's top 20 % so far (x = 1, 5, 10, 20,
               60; expanding percentile, so the threshold only uses the past)
  CUMBOT x     the bottom 20 % (the weakest flows: outflows for BTC and ETH, often still small
               inflows for altcoins whose funds have mostly taken money in)
  FLIPUP y     the y-day net flow turns from outflow to inflow after at least 3 outflow days
  FLIPDN y     the reverse (y = 5, 10, 20)
  INFLOW y     the y-day net flow is positive (a state, not an event)
  STREAK n     n inflow days in a row (n = 3, 5)
  DIPBUY x     net inflow over x days while the price fell over the same x days (x = 5, 20)
  RALLYSELL x  net outflow while the price rose
Price-only twins (the benchmark a flow signal has to beat): PFLIPUP/PFLIPDN y (the y-day return
turns positive / negative), PTOP x (top 20 % x-day return), PUP y (y-day return positive).

Samples. BTC since 2024-01-11 and ETH since 2024-07-23 (data/daily_long.json; market cap =
previous close x Coin Metrics supply); the altcoins since their first ETF day (data/daily.json,
CoinGecko market caps), also pooled (each coin's own mean removed; Driscoll-Kraay standard
errors, which let every coin move together on a day). A coin joins the pool once it has 60
trading days of flows and at most 60 % of them are zero (most small altcoin ETFs create and
redeem in occasional lumps, which says little about day-to-day demand).

A fourth sample, BTC>ALTS, uses BTC's flow signals for the pooled altcoins' returns (does BTC
ETF demand lead the altcoins?).

Statistics. Mean forward return on minus off from an OLS with Newey-West standard errors (h lags,
never below the classical ones). Benjamini-Hochberg q values over every flow test of the four
main samples (BTC, ETH, pooled altcoins, BTC>ALTS) and all horizons. A rotation test: each sample's signals
are shifted against its returns by a random number of days (keeping both series' own
persistence, breaking only their timing), 200 times; it gives every test an assumption-light p
value and the chance that the best of all tests is that good by luck. BTC's two halves and the
altcoins' two halves as an out-of-sample check, and each flow signal re-tested next to its price
twin, to see whether the flow adds anything.

Usage: python3 scripts/signals.py [--out FILE] [--iters N]   (needs numpy)"""
import argparse
import bisect
import datetime as dt
import json
import math
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calendar_us import trading_days  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)  # noqa: E731
PX_CLOSE = 2
ALTS = ["SOL", "XRP", "HYPE", "ZEC", "LINK", "DOGE", "AVAX", "HBAR", "LTC", "SUI", "DOT", "NEAR", "BNB", "TRX"]
POOL_MAX_ZERO = 0.6   # a coin joins the altcoin pool once at most 60 % of its days have zero flow
POOL_MIN_DAYS = 60    # ... and it has 60 trading days of flows (many small ETFs trade in lumps)
CANARY_PART = {"SOL": "SOLC", "XRP": "XRPC", "SUI": "SUIS"}
CANARY_ONLY = {"LTC", "HBAR", "TRX"}
HORIZONS = [1, 5, 10, 20]
CUM_X = [1, 5, 10, 20, 60]
FLIP_Y = [5, 10, 20]
STREAK_N = [3, 5]
DIV_X = [5, 20]
MIN_PRIOR = {"long": 60, "alt": 40}  # history needed before a percentile threshold is trusted
MIN_ON = 8                          # fewer "on" days than this: no test
MAIN = ("BTC", "ETH", "ALTS", "BTC>ALTS")
POOLED = ("ALTS", "BTC>ALTS")
PRICE_TWIN = {"FLIPUP": "PFLIPUP", "FLIPDN": "PFLIPDN", "INFLOW": "PUP", "CUMTOP": "PTOP"}
NAN = float("nan")


# ------------------------------------------------------------------ data
def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def closes(sym):
    j = load_json(P("data", "coin_px", f"{sym}.json"))
    return {d: v[PX_CLOSE] for d, v in j["rows"].items()
            if isinstance(v, list) and len(v) > PX_CLOSE and isinstance(v[PX_CLOSE], (int, float))}


def supply_lookup(sym):
    """Circulating supply on or before a date (Coin Metrics, research snapshot)."""
    try:
        cm = load_json(P("research", "data", f"coinmetrics_{sym.lower()}.json"))
    except OSError:
        return None
    pts = sorted((k, float(v["SplyCur"])) for k, v in cm.items() if v.get("SplyCur"))
    keys = [k for k, _ in pts]

    def at(d):
        i = bisect.bisect_right(keys, d) - 1
        return pts[i][1] if i >= 0 else None
    return at


def next_days(last, k):
    """The k US trading days after `last`."""
    d0 = dt.date.fromisoformat(last)
    return trading_days((d0 + dt.timedelta(days=1)).isoformat(), (d0 + dt.timedelta(days=10)).isoformat())[:k]


def coin_long(sym, long):
    """BTC / ETH since launch: flows, daily closes, market cap = previous close x supply."""
    c = long["coins"][sym]
    px = closes(sym)
    sup = supply_lookup(sym)
    fl = dict(zip(c["dates"], c["flow"]))
    cal = trading_days(c["launch"], c["dates"][-1])
    days = [max(d for d in px if d < cal[0])] + cal + next_days(c["dates"][-1], 1)
    P_ = [px.get(d) for d in days]
    F = [None] + [fl.get(d) for d in cal] + [None]
    MC = [None] * len(days)
    for i in range(1, len(days)):
        s = sup(days[i - 1]) if sup else None
        if P_[i - 1] and s:
            MC[i] = P_[i - 1] * s / 1e6  # US$ millions
    return dict(sym=sym, days=days, P=P_, F=F, K=F[:], MC=MC, lag=1, launch=c["launch"], sample="long")


def coin_alt(sym, daily):
    c = daily["coins"][sym]
    if not c.get("dates"):
        return None
    px = closes(sym)
    cal = trading_days(c["dates"][0], c["dates"][-1])
    prior = [d for d in px if d < cal[0]]
    lag = 2 if sym in CANARY_ONLY else 1
    days = ([max(prior)] if prior else []) + cal
    off = len(days) - len(cal)
    nxt = next_days(c["dates"][-1], lag)  # the decision closes still to come for the newest flows
    fl = dict(zip(c["dates"], c["flow"]))
    mc = dict(zip(c["dates"], c.get("mcap") or []))
    funds = {}
    for tk, arr in (c.get("funds") or {}).items():
        for d, v in zip(c["dates"], arr):
            if v is not None:
                funds.setdefault(d, {})[tk] = v
    pend = set((c.get("pending") or {}).get("dates", []))
    F, K, MC = [None] * off, [None] * off, [None] * off
    for d in cal:
        f = fl.get(d)
        F.append(f)
        m = mc.get(d)
        MC.append(m if isinstance(m, (int, float)) and m > 0 else None)
        if f is None:
            K.append(None)
        elif sym in CANARY_PART:  # the Canary fund's part arrives a day later
            K.append(f - ((funds.get(d) or {}).get(CANARY_PART[sym]) or 0.0))
        else:
            K.append(None if d in pend else f)
    days += nxt
    F += [None] * len(nxt)
    K += [None] * len(nxt)
    MC += [None] * len(nxt)
    zero = sum(1 for v in c["flow"] if v is not None and abs(v) < 1e-9) / max(1, sum(1 for v in c["flow"] if v is not None))
    return dict(sym=sym, days=days, P=[px.get(d) for d in days], F=F, K=K, MC=MC, lag=lag, launch=c["launch"],
                sample="alt", zero_share=zero, flow_days=sum(1 for v in c["flow"] if v is not None))


# ------------------------------------------------------------------ signals
def expanding_pct(v, min_prior):
    """Percentile of each value among the earlier ones (ties count half, so a run of zero-flow
    days sits in the middle, not at the bottom); NaN until min_prior earlier values exist."""
    out, seen = np.full(len(v), NAN), []
    for i, x in enumerate(v):
        if x != x:
            continue
        if len(seen) >= min_prior:
            out[i] = (bisect.bisect_left(seen, x) + bisect.bisect_right(seen, x)) / 2 / len(seen)
        bisect.insort(seen, x)
    return out


def build_signals(c):
    """Per decision index d: 0/1 signal arrays (NaN = not defined), forward returns, flow sums."""
    n, lag = len(c["days"]), c["lag"]
    lp = np.array([math.log(p) if p else NAN for p in c["P"]])
    full = np.array([c["F"][i] / c["MC"][i] * 1e4 if c["F"][i] is not None and c["MC"][i] else NAN for i in range(n)])
    first = np.array([c["K"][i] / c["MC"][i] * 1e4 if c["K"][i] is not None and c["MC"][i] else NAN for i in range(n)])
    has = np.where(np.isfinite(full) | np.isfinite(first))[0]
    ff = int(has[0]) if len(has) else n

    def known_sum(t, x):  # trade dates t-x+1..t as known at d = t + lag (newest day as first published)
        if t - x + 1 < ff or t >= n:
            return NAN
        v = np.append(full[t - x + 1:t], first[t])
        return float(v.sum()) if np.all(np.isfinite(v)) else NAN

    def ret(a, b):
        return lp[b] - lp[a] if 0 <= a and b < n else NAN

    cum = {x: np.array([known_sum(d - lag, x) for d in range(n)]) for x in sorted(set(CUM_X + FLIP_Y + DIV_X))}
    sig = {}
    minp = MIN_PRIOR[c["sample"]]
    pcts = {}
    for x in CUM_X:
        pc = expanding_pct(cum[x], minp)
        pcts[x] = pc
        sig[f"CUMTOP {x}"] = np.where(np.isfinite(pc), (pc >= 0.8).astype(float), NAN)
        sig[f"CUMBOT {x}"] = np.where(np.isfinite(pc), (pc < 0.2).astype(float), NAN)

    def crossing(S):
        up, dn = np.full(n, NAN), np.full(n, NAN)
        for d in range(3, n):
            w = S[d - 3:d + 1]
            if np.all(np.isfinite(w)):
                up[d] = float(w[3] > 0 and np.all(w[:3] <= 0))
                dn[d] = float(w[3] <= 0 and np.all(w[:3] > 0))
        return up, dn

    start = ff + lag
    for y in FLIP_Y:
        S = cum[y]
        sig[f"FLIPUP {y}"], sig[f"FLIPDN {y}"] = crossing(S)
        sig[f"INFLOW {y}"] = np.where(np.isfinite(S), (S > 0).astype(float), NAN)
        R = np.array([ret(d - y, d) if d >= start else NAN for d in range(n)])
        sig[f"PFLIPUP {y}"], sig[f"PFLIPDN {y}"] = crossing(R)
        sig[f"PUP {y}"] = np.where(np.isfinite(R), (R > 0).astype(float), NAN)
    for k in STREAK_N:
        s = np.full(n, NAN)
        for d in range(n):
            t = d - lag
            if t - k + 1 < ff or t >= n:
                continue
            v = np.append(full[t - k + 1:t], first[t])
            if np.all(np.isfinite(v)):
                s[d] = float(np.all(v > 0))
        sig[f"STREAK {k}"] = s
    for x in DIV_X:
        a, b = np.full(n, NAN), np.full(n, NAN)
        for d in range(n):
            r = ret(d - lag - x, d - lag)
            if np.isfinite(cum[x][d]) and np.isfinite(r):
                a[d] = float(cum[x][d] > 0 and r < 0)
                b[d] = float(cum[x][d] < 0 and r > 0)
        sig[f"DIPBUY {x}"], sig[f"RALLYSELL {x}"] = a, b
    for x in CUM_X[1:]:
        pc = expanding_pct(np.array([ret(d - x, d) if d >= start else NAN for d in range(n)]), minp)
        sig[f"PTOP {x}"] = np.where(np.isfinite(pc), (pc >= 0.8).astype(float), NAN)
    fwd = {h: np.array([ret(d, d + h) * 100 for d in range(n)]) for h in HORIZONS}
    return dict(sig=sig, fwd=fwd, cum=cum, pcts=pcts, lp=lp, full=full, first=first)


# ------------------------------------------------------------------ statistics
def norm_p(t):
    return math.erfc(abs(t) / math.sqrt(2))


def hac_ols(y, X, lags, groups=None):
    """OLS of y on [1, X]; Newey-West (Bartlett) covariance, or Driscoll-Kraay when `groups`
    (an integer time key per row) is given; standard errors never below the classical ones."""
    X = np.column_stack([np.ones(len(y))] + list(X))
    n, k = X.shape
    XtX = np.linalg.pinv(X.T @ X)
    b = XtX @ (X.T @ y)
    e = y - X @ b
    U = X * e[:, None]
    if groups is not None:
        T = int(groups.max()) + 1
        Ug = np.zeros((T, k))
        np.add.at(Ug, groups, U)
        U = Ug[np.any(Ug != 0, axis=1)]
    S = U.T @ U
    for L in range(1, min(lags, len(U) - 1) + 1):
        G = U[L:].T @ U[:-L]
        S += (1 - L / (lags + 1)) * (G + G.T)
    V = XtX @ S @ XtX
    V0 = XtX * (e @ e) / max(n - k, 1)
    se = np.sqrt(np.maximum(np.diag(V), np.diag(V0)))
    return b, se


def episodes(s):
    s = np.nan_to_num(s)
    return int(np.sum((s[1:] == 1) & (s[:-1] == 0)) + (1 if len(s) and s[0] == 1 else 0))


def t_single(s, y, h, extra=None):
    m = np.isfinite(s) & np.isfinite(y)
    if extra is not None:
        m &= np.isfinite(extra)
    on = int(s[m].sum())
    if on < MIN_ON or m.sum() - on < MIN_ON:
        return None
    b, se = hac_ols(y[m], [s[m]] + ([extra[m]] if extra is not None else []), h)
    return float(b[1]), float(b[1] / se[1]), m


def test_single(s, y, h, extra=None):
    r = t_single(s, y, h, extra)
    if r is None:
        return None
    diff, t, m = r
    sv, yv = s[m], y[m]
    on, off = yv[sv == 1], yv[sv == 0]
    return dict(diff=diff, t=t, p=norm_p(t), n=int(m.sum()), n_on=int(len(on)), events=episodes(s[m]),
                mean_on=float(on.mean()), mean_off=float(off.mean()),
                hit_on=float((on > 0).mean()), hit_off=float((off > 0).mean()))


class Pool:
    """The pooled altcoins, as stacked arrays: date key, coin id, forward returns per h."""

    def __init__(self, coins):
        self.coins = coins
        all_days = sorted({d for c in coins for d in c["days"]})
        self.key = {d: i for i, d in enumerate(all_days)}

    def stack(self, name, h, twin=None, shift=0):
        ys, ss, ts, gs, raw_y, raw_s = [], [], [], [], [], []
        for c in self.coins:
            s = c["S"]["sig"][name]
            if shift:
                s = np.roll(s, shift % len(s))
            y = c["S"]["fwd"][h]
            tw = c["S"]["sig"].get(twin) if twin else None
            m = np.isfinite(s) & np.isfinite(y)
            if tw is not None:
                m &= np.isfinite(tw)
            if m.sum() < 30:
                continue
            yy, sv = y[m], s[m]
            raw_y.append(yy)
            raw_s.append(sv)
            ys.append(yy - yy.mean())
            ss.append(sv - sv.mean())
            if tw is not None:
                ts.append(tw[m] - tw[m].mean())
            gs.append(np.array([self.key[c["days"][i]] for i in np.where(m)[0]]))
        return ys, ss, ts, gs, raw_y, raw_s

    def t(self, name, h, twin=None, shift=0):
        ys, ss, ts, gs, raw_y, raw_s = self.stack(name, h, twin, shift)
        if not ys:
            return None
        on = int(sum(s.sum() for s in raw_s))
        if on < 2 * MIN_ON:
            return None
        X = [np.concatenate(ss)] + ([np.concatenate(ts)] if twin else [])
        b, se = hac_ols(np.concatenate(ys), X, h, groups=np.concatenate(gs))
        return float(b[1]), float(b[1] / se[1]), (raw_y, raw_s)

    def test(self, name, h, twin=None):
        r = self.t(name, h, twin)
        if r is None:
            return None
        diff, t, (raw_y, raw_s) = r
        y, s = np.concatenate(raw_y), np.concatenate(raw_s)
        on, off = y[s == 1], y[s == 0]
        return dict(diff=diff, t=t, p=norm_p(t), n=int(len(y)), n_on=int(len(on)),
                    events=int(sum(episodes(x) for x in raw_s)), mean_on=float(on.mean()), mean_off=float(off.mean()),
                    hit_on=float((on > 0).mean()), hit_off=float((off > 0).mean()))


def bh(ps):
    idx = sorted(range(len(ps)), key=lambda i: ps[i])
    m = len(ps)
    q = [None] * m
    run = 1.0
    for rank in range(m, 0, -1):
        i = idx[rank - 1]
        run = min(run, ps[i] * m / rank)
        q[i] = run
    return q


# ------------------------------------------------------------------ flips: are they turning points?
def flip_paths(c, name, span=20):
    """Average log-return path around each event, days -span..+span from the flow's trade date t,
    relative to the close of t; days since the 20-day low and the rise from it, at t."""
    s, lp, lag = c["S"]["sig"][name], c["S"]["lp"], c["lag"]
    n = len(lp)
    paths, lows, gains, highs, drops = [], [], [], [], []
    for d in np.where(s == 1)[0]:
        t = int(d) - lag
        if t - span < 0 or not np.isfinite(lp[t]):
            continue
        paths.append([lp[t + k] - lp[t] if 0 <= t + k < n else NAN for k in range(-span, span + 1)])
        w = lp[t - span:t + 1]
        if np.any(np.isfinite(w)):
            li, hi = int(np.nanargmin(w)), int(np.nanargmax(w))
            lows.append(span - li)
            gains.append(float((lp[t] - w[li]) * 100))
            highs.append(span - hi)
            drops.append(float((lp[t] - w[hi]) * 100))
    if not paths:
        return None
    A = np.array(paths)
    fin = np.isfinite(A)
    cnt = fin.sum(axis=0)
    tot = np.where(fin, A, 0.0).sum(axis=0)
    avg = np.where(cnt >= 3, tot / np.maximum(cnt, 1) * 100, np.nan)
    return dict(n=len(paths), path=[None if v != v else round(float(v), 3) for v in avg],
                lows=lows, gains=gains, highs=highs, drops=drops)


def summarize_flips(parts):
    parts = [p for p in parts if p]
    if not parts:
        return None
    n = sum(p["n"] for p in parts)
    L = len(parts[0]["path"])
    path = []
    for k in range(L):
        v = [(p["path"][k], p["n"]) for p in parts if p["path"][k] is not None]
        path.append(round(sum(a * b for a, b in v) / sum(b for _, b in v), 3) if v else None)
    lows = sorted(x for p in parts for x in p["lows"])
    gains = sorted(x for p in parts for x in p["gains"])
    highs = sorted(x for p in parts for x in p["highs"])
    drops = sorted(x for p in parts for x in p["drops"])
    med = lambda v: v[len(v) // 2] if v else None  # noqa: E731
    return dict(n=n, path=path, days_since_low_median=med(lows),
                share_within2=sum(1 for v in lows if v <= 2) / len(lows) if lows else None,
                gain_from_low_median=med(gains), days_since_high_median=med(highs),
                share_within2_high=sum(1 for v in highs if v <= 2) / len(highs) if highs else None,
                drop_from_high_median=med(drops))


# ------------------------------------------------------------------ dose response (Q: how much inflow?)
def dose(c_list, x, h):
    """Forward h-day return by quintile of the expanding percentile of the x-day flow sum."""
    ys = {b: [] for b in range(5)}
    for c in c_list:
        pc, y = c["S"]["pcts"][x], c["S"]["fwd"][h]
        m = np.isfinite(pc) & np.isfinite(y)
        for p, v in zip(pc[m], y[m]):
            ys[min(int(p * 5), 4)].append(v)
    out = []
    for b in range(5):
        v = np.array(ys[b])
        out.append(None if len(v) < 5 else dict(n=int(len(v)), mean=float(v.mean()), hit=float((v > 0).mean())))
    return out


def thresholds_bp(c, x):
    """Today's 20th / 80th percentile of the coin's x-day flow sum, in bp (what 'top 20 %' means)."""
    v = np.sort(c["S"]["cum"][x][np.isfinite(c["S"]["cum"][x])])
    if len(v) < 20:
        return None
    return dict(p20=float(v[int(0.2 * (len(v) - 1))]), p80=float(v[int(0.8 * (len(v) - 1))]),
                median=float(np.median(v)))


# ------------------------------------------------------------------ main
def rnd(v, k=4):
    if isinstance(v, (float, np.floating)):
        v = float(v)
        return None if v != v else round(v, k)
    if isinstance(v, dict):
        return {a: rnd(b, k) for a, b in v.items()}
    if isinstance(v, (list, tuple)):
        return [rnd(b, k) for b in v]
    if isinstance(v, np.integer):
        return int(v)
    return v


def today_state(c):
    """The latest decision close: each signal's state and the flow sums behind them."""
    S, n = c["S"], len(c["days"])
    d = n - 1
    while d > 0 and not np.isfinite(S["cum"][5][d]):
        d -= 1
    st = {k: int(v[d]) for k, v in S["sig"].items() if np.isfinite(v[d]) and not k.startswith("P")}
    vals = {f"cum{x}": float(S["cum"][x][d]) for x in (5, 20) if np.isfinite(S["cum"][x][d])}
    pct = {f"cum{x}": float(S["pcts"][x][d]) for x in (5, 20) if np.isfinite(S["pcts"][x][d])}
    s10, run, sgn10 = S["cum"][10], 0, None
    sgn = lambda v: 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)  # noqa: E731  (0: no flows at all)
    if np.isfinite(s10[d]):
        sgn10 = sgn(s10[d])
        k = d
        while k >= 0 and np.isfinite(s10[k]) and sgn(s10[k]) == sgn10:
            run += 1
            k -= 1
    recent = {}
    for k, v in S["sig"].items():
        if k.split(" ")[0] in ("FLIPUP", "FLIPDN"):
            hits = [j for j in range(max(0, d - 4), d + 1) if v[j] == 1]
            if hits:
                recent[k] = c["days"][hits[-1] - c["lag"]]
    mc = next((c["MC"][i] for i in range(min(d, n - 1), -1, -1) if c["MC"][i]), None)
    return dict(decision_day=c["days"][d], flow_through=c["days"][d - c["lag"]], states=st, values=vals, pct=pct, mcap=mc,
                inflow10=sgn10, inflow10_days=run,
                flips_last5=recent, thr5=thresholds_bp(c, 5), thr20=thresholds_bp(c, 20))


def run(iters=200):
    """Everything the website's signal tab shows, as one JSON-ready dict."""
    daily = load_json(P("data", "daily.json"))
    long = load_json(P("data", "daily_long.json"))
    coins = {}
    for s in ("BTC", "ETH"):
        if s in long.get("coins", {}):
            coins[s] = coin_long(s, long)
    for s in ALTS:
        c = coin_alt(s, daily)
        if c:
            coins[s] = c
    for c in coins.values():
        c["S"] = build_signals(c)
    names = list(coins["BTC"]["S"]["sig"].keys())
    flow_names = [k for k in names if not k.startswith("P")]
    pool_coins = [coins[s] for s in ALTS if s in coins and coins[s]["flow_days"] >= POOL_MIN_DAYS
                  and coins[s]["zero_share"] <= POOL_MAX_ZERO]
    # BTC's flow signals as a signal for the altcoins (does BTC ETF demand lead the altcoins?)
    btc = coins["BTC"]
    bidx = {d: i for i, d in enumerate(btc["days"])}
    cross_coins = []
    for c in pool_coins:
        cc = dict(c)
        cc["S"] = dict(c["S"])
        cc["S"]["sig"] = {nm: np.array([btc["S"]["sig"][nm][bidx[d]] if d in bidx else NAN for d in c["days"]])
                          for nm in names}
        cross_coins.append(cc)
    pools = {"ALTS": Pool(pool_coins), "BTC>ALTS": Pool(cross_coins)}

    results = {}
    for samp in ("BTC", "ETH"):
        if samp in coins:
            S = coins[samp]["S"]
            for name in names:
                for h in HORIZONS:
                    r = test_single(S["sig"][name], S["fwd"][h], h)
                    if r:
                        results[(samp, name, h)] = r
    for samp, pl in pools.items():
        for name in names:
            for h in HORIZONS:
                r = pl.test(name, h)
                if r:
                    results[(samp, name, h)] = r
    fam = [k for k in results if k[0] in MAIN and k[1] in flow_names]
    for k, q in zip(fam, bh([results[k]["p"] for k in fam])):
        results[k]["q"] = q

    # rotation test over the whole family: per-test p and the chance of the best |t|
    rng = random.Random(20261005)
    obs = {k: abs(results[k]["t"]) for k in fam}
    exceed = {k: 0 for k in fam}
    best_exceed, best_obs = 0, max(obs.values())
    for _ in range(iters):
        sh = {smp: rng.randrange(25, max(26, len(coins[smp]["days"]) - 25)) for smp in ("BTC", "ETH") if smp in coins}
        for smp in POOLED:
            sh[smp] = rng.randrange(25, 175)
        best = 0.0
        for k in fam:
            smp, name, h = k
            if smp in pools:
                r = pools[smp].t(name, h, shift=sh[smp])
            else:
                S = coins[smp]["S"]
                r = t_single(np.roll(S["sig"][name], sh[smp]), S["fwd"][h], h)
            if r is None:
                continue
            at = abs(r[1])
            best = max(best, at)
            if at >= obs[k]:
                exceed[k] += 1
        best_exceed += best >= best_obs
    for k in fam:
        results[k]["p_rot"] = (exceed[k] + 1) / (iters + 1)
    reality = dict(max_abs_t=best_obs, p_best=(best_exceed + 1) / (iters + 1), iters=iters, n_tests=len(fam))

    # does the flow add anything to its price twin? and the two halves of each main sample
    halves_at = {}
    for smp in ("BTC", "ETH"):
        if smp in coins:
            halves_at[smp] = coins[smp]["days"][len(coins[smp]["days"]) // 2]
    alt_days = sorted({d for c in pool_coins for d in c["days"]})
    for smp in POOLED:
        halves_at[smp] = alt_days[len(alt_days) // 2]
    for k in fam:
        smp, name, h = k
        base, par = name.split(" ")
        twin = PRICE_TWIN.get(base)
        tw = f"{twin} {par}" if twin else None
        if tw and tw in names:
            if smp in pools:
                r2 = pools[smp].t(name, h, twin=tw)
            else:
                S = coins[smp]["S"]
                r2 = t_single(S["sig"][name], S["fwd"][h], h, extra=S["sig"][tw])
            if r2:
                results[k]["vs_price"] = dict(diff=r2[0], t=r2[1], p=norm_p(r2[1]), twin=tw)
        cut = halves_at[smp]
        hv = []
        for first_half in (True, False):
            if smp in pools:
                sub = []
                for c in pools[smp].coins:
                    mask = np.array([(d < cut) == first_half for d in c["days"]])
                    cc = dict(c)
                    cc["S"] = dict(c["S"])
                    cc["S"]["sig"] = dict(c["S"]["sig"])
                    cc["S"]["sig"][name] = np.where(mask, c["S"]["sig"][name], NAN)
                    sub.append(cc)
                r3 = Pool(sub).t(name, h)
                hv.append(None if r3 is None else dict(diff=r3[0], t=r3[1]))
            else:
                S = coins[smp]["S"]
                mask = np.array([(d < cut) == first_half for d in coins[smp]["days"]])
                r3 = t_single(np.where(mask, S["sig"][name], NAN), S["fwd"][h], h)
                hv.append(None if r3 is None else dict(diff=r3[0], t=r3[1]))
        results[k]["halves"] = hv

    flips = {}
    for y in FLIP_Y:
        for nm in (f"FLIPUP {y}", f"PFLIPUP {y}", f"FLIPDN {y}", f"PFLIPDN {y}"):
            for smp in ("BTC", "ETH"):
                if smp in coins:
                    f = summarize_flips([flip_paths(coins[smp], nm)])
                    if f:
                        flips[f"{smp}|{nm}"] = f
            f = summarize_flips([flip_paths(c, nm) for c in pool_coins])
            if f:
                flips[f"ALTS|{nm}"] = f

    doses = {}
    for x in (5, 10, 20, 60):
        for h in (5, 10, 20):
            for smp in ("BTC", "ETH", "ALTS"):
                cl = pool_coins if smp == "ALTS" else ([coins[smp]] if smp in coins else [])
                if cl:
                    doses[f"{smp}|{x}|{h}"] = dose(cl, x, h)

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "clock": "daily close 00:00 UTC (08:00 Singapore)", "horizons": HORIZONS,
           "samples": {s: {"from": c["days"][1], "flows_to": c["days"][-1 - c["lag"]], "n": len(c["days"]) - 1 - c["lag"],
                           "lag": c["lag"], "zero_share": c.get("zero_share"), "in_pool": c in pool_coins}
                       for s, c in coins.items()},
           "pool": [c["sym"] for c in pool_coins],
           "samples_main": list(MAIN),
           "halves_at": halves_at,
           "tests": [dict(sample=k[0], signal=k[1], h=k[2], **rnd(v)) for k, v in results.items() if k[0] in MAIN],
           "reality": rnd(reality), "flips": rnd(flips), "dose": rnd(doses),
           "thresholds": {s: {f"x{x}": rnd(thresholds_bp(c, x)) for x in (5, 10, 20, 60)} for s, c in coins.items()},
           "today": {s: rnd(today_state(c)) for s, c in coins.items()}}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--iters", type=int, default=200)
    a = ap.parse_args()
    out = run(a.iters)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return out


if __name__ == "__main__":
    o = main()
    T = o["tests"]
    fam = [t for t in T if "q" in t]
    print("family tests:", len(fam), "| q<0.10:", sum(1 for t in fam if t["q"] < 0.10), "| p<0.05:",
          sum(1 for t in fam if t["p"] < 0.05), "| rotation p<0.05:", sum(1 for t in fam if t["p_rot"] < 0.05))
    print("reality:", o["reality"], "halves at", o["halves_at"])
    for t in sorted(fam, key=lambda t: t["p"])[:20]:
        vp = t.get("vs_price") or {}
        hv = t.get("halves") or []
        print(f"{t['sample']:5s} {t['signal']:12s} h={t['h']:2d} diff {t['diff']:+6.2f}% t {t['t']:+5.2f} p {t['p']:.4f} "
              f"q {t['q']:.3f} prot {t['p_rot']:.3f} on {t['n_on']:4d} ev {t['events']:3d} on {t['mean_on']:+6.2f} off {t['mean_off']:+6.2f}"
              + (f" | +price t {vp['t']:+.2f}" if vp else "")
              + (" | halves " + " ".join("—" if x is None else f"{x['diff']:+.2f}({x['t']:+.1f})" for x in hv) if hv else ""))
