#!/usr/bin/env python3
"""Altcoin leverage radar (ALR): a transparent take on CoinKarma's Alt Top Radar, and its backtest.

Inputs (data/oi/, from scripts/fetch_oi_binance.py and scripts/fetch_cm_mcap.py):
  Binance USD-M perpetual open interest per contract and UTC day (end of day, ~23:55 UTC)
  Coin Metrics market cap and price per asset and UTC day (end of day)

Series
  alt market cap  sum of altcoin market caps: ex BTC, ETH, stablecoins, wrapped/staked copies,
                  gold tokens and tokenized funds. Chain-linked for returns, so an asset entering
                  the data does not jump the index.
  alt OI          US$ OI summed over altcoin perpetuals: ex BTC, ETH, stocks, ETFs, metals,
                  indexes, stablecoins. Chain-linked the same way, so new listings do not jump it.
  major OI        BTCUSDT + ETHUSDT

ALR, 0-100. PARAMS were fixed before any outcome was looked at; the sensitivity grid in the
report shows how much the results move when they change.
  concentration  14-day growth of alt OI relative to major OI, as a percentile of the past year,
                 counted only while BTC moved less than 10% over the same 14 days
  leverage       14-day growth of alt OI relative to alt market cap (OI outrunning price), percentile
  heat           best day of the last 10 for min(concentration, leverage)
  near the top   alt market cap vs its 60-day high: weight 0 at 85%, 1 from 95%
  receding       alt OI vs its 7-day high: weight 0 at the high, 1 from 5% below
  ALR = 100 x heat x near x receding
Alert: ALR crosses 90; at least 14 days after the previous alert; when BTC rose 20%+ over the past
30 days the first overheat is skipped and only a second one within 30 days alerts (CoinKarma's
rule; the report shows results with and without it).

Usage: python3 research/alt_radar.py [--out research/alt_radar]
"""
import argparse
import bisect
import csv
import datetime as dt
import glob
import gzip
import json
import math
import os
import random
import statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "oi")

PARAMS = dict(L=14, year=365, btc_flat=0.10, heat_days=10, near_win=60, near_lo=0.85, near_hi=0.95,
              recede_win=7, recede_full=0.05, thr=90, gap=14, rally=0.20, rally_win=30, rally_rule=True,
              horizon=30)
SPLIT = "2025-01-01"  # results are shown for alerts before and from this day
# v1 above alerted 0.8 times a year (too strict). v2 keeps every component and changes only the
# scaling: ALR = where today's heat x near x receding ranks within the past year, with the heat pooled
# as the mean of the two percentiles; the threshold (95) was picked from 85/90/95 as the one closest
# to CoinKarma's ~5 alerts a year, looking at alert counts only, before any outcome was computed.
PARAMS_V2 = dict(PARAMS, mode="v2", thr=95)

# ------------------------------------------------------------------ contracts that are not altcoins
INDEX_STABLE = {"BTCDOM", "DEFI", "BLUEBIRD", "FOOTBALL", "USDC", "USDEX", "USDBRL", "TUSD", "BUSD", "USDP"}
METALS_COMMODITIES = {"XAU", "XAG", "XPT", "XPD", "PAXG", "XAUT", "COPPER", "NATGAS", "CL", "BZ", "URNM", "GDX", "XLE"}
EQUITY = {  # stocks, ETFs and pre-IPO contracts Binance listed from 2025 on
    "AAOI", "AAPL", "ADBE", "AKAM", "AMAT", "AMC", "AMD", "AMZN", "ANET", "APLD", "APP", "ARM", "ASML", "ASTS",
    "AVGO", "BABA", "BMNR", "BRKB", "BYD", "CEG", "CIEN", "COHR", "COIN", "COST", "CRCL", "CRDO", "CRM", "CRWD",
    "CRWV", "CSCO", "CVNA", "CYPH", "DDOG", "DELL", "DIS", "DJT", "DKNG", "EBAY", "FLNC", "FWDI", "GEV", "GLW", "GME",
    "GOOGL", "GTLB", "HIMS", "HOOD", "HPE", "HUT", "IBKR", "IBM", "INTC", "IONQ", "IREN", "JPM", "KLAC", "LLY",
    "LRCX", "MARA", "MCD", "MDB", "META", "MRK", "MRNA", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NFLX", "NKE", "NOK",
    "NVDA", "NVO", "OKLO", "ONDS", "ORCL", "PANW", "PDD", "PLTR", "PYPL", "QCOM", "RDDT", "RDW", "RIVN", "RKLB",
    "SHOP", "SMCI", "SNDK", "SNOW", "SNPS", "SOFI", "SONY", "TEM", "TSLA", "TSM", "TTWO", "TXN", "UBER", "UNH",
    "VKTX", "WDC", "WMT", "XOM", "QQQ", "SPY", "IWM", "SMH", "SOXL", "SOXS", "TQQQ", "SQQQ", "UVXY", "TZA", "TMF",
    "TSLL", "NVDL", "MSTU", "EWJ", "EWY", "EWZ", "XBI", "KODEX200", "CSOPSAMSUNG2L", "CSOPSKHYNIX2L", "HK0625",
    "HK0700", "HK0992", "HK1810", "TENCENT", "KUAISHOU", "MEITUAN", "POPMART", "SAMSUNG", "SAMSUNGEM", "SKHYNIX",
    "HYUNDAI", "NAVER", "LGELECTRONICS", "HANMI", "ZHIPU", "MINIMAX", "CXMT", "UNITREE", "ZHONGJI", "OPENAI",
    "ANTHROPIC", "SPCX"}
NOT_ALT_ASSETS = {"btc", "eth", "paxg", "xaut", "kau", "kag", "eurc", "eurs", "eurt", "ageur", "eure", "xsgd", "gyen",
                  "buidl", "ousg", "usyc", "ustb", "benji", "jtrsy", "jaaa"}
PEG_BASES = ["btc", "eth", "sol", "bnb", "avax", "hype", "trx", "sui", "near", "ada", "xrp", "doge", "ltc", "dot"]
# the same token counted twice: a copy on another chain (leo_eth), or a migration both sides of
# which Coin Metrics still values (matic -> pol). Each group counts once a day, at its largest member.
CHAIN_SUFFIX = {"eth", "eos", "evm", "native", "bsc", "trx", "sol", "matic", "arb", "op", "base", "avaxc", "polygon"}
SAME_TOKEN = [("matic", "pol"), ("rndr", "render"), ("lend", "aave"), ("ftm", "s"), ("eos", "a"), ("mkr", "sky_sky")]
NEW_CONTRACTS = "2025-06-01"  # Binance lists stocks, ETFs and metals from here on: newer contracts must
WEEKEND_MIN = 0.5             # move on weekends at least half as much as on weekdays to count as crypto


def d2s(d):
    return d.isoformat()


def s2d(s):
    return dt.date.fromisoformat(s)


# ------------------------------------------------------------------ loading
def load_oi():
    """symbol -> {date: (oi_usd_last, qty_last, n)}"""
    oi = defaultdict(dict)
    for fn in sorted(glob.glob(os.path.join(DATA, "binance", "*.csv.gz"))):
        with gzip.open(fn, "rt", newline="") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if len(row) < 8:
                    continue
                date, sym, oi_last, _, qty_last = row[:5]
                try:
                    v = float(oi_last)
                    q = float(qty_last) if qty_last else None
                    n = int(row[7] or 0)
                except ValueError:
                    continue
                if v > 0:
                    oi[sym][date] = (v, q, n)
    return oi


def load_cm():
    """asset -> {date: (mcap, price)}"""
    cm = defaultdict(dict)
    for fn in sorted(glob.glob(os.path.join(DATA, "cm", "*.csv.gz"))):
        with gzip.open(fn, "rt", newline="") as f:
            r = csv.reader(f)
            next(r, None)
            for row in r:
                if len(row) < 4:
                    continue
                date, a, mc, px = row
                try:
                    mc = float(mc)
                except ValueError:
                    continue
                try:
                    px = float(px) if px else None
                except ValueError:
                    px = None
                if mc > 0:
                    cm[a][date] = (mc, px)
    return cm


# ------------------------------------------------------------------ classification
def weekend_ratio(series):
    """median |daily return| on Sat/Sun over Tue-Fri, from contract prices (OI value / OI qty).
    Stocks, ETFs and metals barely move on weekends; crypto moves about as much as on weekdays."""
    px = {s2d(d): v / q for d, (v, q, n) in series.items() if q and q > 0}
    we, wd = [], []
    for d, p in px.items():
        p0 = px.get(d - dt.timedelta(days=1))
        if not p0 or p <= 0:
            continue
        r = abs(math.log(p / p0))
        (we if d.weekday() >= 5 else wd if 1 <= d.weekday() <= 4 else []).append(r)
    if len(we) < 6 or len(wd) < 10:
        return None
    m = statistics.median(wd)
    return statistics.median(we) / m if m > 0 else None


def classify_contracts(oi):
    out = {}
    for sym, s in oi.items():
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in ("BTC", "ETH"):
            out[sym] = "major"
        elif base in INDEX_STABLE:
            out[sym] = "index/stable"
        elif base in METALS_COMMODITIES:
            out[sym] = "commodity"
        elif base in EQUITY:
            out[sym] = "equity"
        elif min(s) < NEW_CONTRACTS:
            out[sym] = "alt"
        else:
            wr = weekend_ratio(s)
            out[sym] = "too new to tell" if wr is None else "alt" if wr >= WEEKEND_MIN else "equity (weekend test)"
    return out


def classify_assets(cm):
    """asset -> 'alt' | reason it is not counted"""
    px = {a: {d: p for d, (m, p) in s.items() if p} for a, s in cm.items()}
    out = {}
    for a, s in cm.items():
        if a in NOT_ALT_ASSETS:
            out[a] = "excluded"
            continue
        p = px.get(a, {})
        vals = [v for d, v in sorted(p.items())][-365:]
        if len(vals) >= 30:
            dev = sorted(abs(v - 1) for v in vals)
            if dev[len(dev) // 2] < 0.02 and dev[int(len(dev) * 0.95)] < 0.08:
                out[a] = "stablecoin"
                continue
            rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals)) if vals[i] > 0 and vals[i - 1] > 0]
            if len(rets) > 20 and statistics.pstdev(rets) < 0.005:
                out[a] = "yield/fund token"  # sUSDe, USDY, sDAI: drift, no volatility
                continue
        pegged = None
        for b in PEG_BASES:
            if b == a or b not in px:
                continue
            common = [d for d in sorted(p) if d in px[b]][-365:]
            if len(common) < 60:
                continue
            ratios = [p[d] / px[b][d] for d in common if px[b][d] > 0]
            mu = statistics.mean(ratios)
            if mu > 0 and statistics.pstdev(ratios) / mu < 0.05:
                pegged = b
                break
        out[a] = f"pegged to {pegged}" if pegged else "alt"
    return out


def dup_groups(cm, alts):
    """Groups of assets that are the same token (see CHAIN_SUFFIX / SAME_TOKEN), plus any two of the
    300 largest whose price ratio stays within 3% (coefficient of variation) over 60+ shared days."""
    import numpy as np
    parent = {a: a for a in alts}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a in alts:
        if "_" in a:
            base, suf = a.split("_", 1)
            if suf in CHAIN_SUFFIX and base in parent:
                union(base, a)
    for a, b in SAME_TOKEN:
        if a in parent and b in parent:
            union(a, b)
    mx = {a: max(m for m, p in cm[a].values()) for a in alts}
    top = sorted(alts, key=lambda a: -mx[a])[:300]
    days = sorted({d for a in top for d in cm[a]})
    col = {d: i for i, d in enumerate(days)}
    P = np.full((len(top), len(days)), np.nan)
    for i, a in enumerate(top):
        for d, (m, p) in cm[a].items():
            if p and p > 0:
                P[i, col[d]] = p
    for i in range(len(top) - 1):
        R = P[i] / P[i + 1:]
        ok = np.isfinite(R)
        n = ok.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            mu = np.nanmean(np.where(ok, R, np.nan), axis=1)
            sd = np.nanstd(np.where(ok, R, np.nan), axis=1)
            cv = sd / mu
        for j in np.where((n >= 60) & (cv < 0.03))[0]:
            union(top[i], top[i + 1 + j])
    groups = defaultdict(list)
    for a in alts:
        groups[find(a)].append(a)
    return [sorted(g, key=lambda a: -mx[a]) for g in groups.values()]


# ------------------------------------------------------------------ series
def day_range(first, last):
    d, out = s2d(first), []
    while d <= s2d(last):
        out.append(d2s(d))
        d += dt.timedelta(days=1)
    return out


def chain(series, dates, max_jump):
    """Chain-linked index (start 100) and raw sum: each day's change uses only members present on
    that day and on the previous day with data; a member whose value jumps by more than max_jump in
    a day is left out that day. A day with no data at all (Binance skipped 2022-03-07) carries the
    previous level and is not in tot."""
    idx, tot, level, prev = {}, {}, 100.0, None
    for d in dates:
        s = sum(v[d] for v in series.values() if d in v)
        if s <= 0:
            if prev is not None:
                idx[d] = level
            continue
        tot[d] = s
        if prev is not None:
            num = den = 0.0
            for v in series.values():
                a, b = v.get(prev), v.get(d)
                if a and b and 1 / max_jump < b / a < max_jump:
                    num += b
                    den += a
            if den > 0:
                level *= num / den
        idx[d] = level
        prev = d
    return idx, tot


def build(oi, cm):
    ccls = classify_contracts(oi)
    acls = classify_assets(cm)
    alt_oi = {s: {d: v[0] for d, v in oi[s].items()} for s, c in ccls.items() if c == "alt"}
    first = max(min(oi["BTCUSDT"]), min(oi["ETHUSDT"]), min(min(v) for v in alt_oi.values()))
    last = min(max(oi["BTCUSDT"]), max(oi["ETHUSDT"]))
    alt_mc = {}
    for g in dup_groups(cm, [a for a, c in acls.items() if c == "alt"]):
        ser = {}
        for a in g:
            for d, (m, p) in cm[a].items():
                if m > ser.get(d, 0):
                    ser[d] = m
        alt_mc[g[0]] = ser
        for a in g[1:]:
            acls[a] = f"same token as {g[0]}"
    last = min(last, max(max(v) for v in alt_mc.values()))
    dates = day_range(min(first, min(min(v) for v in alt_mc.values())), last)
    oi_dates = [d for d in dates if d >= first]
    alt_oi_idx, alt_oi_sum = chain(alt_oi, oi_dates, 5.0)
    alt_idx, alt_sum = chain(alt_mc, dates, 3.0)
    major, prev = {}, None
    for d in oi_dates:  # a missing Binance day carries the previous value
        if d in oi["BTCUSDT"] and d in oi["ETHUSDT"]:
            prev = oi["BTCUSDT"][d][0] + oi["ETHUSDT"][d][0]
        if prev is not None:
            major[d] = prev
    btc = {d: p for d, (m, p) in cm["btc"].items() if p}
    return dict(dates=dates, oi_dates=oi_dates, alt_oi_idx=alt_oi_idx, alt_oi_sum=alt_oi_sum, major=major,
                alt_idx=alt_idx, alt_sum=alt_sum, btc=btc, contracts=ccls, assets=acls)


# ------------------------------------------------------------------ ALR
def pct_rank(hist, x):
    """share of past values below x (ties count half)"""
    lo = bisect.bisect_left(hist, x)
    hi = bisect.bisect_right(hist, x)
    return (lo + 0.5 * (hi - lo)) / len(hist)


def lagged(series, d, k):
    return series.get(d2s(s2d(d) - dt.timedelta(days=k)))


def alr(S, P=PARAMS):
    L, year = P["L"], P["year"]
    f1, f2, rb = {}, {}, {}
    for d in S["oi_dates"]:
        a0, a1 = lagged(S["alt_oi_idx"], d, L), S["alt_oi_idx"].get(d)
        m0, m1 = lagged(S["major"], d, L), S["major"].get(d)
        x0, x1 = lagged(S["alt_idx"], d, L), S["alt_idx"].get(d)
        b0, b1 = lagged(S["btc"], d, L), S["btc"].get(d)
        if None in (a0, a1, m0, m1, x0, x1, b0, b1):
            continue
        f1[d] = math.log(a1 / a0) - math.log(m1 / m0)
        f2[d] = math.log(a1 / a0) - math.log(x1 / x0)
        rb[d] = math.log(b1 / b0)
    days = sorted(f1)
    p1, p2 = {}, {}
    for i, d in enumerate(days):
        past = [x for x in days[max(0, i - year):i] if s2d(d) - s2d(x) <= dt.timedelta(days=year)]
        if len(past) < year * 0.8:
            continue
        h1 = sorted(f1[x] for x in past)
        h2 = sorted(f2[x] for x in past)
        p1[d] = pct_rank(h1, f1[d]) if abs(rb[d]) <= P["btc_flat"] else 0.0
        p2[d] = pct_rank(h2, f2[d])
    out, comp, H = {}, {}, {}
    pool = (lambda a, b: min(a, b)) if P.get("mode", "v1") == "v1" else (lambda a, b: (a + b) / 2)
    for d in sorted(p1):
        heat = max((pool(p1[x], p2[x]) for x in (d2s(s2d(d) - dt.timedelta(days=k)) for k in range(P["heat_days"]))
                    if x in p1), default=0.0)
        hi = max((v for k in range(P["near_win"]) for v in [lagged(S["alt_idx"], d, k)] if v), default=None)
        g = S["alt_idx"][d] / hi if hi else 0
        near = min(1.0, max(0.0, (g - P["near_lo"]) / (P["near_hi"] - P["near_lo"])))
        top = max((v for k in range(P["recede_win"]) for v in [lagged(S["alt_oi_idx"], d, k)] if v), default=None)
        q = S["alt_oi_idx"][d] / top if top else 1
        rec = min(1.0, max(0.0, (1 - q) / P["recede_full"]))
        H[d] = heat * near * rec
        comp[d] = dict(conc=round(p1[d], 3), lev=round(p2[d], 3), heat=round(heat, 3), near=round(near, 3),
                       rec=round(rec, 3))
    if P.get("mode", "v1") == "v1":
        out = {d: round(100 * h, 2) for d, h in H.items()}
    else:  # v2: where today's heat x near x receding ranks within the past year (at least 180 days)
        days = sorted(H)
        for i, d in enumerate(days):
            past = sorted(H[x] for x in days[max(0, i - year):i])
            if len(past) >= 180:  # no heat today (e.g. alt OI at its 7-day high) reads 0, not a tied rank
                out[d] = round(100 * pct_rank(past, H[d]), 2) if H[d] > 0 else 0.0
                comp[d]["raw"] = round(H[d], 4)
    return out, comp


def alerts(score, S, P=PARAMS):
    out, last, suppressed, above_prev = [], None, None, False
    for d in sorted(score):
        above = score[d] >= P["thr"]
        if above and not above_prev:
            dd = s2d(d)
            b0 = lagged(S["btc"], d, P["rally_win"])
            rally = b0 and S["btc"].get(d) and S["btc"][d] / b0 - 1 >= P["rally"]
            if last and (dd - last).days < P["gap"]:
                pass
            elif P["rally_rule"] and rally and not (suppressed and (dd - suppressed).days <= 30):
                suppressed = dd
            else:
                out.append(d)
                last, suppressed = dd, None
        above_prev = above
    return out


# ------------------------------------------------------------------ evaluation
def outcome(idx, d, H, delay=0):
    """30-day path after day d's close; delay=1 starts a day later (Binance publishes day d's file
    about 7 hours after midnight UTC, so an alert from these free files is known on d+1 morning)."""
    d = d2s(s2d(d) + dt.timedelta(days=delay))
    base = idx.get(d)
    path = [idx.get(d2s(s2d(d) + dt.timedelta(days=k))) for k in range(1, H + 1)]
    if not base or any(v is None for v in path):
        return None
    lo = min(range(H), key=lambda k: path[k])
    hi = max(range(H), key=lambda k: path[k])
    dd = min(0.0, path[lo] / base - 1)
    up = max(0.0, path[hi] / base - 1)
    kind = "down" if -dd > up else ("down_then_up" if lo < hi else "up_first")
    return dict(dd=dd, up=up, kind=kind, day_dd=lo + 1, day_up=hi + 1, ret=path[-1] / base - 1)


def evaluate(alert_days, S, score, P=PARAMS, lo=None, hi=None):
    H, idx = P["horizon"], S["alt_idx"]
    days = [d for d in sorted(score) if (lo is None or d >= lo) and (hi is None or d < hi)]
    oc = {d: outcome(idx, d, H, P.get("delay", 0)) for d in days}
    oc = {d: o for d, o in oc.items() if o}
    al = [d for d in alert_days if d in oc]
    if not oc:
        return None
    med = lambda xs: statistics.median(xs) if xs else None  # noqa: E731
    res = {"alerts": len(al), "days": len(oc),
           "per_year": round(len(al) / (len(oc) / 365.25), 2),
           "kinds": {k: sum(1 for d in al if oc[d]["kind"] == k) for k in ("down", "down_then_up", "up_first")},
           "median_dd": med([oc[d]["dd"] for d in al]), "median_up": med([oc[d]["up"] for d in al]),
           "median_ret": med([oc[d]["ret"] for d in al]),
           "random_dd": med([o["dd"] for o in oc.values()]), "random_up": med([o["up"] for o in oc.values()]),
           "random_down_share": sum(1 for o in oc.values() if o["kind"] == "down") / len(oc)}
    # matched control: days with a similar 30-day run-up, ALR below the threshold, no alert within 30 days
    r30 = {d: idx[d] / lagged(idx, d, 30) - 1 for d in oc if lagged(idx, d, 30)}
    near_alert = set()
    for a in alert_days:
        for k in range(-30, 31):
            near_alert.add(d2s(s2d(a) + dt.timedelta(days=k)))
    ctrl = set()
    for a in al:
        if a not in r30:
            continue
        for d in oc:
            if d in r30 and d not in near_alert and score.get(d, 0) < P["thr"] and abs(r30[d] - r30[a]) <= 0.05:
                ctrl.add(d)
    res["control_days"] = len(ctrl)
    res["control_dd"] = med([oc[d]["dd"] for d in ctrl])
    res["control_up"] = med([oc[d]["up"] for d in ctrl])
    # rotation test: shift every alert by the same number of days (wrapping inside the window)
    if len(al) >= 3:
        order = sorted(oc)
        pos = {d: i for i, d in enumerate(order)}
        n = len(order)
        obs = res["median_dd"]
        obs_down = res["kinds"]["down"] / len(al)
        le = ge = tot = 0
        for k in range(30, n - 30):
            sh = [order[(pos[d] + k) % n] for d in al]
            m = statistics.median(oc[d]["dd"] for d in sh)
            f = sum(1 for d in sh if oc[d]["kind"] == "down") / len(sh)
            le += m <= obs
            ge += f >= obs_down
            tot += 1
        res["p_rot_dd"] = round((le + 1) / (tot + 1), 3)
        res["p_rot_down_share"] = round((ge + 1) / (tot + 1), 3)
    for k in ("median_dd", "median_up", "median_ret", "random_dd", "random_up", "random_down_share",
              "control_dd", "control_up"):
        if res.get(k) is not None:
            res[k] = round(res[k], 4)
    res["list"] = [dict(date=d, alr=score[d], **{k: (round(v, 4) if isinstance(v, float) else v)
                                                  for k, v in oc[d].items()}) for d in al]
    return res


def run(S, P=PARAMS):
    score, comp = alr(S, P)
    al = alerts(score, S, P)
    return score, comp, al, {"all": evaluate(al, S, score, P), "before_" + SPLIT: evaluate(al, S, score, P, hi=SPLIT),
                             "from_" + SPLIT: evaluate(al, S, score, P, lo=SPLIT)}


def grid(S, base=PARAMS):
    rows = []
    for L in (10, 14, 21):
        for flat in (0.07, 0.10, 0.15):
            for rf in (0.03, 0.05, 0.08):
                for thr in (85, 90, 95):
                    for rule in (True, False):
                        P = dict(base, L=L, btc_flat=flat, recede_full=rf, thr=thr, rally_rule=rule)
                        score, _, al, ev = run(S, P)
                        e = ev["all"]
                        rows.append(dict(L=L, btc_flat=flat, recede_full=rf, thr=thr, rally_rule=rule,
                                         alerts=e["alerts"], per_year=e["per_year"], median_dd=e["median_dd"],
                                         median_up=e["median_up"], down=e["kinds"]["down"],
                                         control_dd=e["control_dd"], p_rot_dd=e.get("p_rot_dd")))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "research", "alt_radar"))
    ap.add_argument("--no-grid", action="store_true")
    a = ap.parse_args()
    random.seed(7)
    oi, cm = load_oi(), load_cm()
    S = build(oi, cm)
    os.makedirs(a.out, exist_ok=True)
    rep = {"split": SPLIT, "first_day": S["oi_dates"][0], "last_day": S["oi_dates"][-1],
           "contracts": {c: sum(1 for v in S["contracts"].values() if v == c) for c in set(S["contracts"].values())},
           "assets": {c: sum(1 for v in S["assets"].values() if v == c) for c in set(S["assets"].values())}}
    scores = {}
    for name, P in (("v1", PARAMS), ("v2", PARAMS_V2)):
        score, comp, al, ev = run(S, P)
        scores[name] = score
        rep[name] = {"params": P, "alerts": al, "eval": ev,
                     "eval_no_rally_rule": run(S, dict(P, rally_rule=False))[3],
                     "eval_delay_1d": run(S, dict(P, delay=1))[3],
                     "latest": {"date": max(score), "alr": score[max(score)], **comp[max(score)]}}
    if not a.no_grid:
        rep["v2"]["grid"] = grid(S, PARAMS_V2)
    with open(os.path.join(a.out, "backtest.json"), "w") as f:
        json.dump(rep, f, indent=1, sort_keys=True)
    series = {d: [round(S["alt_sum"].get(d, 0) / 1e9, 2), round(S["alt_idx"][d], 3),
                  round(S["alt_oi_sum"].get(d, 0) / 1e9, 3), round(S["alt_oi_idx"].get(d, 0), 3),
                  round(S["major"].get(d, 0) / 1e9, 3), scores["v1"].get(d), scores["v2"].get(d)]
              for d in S["dates"] if d in S["alt_idx"]}
    with open(os.path.join(a.out, "series.json"), "w") as f:
        json.dump({"fields": ["alt_mcap_bn", "alt_idx", "alt_oi_bn", "alt_oi_idx", "major_oi_bn", "alr_v1", "alr_v2"],
                   "rows": series}, f, separators=(",", ":"))
    print(json.dumps({k: rep[k] for k in ("first_day", "last_day", "contracts")}, indent=1))
    for name in ("v1", "v2"):
        r = rep[name]
        print(name, "alerts:", r["alerts"], "latest:", r["latest"])
        for k in ("eval", "eval_no_rally_rule", "eval_delay_1d"):
            e = r[k]["all"]
            print(f"  {k}: n={e['alerts']} kinds={e['kinds']} dd={e['median_dd']} up={e['median_up']} "
                  f"random={e['random_dd']}/{e['random_up']} control={e['control_dd']} p_dd={e.get('p_rot_dd')}")


if __name__ == "__main__":
    main()
