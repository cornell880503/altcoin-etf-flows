#!/usr/bin/env python3
"""The LIQ family of the indicator library: open copies of CoinKarma's order-book liquidity
indicators (docs.coinkarma.co/english/indicators-guide/liq, features-guide/resonance,
features-guide/token-screener, features-guide/triton-signal).

CoinKarma: LIQ is a standardized index of the ratio of bids to asks in a coin's spot order book,
hourly (buy > 3, sell < -3); Overall LIQ is its market-cap-weighted version over the tracked coins
(> 2 / < -2); Accumulated LIQ is the sum of the last 24 hourly values (+-60); 10% Orderbook DIF is
bids minus asks within 10% of the price (+-2000); 10% Overall Liquidity is the same over the whole
market (+-300M US$). A resonance is an hour in which a coin's LIQ >= 3 while Overall LIQ >= 2; the
first one starts a wave, coins join while Overall LIQ >= 2, none join while it is between 0 and 2,
and the wave ends for every coin when Overall LIQ falls below 0. A coin counts once it has 60 full
days of data.

Our inputs (proxies): Binance USD-M perpetual order-book depth within 1/2/5% of the price, the hourly
mean of its 30-second snapshots (data/ck/book, scripts/fetch_book_binance.py; Binance publishes no
spot depth history), hourly perp candles (data/ck/px1h) and daily market caps (Coin Metrics,
data/oi/cm). CoinKarma does not publish the standardization: ours is a rolling z-score of
log(bids / asks); the depth band and window are chosen by matching CoinKarma's published wave
statistics (calibrate()), and the choice is reported with the items.

    python3 scripts/ck_liq.py            print the calibration grid and the waves
"""
import copy
import csv
import datetime as dt
import glob
import gzip
import math
import os
import statistics
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oi_store as store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK = os.path.join(ROOT, "data", "ck", "book")
PX = os.path.join(ROOT, "data", "ck", "px1h")
CM = os.path.join(ROOT, "data", "oi", "cm")
# CoinKarma's resonance coins (23) and Triton's 24th (ZEC); TONUSDT became GRAMUSDT on Binance
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC", "BCH", "SUI", "HYPE", "UNI",
         "AAVE", "NEAR", "GRAM", "ATOM", "APT", "ARB", "OP", "ZEC"]
ALIAS = {"TON": "GRAM"}
DEPTHS = {1: (2, 3), 2: (4, 5), 5: (6, 7)}  # band -> (bid column, ask column) in the book rows
NEED_H = 60 * 24  # 60 full days before a coin counts
UTC8 = dt.timedelta(hours=8)
# CoinKarma's published statistics (Oct 2022 - Sep 2026, 23 coins) and example waves (UTC+8, candle close)
CK_STATS = {"per_month": 3.5, "median_days": 1.4, "short_share": 0.40, "long_share": 0.10, "median_coins": 11, "joined_24h": 9}
CK_WAVES = [
    {"start": "2026-09-12 03:00", "end": "2026-09-15 03:00", "coins": 7, "note": "CoinKarma 貼文"},
    {"start": "2026-09-15 10:00", "end": "2026-09-18 22:00", "coins": 17, "first": ["ARB", "ETH", "BCH", "SUI"], "note": "CoinKarma 文件的範例"},
    {"start": "2026-10-13", "hours": 6, "coins": 15, "avg": 4.7, "note": "CoinKarma 貼文"},
    {"start": "2026-10-14", "hours": 7 * 24 + 3, "coins": 19, "avg": 5.1, "up": 16, "note": "CoinKarma 貼文"},
]
EPOCH = dt.date(2020, 1, 1).toordinal()


def hidx(h):
    """'YYYY-MM-DDTHH' -> hours since 2020-01-01 00:00 UTC."""
    return (dt.date(int(h[:4]), int(h[5:7]), int(h[8:10])).toordinal() - EPOCH) * 24 + int(h[11:13])


def hstr(i):
    d = dt.date.fromordinal(EPOCH + i // 24)
    return f"{d.isoformat()}T{i % 24:02d}"


def ck_label(i):
    """Our hour (the candle that opens at i) as CoinKarma shows it: the candle's close, UTC+8."""
    t = dt.datetime.combine(dt.date.fromordinal(EPOCH + i // 24), dt.time(i % 24)) + dt.timedelta(hours=1) + UTC8
    return t.strftime("%Y-%m-%d %H:%M")


def short(label):
    """'2026-09-15 10:00' -> '9/15 10:00'."""
    if not label:
        return label
    y, m, d = label[:10].split("-")
    return f"{int(m)}/{int(d)}{label[10:]}"


def from_ck_label(s):
    t = dt.datetime.strptime(s, "%Y-%m-%d %H:%M") - UTC8 - dt.timedelta(hours=1)
    return (t.date().toordinal() - EPOCH) * 24 + t.hour


class Data:
    """Hourly grid: order-book depth, closes and market caps of the coins."""

    def __init__(self):
        rows = store.read_all(BOOK)
        self.ok = bool(rows)
        if not rows:
            return
        hs = [hidx(r[0]) for r in rows]
        self.h0, self.h1 = min(hs), max(hs)
        n = self.n = self.h1 - self.h0 + 1
        self.hours = np.arange(self.h0, self.h1 + 1)
        self.bid = {k: {c: np.full(n, np.nan) for c in COINS} for k in DEPTHS}
        self.ask = {k: {c: np.full(n, np.nan) for c in COINS} for k in DEPTHS}
        alias_rows = []
        for r, i in zip(rows, hs):
            c = ALIAS.get(r[1], r[1])
            if c not in COINS:
                continue
            if r[1] in ALIAS:
                alias_rows.append((r, i, c))
                continue
            self._put(r, i - self.h0, c)
        for r, i, c in alias_rows:  # the old symbol fills only the hours the new one does not have
            if np.isnan(self.bid[1][c][i - self.h0]):
                self._put(r, i - self.h0, c)
        self.first = {}
        for c in COINS:
            v = np.flatnonzero(~np.isnan(self.bid[5][c]))
            self.first[c] = int(v[0]) if len(v) else None
        # hourly closes
        self.close = {c: np.full(n, np.nan) for c in COINS}
        self.qvol = {c: np.full(n, np.nan) for c in COINS}
        self.tqvol = {c: np.full(n, np.nan) for c in COINS}
        px = store.read_all(PX)
        alias_px = []
        for r in px:
            c = ALIAS.get(r[1], r[1])
            if c not in COINS:
                continue
            i = hidx(r[0]) - self.h0
            if not 0 <= i < n:
                continue
            if r[1] in ALIAS:
                alias_px.append((r, i, c))
                continue
            self._px(r, i, c)
        for r, i, c in alias_px:
            if np.isnan(self.close[c][i]):
                self._px(r, i, c)
        # market caps (Coin Metrics, daily): the previous day's value for every hour of a day
        self.mcap = {c: np.full(n, np.nan) for c in COINS}
        day_cap = {}
        for r in store.read_all(CM):
            c = r[1].upper()
            if c in COINS and r[2]:
                day_cap.setdefault(c, {})[r[0]] = float(r[2])
        d0 = dt.date.fromordinal(EPOCH + self.h0 // 24)
        days = (self.h1 // 24) - (self.h0 // 24) + 1
        for c in COINS:
            caps = day_cap.get(c, {})
            last = None
            for k in range(-10, days):
                d = d0 + dt.timedelta(days=k)
                v = caps.get((d - dt.timedelta(days=1)).isoformat())
                if v:
                    last = v
                if k < 0 or last is None:
                    continue
                a = (d.toordinal() - EPOCH) * 24 - self.h0
                self.mcap[c][max(0, a):max(0, a + 24)] = last

    def _put(self, r, i, c):
        for k, (bi, ai) in DEPTHS.items():
            try:
                self.bid[k][c][i] = float(r[bi])
                self.ask[k][c][i] = float(r[ai])
            except (ValueError, IndexError):
                pass

    def _px(self, r, i, c):
        try:
            if float(r[3]) <= 0:  # no trading that hour (a delisted symbol keeps a frozen price): leave it empty
                return
            self.close[c][i] = float(r[2])
            self.qvol[c][i] = float(r[3])
            self.tqvol[c][i] = float(r[4])
        except (ValueError, IndexError):
            pass

    def label(self, i):
        """A grid index as CoinKarma shows the hour (candle close, UTC+8)."""
        return ck_label(int(i) + self.h0)

    def hour(self, i):
        return hstr(int(i) + self.h0)

    def eligible(self, c):
        """True for the hours a coin counts (60 full days after its first order-book hour)."""
        e = np.zeros(self.n, dtype=bool)
        if self.first.get(c) is not None:
            e[self.first[c] + NEED_H:] = True
        return e


def rolling_mean(x, w):
    """Mean of the valid values among the last w hours (inclusive); NaN where none."""
    v = ~np.isnan(x)
    c1 = np.concatenate([[0.0], np.cumsum(np.where(v, x, 0.0))])
    cn = np.concatenate([[0], np.cumsum(v)])
    i = np.arange(len(x))
    lo = np.maximum(0, i - w + 1)
    n = cn[i + 1] - cn[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, (c1[i + 1] - c1[lo]) / np.maximum(n, 1), np.nan)


def rolling_max(x, w):
    """Max of the valid values among the last w hours (inclusive), with a monotonic queue."""
    from collections import deque
    out = np.full(len(x), np.nan)
    q = deque()
    for i, v in enumerate(x):
        if not np.isnan(v):
            while q and x[q[-1]] <= v:
                q.pop()
            q.append(i)
        while q and q[0] <= i - w:
            q.popleft()
        if q:
            out[i] = x[q[0]]
    return out


def rolling_z(x, w, minp):
    v = ~np.isnan(x)
    x0 = np.where(v, x, 0.0)
    c1 = np.concatenate([[0.0], np.cumsum(x0)])
    c2 = np.concatenate([[0.0], np.cumsum(x0 * x0)])
    cn = np.concatenate([[0], np.cumsum(v)])
    i = np.arange(len(x))
    lo = np.maximum(0, i - w + 1)
    n = cn[i + 1] - cn[lo]
    nn = np.maximum(n, 1)
    m = (c1[i + 1] - c1[lo]) / nn
    var = np.maximum((c2[i + 1] - c2[lo]) / nn - m * m, 0.0)
    sd = np.sqrt(var)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (x - m) / sd
    z[(n < minp) | ~v | (sd <= 1e-12)] = np.nan
    return z


def rolling_sd(x, w, minp):
    v = ~np.isnan(x)
    x0 = np.where(v, x, 0.0)
    c1 = np.concatenate([[0.0], np.cumsum(x0)])
    c2 = np.concatenate([[0.0], np.cumsum(x0 * x0)])
    cn = np.concatenate([[0], np.cumsum(v)])
    i = np.arange(len(x))
    lo = np.maximum(0, i - w + 1)
    n = cn[i + 1] - cn[lo]
    nn = np.maximum(n, 1)
    m = (c1[i + 1] - c1[lo]) / nn
    sd = np.sqrt(np.maximum((c2[i + 1] - c2[lo]) / nn - m * m, 0.0))
    sd[(n < minp) | (sd <= 1e-12)] = np.nan
    return sd


def standardize(x, norm, window):
    """level: x over its trailing standard deviation, not centred (0 = as many bids as asks);
    z: the usual rolling z-score (centred on the trailing mean)."""
    minp = max(48, window // 2)
    if norm == "level":
        with np.errstate(invalid="ignore", divide="ignore"):
            z = x / rolling_sd(x, window, minp)
        z[~np.isfinite(z)] = np.nan
        return z
    return rolling_z(x, window, minp)


def compute(D, depth=5, window=720, smooth=1, mode="avg_z", norm="level"):
    """Coin LIQ (log(bids/asks), standardized) and Overall LIQ over the coins with 60 days of data, market-cap
    weighted: mode "avg_z" averages the coins' LIQ; "z_avg" standardizes the weighted average of their raw
    log(bids/asks) the same way as a coin's."""
    liq, elig, raw = {}, {}, {}
    for c in COINS:
        b, a = D.bid[depth][c], D.ask[depth][c]
        with np.errstate(invalid="ignore", divide="ignore"):
            x = np.log(b / a)
        x[~np.isfinite(x)] = np.nan
        if smooth > 1:
            x = np.where(np.isnan(x), np.nan, rolling_mean(x, smooth))
        raw[c] = x
        liq[c] = standardize(x, norm, window)
        elig[c] = D.eligible(c) & ~np.isnan(liq[c])
    num, den = np.zeros(D.n), np.zeros(D.n)
    for c in COINS:
        w = D.mcap[c]
        ok = elig[c] & ~np.isnan(w)
        val = liq[c] if mode == "avg_z" else raw[c]
        num += np.where(ok, w * np.nan_to_num(val), 0.0)
        den += np.where(ok, w, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        overall = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)
    if mode != "avg_z":
        overall = standardize(overall, norm, window)
    sd = {c: rolling_sd(raw[c], window, max(48, window // 2)) for c in COINS}
    return {"liq": liq, "elig": elig, "overall": overall, "raw": raw, "sd": sd, "depth": depth, "window": window, "smooth": smooth,
            "mode": mode, "norm": norm}


def waves(D, R):
    """CoinKarma's resonance waves on our LIQ. Hours are grid indices."""
    liq, elig, O = R["liq"], R["elig"], R["overall"]
    out, cur = [], None
    for i in range(D.n):
        o = O[i]
        if np.isnan(o):
            continue
        if cur is not None and o < 0:
            cur["end"] = i
            out.append(cur)
            cur = None
            continue
        if o >= 2:
            res = sorted(((liq[c][i], c) for c in COINS if elig[c][i] and liq[c][i] >= 3
                          and (cur is None or c not in cur["join"])), reverse=True)
            if res:
                if cur is None:
                    cur = {"start": i, "join": {}, "order": []}
                for z, c in res:
                    cur["join"][c] = i
                    cur["order"].append(c)
    if cur is not None:
        cur["end"] = None  # still going
        out.append(cur)
    for w in out:
        end = w["end"] if w["end"] is not None else D.n - 1
        w["hours"] = end - w["start"]
        rets = {}
        for c, j in w["join"].items():
            p0, p1 = D.close[c][j], D.close[c][end]
            if p0 > 0 and p1 > 0:
                rets[c] = float(p1 / p0 - 1)
        w["ret"] = rets
    return out


def wave_stats(D, W, lo=None, hi=None):
    """CoinKarma's wave statistics over the finished waves that start in [lo, hi) (grid indices)."""
    ws = [w for w in W if w["end"] is not None and (lo is None or w["start"] >= lo) and (hi is None or w["start"] < hi)]
    if not ws:
        return None
    span_h = ((hi if hi is not None else D.n) - (lo if lo is not None else 0))
    months = span_h / 24 / 30.44
    hours = [w["hours"] for w in ws]
    coins = [len(w["join"]) for w in ws]
    j24 = [sum(1 for j in w["join"].values() if j - w["start"] < 24) for w in ws]
    avg = [statistics.mean(w["ret"].values()) for w in ws if w["ret"]]
    return {"n": len(ws), "per_month": len(ws) / months if months > 0 else None, "median_days": float(statistics.median(hours)) / 24,
            "short_share": sum(h < 24 for h in hours) / len(ws), "long_share": sum(h > 168 for h in hours) / len(ws),
            "median_coins": float(statistics.median(coins)), "joined_24h": float(statistics.median(j24)),
            "avg_ret": float(statistics.mean(avg)) if avg else None, "median_ret": float(statistics.median(avg)) if avg else None,
            "up_share": sum(a > 0 for a in avg) / len(avg) if avg else None}


def stats_score(s):
    if not s or not s["per_month"]:
        return 99.0
    t = CK_STATS
    return (abs(math.log(s["per_month"] / t["per_month"])) + abs(math.log(max(s["median_days"], 0.01) / t["median_days"]))
            + 2 * abs(s["short_share"] - t["short_share"]) + 3 * abs(s["long_share"] - t["long_share"])
            + abs(s["median_coins"] - t["median_coins"]) / t["median_coins"] + abs(s["joined_24h"] - t["joined_24h"]) / t["joined_24h"])


def match_ck(D, W):
    """Each of CoinKarma's example waves against ours: the wave of ours that overlaps it most."""
    out = []
    for ck in CK_WAVES:
        if " " in ck["start"]:
            s = from_ck_label(ck["start"]) - D.h0
            e = from_ck_label(ck["end"]) - D.h0 if ck.get("end") else s + ck["hours"]
        else:  # a day only (UTC+8): the wave that starts that day
            d0 = from_ck_label(ck["start"] + " 00:00") - D.h0
            cands = [w for w in W if d0 <= w["start"] < d0 + 24]
            s = cands[0]["start"] if cands else d0
            e = s + ck["hours"]
        if s >= D.n or e < 0:
            out.append({**ck, "ours": None})
            continue
        best, ov = None, 0
        for w in W:
            we = w["end"] if w["end"] is not None else D.n - 1
            o = min(e, we) - max(s, w["start"])
            if o > ov:
                best, ov = w, o
        if best is None:
            out.append({**ck, "ours": None})
            continue
        we = best["end"] if best["end"] is not None else D.n - 1
        out.append({**ck, "ours": {"start": D.label(best["start"]), "end": D.label(we) if best["end"] is not None else None,
                                   "hours": int(best["hours"]), "coins": len(best["join"]), "first": best["order"][:4],
                                   "avg": float(statistics.mean(best["ret"].values()) * 100) if best["ret"] else None,
                                   "up": int(sum(r > 0 for r in best["ret"].values()))}})
    return out


def calibrate(D, grid=None, lo=None):
    """Standardization x depth band x window x Overall mode: how close our waves come to CoinKarma's published
    statistics, and how many of their example waves we also have (with at least 5 coins)."""
    grid = grid or [(nm, d, w, 1, m) for nm in ("level", "z") for d in (1, 2, 5) for w in (168, 360, 720, 1440) for m in ("avg_z", "z_avg")]
    if lo is None:
        lo = D.first["BTC"] + NEED_H if D.first.get("BTC") is not None else 0
    out = []
    for nm, d, w, s, m in grid:
        R = compute(D, d, w, s, m, nm)
        W = waves(D, R)
        st = wave_stats(D, W, lo=lo)
        ex = sum(1 for x in match_ck(D, W) if x["ours"] and x["ours"]["coins"] >= 5)
        out.append({"norm": nm, "depth": d, "window": w, "smooth": s, "mode": m, "score": stats_score(st), "examples": ex, "stats": st})
    out.sort(key=lambda r: r["score"])
    return out


# ------------------------------------------------------------------ live hours (after Binance's last published hour)
LIVE = os.path.join(ROOT, "data", "ck", "live", "hours")
LIVE_VENUES = {"okx": "OKX 永續", "cb": "Coinbase 現貨", "hl": "Hyperliquid"}
FIT_MIN, FIT_FULL = 6, 48  # hours both have: an offset-only map from 6, a linear map from 48
LIVE_PENDING = ("即時估計：每小時另外抓 OKX、Coinbase、Hyperliquid 的訂單簿；Binance 的檔案隔天才公布，兩者重疊滿 6 小時、"
                "完成校準後，這裡會接上到最近一小時的估計值。")


def load_live():
    """Live books (scripts/fetch_book_live.py): {(venue, coin): {hour: log(bidR / askR)}}, {coin: {hour: close}}."""
    feats, px = {}, {}
    for p in sorted(glob.glob(os.path.join(LIVE, "*.csv.gz"))):
        with gzip.open(p, "rt", newline="") as f:
            rd = csv.reader(f)
            next(rd, None)
            for row in rd:
                if len(row) < 4 or ":" not in row[1]:
                    continue
                v, c = row[1].split(":", 1)
                c = ALIAS.get(c, c)
                h = hidx(row[0])
                if v == "px":
                    try:
                        px.setdefault(c, {})[h] = float(row[3])
                    except ValueError:
                        pass
                    continue
                try:
                    b, a = float(row[9]), float(row[10])
                except (ValueError, IndexError):
                    continue
                if b > 0 and a > 0:
                    feats.setdefault((v, c), {})[h] = math.log(b / a)
    return feats, px


def live_extend(D, R, now=None):
    """Extend LIQ past Binance's last published hour T with the live books: per coin, the venue whose
    imbalance best tracks Binance's on the hours both have is mapped onto Binance's (x = a + b * live;
    an offset only until FIT_FULL hours), divided by the coin's trailing standard deviation at T.
    Returns None when there is nothing to add."""
    feats, px = load_live()
    if not feats:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    done = (now.date().toordinal() - EPOCH) * 24 + now.hour - 1  # the last complete hour
    Tabs = D.h0 + D.n - 1
    L = min(done, max(max(f) for f in feats.values()))
    fits = {}
    k = max(0, L - Tabs)
    liq_live = {}
    for c in COINS:
        x, sd = R["raw"][c], R["sd"][c]
        fin = np.flatnonzero(np.isfinite(sd))
        sdv = float(sd[fin[-1]]) if len(fin) else None
        best = None
        for v in LIVE_VENUES:
            f = feats.get((v, c))
            if not f:
                continue
            pairs = [(x[h - D.h0], fv) for h, fv in f.items() if D.h0 <= h <= Tabs and np.isfinite(x[h - D.h0])]
            m = len(pairs)
            if m < FIT_MIN:
                continue
            xs, fs = np.array([q[0] for q in pairs]), np.array([q[1] for q in pairs])
            if m >= FIT_FULL and fs.std() > 1e-9:
                b, a = (float(t) for t in np.polyfit(fs, xs, 1))
                kind = "線性"
            else:
                b, a = 1.0, float(np.mean(xs - fs))
                kind = "只校正偏移"
            ss = float(((xs - xs.mean()) ** 2).sum())
            r2 = 1 - float(((xs - (a + b * fs)) ** 2).sum()) / ss if ss > 0 else None
            live_n = sum(1 for h in f if Tabs < h <= L)
            rank = (kind == "線性", -9.0 if r2 is None else r2, m)
            if live_n and (best is None or rank > best[0]):
                best = (rank, v, kind, a, b, r2, m)
        if best is None or not sdv:
            continue
        _, v, kind, a, b, r2, m = best
        arr = np.full(k, np.nan)
        for h, fv in feats[(v, c)].items():
            if Tabs < h <= L:
                arr[h - Tabs - 1] = (a + b * fv) / sdv
        liq_live[c] = arr
        fits[c] = {"venue": v, "kind": kind, "a": a, "b": b, "r2": r2, "overlap": m}
    if k == 0 or "BTC" not in liq_live:
        return {"fits": fits, "k": 0} if fits else None
    # Overall LIQ: the last market caps; at least half of the counted market cap must have a live value
    elig_T = {c: bool(D.eligible(c)[-1]) for c in COINS}
    w = {}
    for c in COINS:
        fin = np.flatnonzero(np.isfinite(D.mcap[c]))
        if elig_T[c] and len(fin):
            w[c] = float(D.mcap[c][fin[-1]])
    total = sum(w.values())
    num, den = np.zeros(k), np.zeros(k)
    for c, arr in liq_live.items():
        if c in w:
            ok = np.isfinite(arr)
            num += np.where(ok, w[c] * np.nan_to_num(arr), 0.0)
            den += np.where(ok, w[c], 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        o_live = np.where(den >= 0.5 * total, num / np.where(den > 0, den, 1), np.nan)
    E = copy.copy(D)
    E.n = D.n + k
    E.close = {}
    for c in COINS:
        lp = np.full(k, np.nan)
        for h, v in px.get(c, {}).items():
            if Tabs < h <= L:
                lp[h - Tabs - 1] = v
        E.close[c] = np.concatenate([D.close[c], lp])
    X = {"liq": {c: np.concatenate([R["liq"][c], liq_live.get(c, np.full(k, np.nan))]) for c in COINS},
         "elig": {c: np.concatenate([R["elig"][c], np.full(k, elig_T[c] and c in w) & np.isfinite(liq_live.get(c, np.full(k, np.nan)))])
                  for c in COINS},
         "overall": np.concatenate([R["overall"], o_live])}
    return {"E": E, "R": X, "fits": fits, "k": k, "cover": float(np.nanmin(den / total)) if total else 0.0}


def wave_view(D, W, O, r0, live_from=None):
    """What the page shows of the waves: shaded spans, the latest waves, and the state now."""
    n = D.n
    spans = [{"from": D.hour(max(w["start"], r0)), "to": D.hour(w["end"] if w["end"] is not None else n - 1)}
             for w in W if (w["end"] is None or w["end"] >= r0)]
    recent = []
    for w in reversed(W[-12:]):
        end = w["end"]
        avg = statistics.mean(w["ret"].values()) * 100 if w["ret"] else None
        live = live_from is not None and (w["start"] >= live_from or (end is None and max(w["join"].values()) >= live_from))
        recent.append([short(D.label(w["start"])) + ("（即時）" if live_from is not None and w["start"] >= live_from else ""),
                       short(D.label(end)) if end is not None else "進行中", f"{w['hours'] // 24} 天 {w['hours'] % 24} 小時",
                       len(w["join"]), "、".join(w["order"][:6]) + ("…" if len(w["order"]) > 6 else "") + ("（含即時加入）" if live and w["start"] < live_from else ""),
                       f"{avg:+.1f}%" if avg is not None else "—"])
    cur = W[-1] if W and W[-1]["end"] is None else None
    last = n - 1
    while last > 0 and np.isnan(O[last]):
        last -= 1
    o_now = O[last]
    if cur:
        state = f"波段進行中：{len(cur['join'])} 個幣（{'可加入' if o_now >= 2 else '延續中、不再加入'}）"
    else:
        state = "等待中（沒有波段）"
    return spans, recent, last, o_now, state, cur


def apply_live(out, D, R, lo, ext, now=None):
    """Put the live hours into the LIQ and resonance cards (their statistics stay on Binance's data); returns the
    shared hourly BTC closes over the extended window, or None."""
    if not ext:
        return None
    fits = ext.get("fits") or {}
    rows = []
    for c in COINS:
        f = fits.get(c)
        rows.append([c, LIVE_VENUES[f["venue"]] if f else "—", f["kind"] if f else "重疊不足", f["overlap"] if f else 0,
                     f"{f['r2']:.2f}" if f and f["r2"] is not None else "—"])
    fit_table = {"title": "即時估計的校準（每個幣選最貼近 Binance 的一家）", "head": ["幣", "即時資料", "方式", "重疊小時", "R²"], "rows": rows, "text": [1, 2]}
    if not ext.get("k"):
        for it in out:
            if it["id"] == "liq":
                it["tables"] = [fit_table]
        return None
    E, X = ext["E"], ext["R"]
    T = D.n - 1
    n_e = E.n
    r0 = max(lo, n_e - RECENT_H)
    r0c = max(lo, D.n - RECENT_H)
    hrs = [E.hour(i) for i in range(r0, n_e)]
    live_from = T + 1
    O, B = X["overall"], X["liq"]["BTC"]
    note = (f"最後 {n_e - 1 - T} 小時（{short(E.label(live_from))} 起，台灣時間）是即時估計：Binance 的訂單簿檔案隔天才公布，"
            "這段改用 OKX、Coinbase、Hyperliquid 每小時抓的訂單簿，換算成 Binance 的尺度（校準見下表）；Binance 檔案到了就換回正式值。")
    for it in out:
        if it["id"] == "liq":
            with np.errstate(invalid="ignore"):
                green, red = (O > 2) & (B > 3), (O < -2) & (B < -3)
            known = np.isfinite(O) & np.isfinite(B)
            g_ev, r_ev = first_hours(green, 24, known, lo), first_hours(red, 24, known, lo)
            last = n_e - 1
            while last > T and not np.isfinite(O[last]):
                last -= 1
            ov, bv = O[last], B[last]
            st = "綠色箭頭" if green[last] else "紅色箭頭" if red[last] else "偏多" if ov > 2 else "偏空" if ov < -2 else "中性"
            it["series"] = {"h0": hrs[0], "n": len(hrs), "v": [_r(v, 2) for v in O[r0:n_e]], "b": [_r(v, 2) for v in B[r0:n_e]],
                            "liveFrom": E.hour(live_from)}
            it["marks"] = ([{"d": E.hour(i), "label": "綠色箭頭" + ("（即時）" if i >= live_from else ""), "tone": "good"} for i in g_ev if i >= r0]
                           + [{"d": E.hour(i), "label": "紅色箭頭" + ("（即時）" if i >= live_from else ""), "tone": "bad"} for i in r_ev if i >= r0])
            live_txt = "（即時估計）" if last > T else ""
            it["now"] = {"d": E.hour(last), "v": _r(ov, 2), "state": f"{st}，BTC LIQ {bv:+.1f}{live_txt}" if np.isfinite(bv) else st + live_txt,
                         "tone": "good" if ov > 2 else "bad" if ov < -2 else "mid"}
            it["chartNote"] = CHART_NOTE + " " + note
            it["tables"] = [fit_table]
        elif it["id"] == "resonance":
            W = waves(E, X)
            spans, recent, last, o_now, state, cur = wave_view(E, W, O, r0, live_from)
            it["series"] = {"h0": hrs[0], "n": len(hrs), "v": [_r(v, 2) for v in O[r0:n_e]], "liveFrom": E.hour(live_from)}
            it["spans"] = spans
            live_txt = "（即時估計）" if last > T else ""
            it["now"] = {"d": E.hour(last), "v": _r(o_now, 2), "state": state + live_txt, "tone": "good" if cur else "mid"}
            it["chartNote"] = CHART_NOTE + " " + note
            for t in it.get("tables", []):
                if t["title"] == "最近的波段":
                    t["rows"] = recent
            if cur is not None:
                it["live_wave"] = {"start": E.label(cur["start"]), "coins": cur["order"],
                                   "ret": {c: _r(r * 100, 1) for c, r in cur["ret"].items()}}
    return {"h0": E.hour(r0c), "v": [_r(v, 0) for v in E.close["BTC"][r0c:n_e]]}


def patch_live(ck, item):
    """For the site build: the live hours into an indicators.json computed earlier (only the LIQ and
    resonance cards and the shared BTC closes change)."""
    def pending():
        for it in ck.get("items", []):
            if it.get("id") in ("liq", "resonance") and "即時估計：" not in (it.get("chartNote") or ""):
                it["chartNote"] = CHART_NOTE + " " + LIVE_PENDING
        return ck

    if not glob.glob(os.path.join(LIVE, "*.csv.gz")):
        return pending()
    D = Data()
    if not D.ok or D.first.get("BTC") is None:
        return ck
    R = compute(D, **PARAMS)
    ext = live_extend(D, R)
    if not ext:
        return pending()
    lo = D.first["BTC"] + NEED_H
    hb = apply_live(ck.get("items", []), D, R, lo, ext)
    if hb:
        ck["hbtc"] = hb
    elif not ext.get("k"):
        pending()
    return ck


# ------------------------------------------------------------------ the page's items
PARAMS = {"depth": 5, "window": 720, "smooth": 1, "mode": "avg_z", "norm": "level"}  # chosen with calibrate() (research/ck_liq_calibration.json)
RECENT_H = 60 * 24  # the charts show the last 60 days, hourly
CHART_NOTE = "圖是最近 60 天、每小時（台灣時間、K 線收盤），不受「全部／近一年」切換影響；回測用 2023 年起的全部資料。"
H_FWD = 72  # the signals are for range trading: what BTC (or the coins) did over the next 72 hours


def _r(v, k=2):
    if v is None:
        return None
    v = float(v)
    return round(v, k) if math.isfinite(v) else None


def to_daily(D, arr, how="mean", min_n=12):
    """{UTC date: the day's mean (or max) of an hourly series}, days with at least min_n values."""
    off = (24 - D.h0 % 24) % 24
    k = (D.n - off) // 24
    if k <= 0:
        return {}
    m = arr[off:off + k * 24].reshape(k, 24)
    cnt = (~np.isnan(m)).sum(axis=1)
    with np.errstate(invalid="ignore"), __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore")
        v = np.nanmean(m, axis=1) if how == "mean" else np.nanmax(m, axis=1)
    d0 = (D.h0 + off) // 24
    return {dt.date.fromordinal(EPOCH + d0 + j).isoformat(): float(v[j]) for j in range(k) if cnt[j] >= min_n}


def first_hours(flag, gap=24, valid=None, lo=0):
    """Signal hours (from grid index lo on): the condition holds after at least `gap` hours with data in which it
    did not (hours without data are skipped, not counted as "no signal")."""
    flag = np.asarray(flag, dtype=bool)
    valid = np.ones(len(flag), dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    seen = np.cumsum(valid)  # hours with data so far
    out, last = [], None
    for i in np.flatnonzero(flag & valid):
        if i < lo:
            last = int(seen[i])
            continue
        if last is None or seen[i] - last > gap:
            out.append(int(i))
        last = int(seen[i])
    return out


def fwd_h(px, i, H):
    if i + H >= len(px) or not px[i] > 0:
        return None
    path = px[i + 1:i + H + 1]
    if np.isnan(path).any():
        return None
    p0 = px[i]
    return (path[-1] / p0 - 1, min(0.0, path.min() / p0 - 1), max(0.0, path.max() / p0 - 1))


def ev_stats_h(px, events, H, lo, label=None):
    ev = [o for o in (fwd_h(px, i, H) for i in events) if o]
    base = [o for o in (fwd_h(px, i, H) for i in range(max(lo, 0), len(px) - H, 3)) if o]
    if not ev or not base:
        return None
    med = lambda xs: float(np.median(xs)) * 100  # noqa: E731
    out = {"n": len(ev), "h": H, "unit": "h", "hl": f"{H // 24} 天" if H % 24 == 0 and H >= 120 else f"{H} 小時", "ret": _r(med([o[0] for o in ev]), 1), "up_share": _r(sum(o[0] > 0 for o in ev) / len(ev) * 100, 0),
           "dd": _r(med([o[1] for o in ev]), 1), "upmax": _r(med([o[2] for o in ev]), 1),
           "base_ret": _r(med([o[0] for o in base]), 1), "base_up_share": _r(sum(o[0] > 0 for o in base) / len(base) * 100, 0),
           "base_dd": _r(med([o[1] for o in base]), 1), "base_upmax": _r(med([o[2] for o in base]), 1)}
    if label:
        out["label"] = label
    return out


def rsi(px, n=14):
    """Wilder's RSI of hourly closes (NaN until n changes are seen; gaps restart nothing, they are skipped)."""
    out = np.full(len(px), np.nan)
    ag = al = None
    prev = None
    k = 0
    for i, p in enumerate(px):
        if np.isnan(p):
            continue
        if prev is not None:
            ch = p - prev
            g, lo = max(ch, 0.0), max(-ch, 0.0)
            k += 1
            if ag is None:
                ag, al = g, lo
            else:
                ag = (ag * (n - 1) + g) / n
                al = (al * (n - 1) + lo) / n
            if k >= n:
                out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        prev = p
    return out


def items(item, D=None, ctx=None):
    """The LIQ family as indicator-library items (scripts/ck_indicators.py's item()), and BTC's hourly
    closes over the charts' window (shared by the hourly charts)."""
    D = D or Data()
    if not D.ok or D.first.get("BTC") is None:
        return [], None
    R = compute(D, **PARAMS)
    O, L = R["overall"], R["liq"]
    btc = D.close["BTC"]
    lo = D.first["BTC"] + NEED_H  # the first hour every number is defined for BTC
    n = D.n
    r0 = max(lo, n - RECENT_H)
    hrs = [D.hour(i) for i in range(r0, n)]
    last = n - 1
    while last > 0 and np.isnan(O[last]):
        last -= 1
    cut = lambda a, k=3: [_r(v, k) for v in a[r0:n]]  # noqa: E731
    span = f"{D.hour(lo)[:10]} 起"
    how = (f"Binance 永續合約 ±{PARAMS['depth']}% 內的買單與賣單金額（每小時平均），取 log(買 ÷ 賣)，"
           f"除以它過去 {PARAMS['window'] // 24} 天的標準差（不扣平均：0 = 買賣一樣厚）。")
    diff_common = ("CoinKarma 用現貨訂單簿，標準化方式沒有公開；Binance 沒有現貨深度的歷史，只能用合約的 ±1–5% 深度（2023 年起）。"
                   "我們試了兩種標準化（扣不扣平均）、三個深度、四個視窗，選最接近 CoinKarma 公布的共振統計與範例波段的一組"
                   "（research/ck_liq_calibration.json）；只看波段的樣子，沒有看報酬。")
    out = []
    # 1) Overall LIQ and BTC LIQ; CoinKarma's arrows: Overall > 2 and BTC LIQ > 3 (green), < -2 and < -3 (red)
    B = L["BTC"]
    with np.errstate(invalid="ignore"):
        green = (O > 2) & (B > 3)
        red = (O < -2) & (B < -3)
    known = ~np.isnan(O) & ~np.isnan(B)
    g_ev, r_ev = first_hours(green, 24, known, lo), first_hours(red, 24, known, lo)
    st = [x for x in (ev_stats_h(btc, g_ev, H_FWD, lo, "綠色箭頭（Overall > 2 且 BTC LIQ > 3）"),
                      ev_stats_h(btc, r_ev, H_FWD, lo, "紅色箭頭（Overall < −2 且 BTC LIQ < −3）")) if x]
    ov = O[last]
    state = "綠色箭頭" if green[last] else "紅色箭頭" if red[last] else "偏多" if ov > 2 else "偏空" if ov < -2 else "中性"
    out.append(item(id="liq", group="訂單簿流動性", name="Overall LIQ / LIQ", zh="LIQ 與 Overall LIQ（訂單簿買賣力道）", fidelity="proxy", res="h", chartNote=CHART_NOTE,
                    what="訂單簿上買單相對賣單的厚度，標準化成分數：正值代表買盤掛得比平常厚。Overall LIQ 是 24 個主流幣依市值加權。",
                    ck="LIQ > 3 偏多、< −3 偏空；Overall LIQ > 2 偏多、< −2 偏空。Overall > 2 且 BTC LIQ > 3 是綠色箭頭（分批做多），< −2 且 < −3 是紅色箭頭（較快了結）。適合震盪盤，單邊趨勢中會鈍化。",
                    ours=how + f"Overall LIQ 用 Coin Metrics 的市值加權，幣要有 60 天資料才計入。訊號取每次的第一個小時（前 24 小時沒有），看之後 {H_FWD} 小時 BTC 的表現（{span}）。",
                    diff=diff_common, unit="分", dec=2,
                    lines=[{"k": "v", "label": "Overall LIQ"}, {"k": "b", "label": "BTC LIQ"}],
                    series={"h0": hrs[0], "n": len(hrs), "v": cut(O, 2), "b": cut(B, 2)},
                    zones=[{"from": 2, "to": None, "tone": "good", "label": "> 2"}, {"from": None, "to": -2, "tone": "bad", "label": "< −2"}],
                    marks=[{"d": D.hour(i), "label": "綠色箭頭", "tone": "good"} for i in g_ev if i >= r0]
                          + [{"d": D.hour(i), "label": "紅色箭頭", "tone": "bad"} for i in r_ev if i >= r0],
                    now={"d": D.hour(last), "v": _r(ov, 2), "state": f"{state}，BTC LIQ {B[last]:+.1f}" if not np.isnan(B[last]) else state,
                         "tone": "good" if ov > 2 else "bad" if ov < -2 else "mid"},
                    stats=st, statsKind="labeled", baseLabel="隨機一小時", src="Binance 合約公開資料（bookDepth、K 線）、Coin Metrics 市值"))
    daily = (ctx or {}).get("daily")
    if daily is not None:
        daily["liq"] = to_daily(D, O)
    # 2) Accumulated LIQ (BTC): the sum of the last 24 hourly values, +-60
    acc = np.full(n, np.nan)
    vb = ~np.isnan(B)
    c1 = np.concatenate([[0.0], np.cumsum(np.where(vb, B, 0.0))])
    cn = np.concatenate([[0], np.cumsum(vb)])
    for i in range(23, n):
        if cn[i + 1] - cn[i - 23] >= 20 and vb[i]:
            acc[i] = c1[i + 1] - c1[i - 23]
    with np.errstate(invalid="ignore"):
        a_up, a_dn = first_hours(acc >= 60, 24, ~np.isnan(acc), lo), first_hours(acc <= -60, 24, ~np.isnan(acc), lo)
    st = [x for x in (ev_stats_h(btc, a_up, H_FWD, lo, "≥ +60"), ev_stats_h(btc, a_dn, H_FWD, lo, "≤ −60")) if x]
    av = acc[last]
    if daily is not None:
        daily["liqacc"] = to_daily(D, acc)
    out.append(item(id="liqacc", group="訂單簿流動性", name="Accumulated LIQ", zh="累積 LIQ（BTC，24 小時加總）", fidelity="proxy", res="h", chartNote=CHART_NOTE,
                    what="BTC 的 LIQ 過去 24 小時加總，看買賣力道是否持續偏向一邊。",
                    ck="預設 (24, 60, −60)：加總過去 24 個 1 小時 LIQ，> 60 偏多、< −60 偏空（4 小時圖用 50 根、±100）。",
                    ours="上面的 BTC LIQ，加總過去 24 小時（至少 20 小時有資料）。訊號取第一次越過 ±60 的小時（前 24 小時沒有）。",
                    diff=diff_common, unit="分", dec=1,
                    lines=[{"k": "v", "label": "累積 LIQ（24 小時）"}],
                    series={"h0": hrs[0], "n": len(hrs), "v": cut(acc, 1)},
                    zones=[{"from": 60, "to": None, "tone": "good", "label": "≥ 60"}, {"from": None, "to": -60, "tone": "bad", "label": "≤ −60"}],
                    marks=[{"d": D.hour(i), "label": "≥ +60", "tone": "good"} for i in a_up if i >= r0]
                          + [{"d": D.hour(i), "label": "≤ −60", "tone": "bad"} for i in a_dn if i >= r0],
                    now={"d": D.hour(last), "v": _r(av, 1), "state": "偏多" if av >= 60 else "偏空" if av <= -60 else "中性",
                         "tone": "good" if av >= 60 else "bad" if av <= -60 else "mid"},
                    stats=st, statsKind="labeled", baseLabel="隨機一小時", src="Binance 合約公開資料（bookDepth）"))
    # 3) order-book difference, BTC, in BTC; 4) the same over all coins, in US$ (CoinKarma: within 10%; ours: 5%)
    with np.errstate(invalid="ignore", divide="ignore"):
        dif = (D.bid[5]["BTC"] - D.ask[5]["BTC"]) / btc
    tot = np.zeros(n)
    cnt = np.zeros(n)
    for c in COINS:
        d5 = D.bid[5][c] - D.ask[5][c]
        ok = R["elig"][c] & ~np.isnan(d5)
        tot += np.where(ok, d5, 0.0)
        cnt += ok
    tot = np.where(cnt >= 10, tot / 1e6, np.nan)
    for key, arr, zh, unit, thr, ckt, src_k in (
            ("liqdif", dif, "5% 掛單差（BTC，以顆計）", "BTC", 2000, "10% Orderbook DIF：單一幣 ±10% 內買單減賣單，> 2000 偏多、< −2000 偏空（可切換成美元）。", "BTC"),
            ("liqtotal", tot, "5% 全市場掛單差（美元）", "百萬美元", 300, "10% Overall Liquidity：全市場訂單簿 ±10% 內買單減賣單，> 3 億美元偏多、< −3 億偏空（或 ±3000 BTC）。", "全部")):
        with np.errstate(invalid="ignore"):
            up_e, dn_e = first_hours(arr >= thr, 24, ~np.isnan(arr), lo), first_hours(arr <= -thr, 24, ~np.isnan(arr), lo)
        st = [x for x in (ev_stats_h(btc, up_e, H_FWD, lo, f"≥ +{thr:,}"), ev_stats_h(btc, dn_e, H_FWD, lo, f"≤ −{thr:,}")) if x]
        v = arr[last]
        valid = arr[lo:][~np.isnan(arr[lo:])]
        p_hi = float(np.mean(valid >= thr) * 100) if len(valid) else 0
        p_lo = float(np.mean(valid <= -thr) * 100) if len(valid) else 0
        out.append(item(id=key, group="訂單簿流動性", name="10% Orderbook DIF" if key == "liqdif" else "10% Overall Liquidity", zh=zh, fidelity="proxy", res="h", chartNote=CHART_NOTE,
                        what=("BTC 訂單簿上、價格 ±5% 內的買單減賣單（換算成顆數）。" if key == "liqdif" else
                              "24 個主流幣的訂單簿、價格 ±5% 內的買單減賣單，加總成美元。"),
                        ck=ckt, ours=("Binance 永續合約 ±5% 深度（每小時平均）；" + ("除以 BTC 價格換成顆數。" if key == "liqdif" else "有 60 天資料的幣才加總，至少 10 個幣。")
                                      + f"門檻沿用 CoinKarma 的數字；過去 {p_hi:.0f}% 的小時在上門檻之上、{p_lo:.0f}% 在下門檻之下。"),
                        diff="CoinKarma 是 ±10% 的現貨訂單簿，Binance 公開資料只到 ±5%，所以數字會比他們小，門檻只能參考。",
                        unit=unit, dec=0, lines=[{"k": "v", "label": zh, "bar": True}],
                        series={"h0": hrs[0], "n": len(hrs), "v": cut(arr, 0)},
                        zones=[{"from": thr, "to": None, "tone": "good", "label": f"≥ {thr:,}"}, {"from": None, "to": -thr, "tone": "bad", "label": f"≤ −{thr:,}"}],
                        now={"d": D.hour(last), "v": _r(v, 0), "state": "偏多" if v >= thr else "偏空" if v <= -thr else "中性",
                             "tone": "good" if v >= thr else "bad" if v <= -thr else "mid"},
                        stats=st, statsKind="labeled", baseLabel="隨機一小時", src="Binance 合約公開資料（bookDepth）"))
    # CVD: BTC perpetual taker buys minus taker sells (US$), cumulated over the chart window
    tq, qv = D.tqvol["BTC"], D.qvol["BTC"]
    delta = 2 * tq - qv  # taker buy - taker sell
    dw = np.where(np.isnan(delta), 0.0, delta)
    cvd = np.full(n, np.nan)
    cvd[r0:] = np.cumsum(dw[r0:]) / 1e6
    cvd[np.isnan(delta)] = np.nan
    d24 = np.full(n, np.nan)
    c1d = np.concatenate([[0.0], np.cumsum(dw)])
    d24[24:] = (c1d[25:] - c1d[1:-24]) / 1e6
    lc = n - 1
    while lc > 0 and np.isnan(delta[lc]):
        lc -= 1
    v24 = d24[lc]
    out.append(item(id="cvd", group="訂單簿流動性", name="CVD (Cumulative Volume Delta)", zh="CVD（主動買減主動賣，BTC）", fidelity="proxy", res="h", chartNote=CHART_NOTE,
                    what="市價單的力道：主動買進的成交額減主動賣出的成交額，累加起來。和 LIQ（掛單）互補。",
                    ck="市價單持續往一邊推時，會吃掉掛單、讓 LIQ 看起來過度延伸；例如 Overall LIQ 一直紅、價格卻一直創高時，CVD 顯示市價買盤很強，這時不要照 LIQ 去放空。沒有數字門檻。",
                    ours="Binance BTCUSDT 永續合約每小時 K 線的主動買進成交額 × 2 − 總成交額，從圖的起點累加（百萬美元）；目前值是最近 24 小時的合計。",
                    diff="CoinKarma 沒寫用現貨還是合約、哪些交易所；這裡只有 Binance 合約。", unit="百萬美元（24 小時）", dec=0,
                    lines=[{"k": "v", "label": "CVD，60 天累加（百萬美元）", "bar": True}],
                    series={"h0": hrs[0], "n": len(hrs), "v": cut(cvd, 0)},
                    now={"d": D.hour(lc), "v": _r(v24, 0), "state": "市價買盤較強" if v24 > 0 else "市價賣盤較強", "tone": "good" if v24 > 0 else "bad"},
                    src="Binance 合約 K 線（data.binance.vision）"))
    # 5) resonance waves
    W = waves(D, R)
    out.append(resonance_item(item, D, R, W, lo, r0, hrs, diff_common))
    # 6) token screener
    out.append(screener_item(item, D, R, lo, diff_common))
    # 7) Triton backtest; 8) ARI (ours)
    out.append(triton_item(item, D, R, W, lo, ctx or {}))
    a_item, ari = ari_item(item, D, R, lo, r0, hrs)
    out.append(a_item)
    if daily is not None:
        daily["ari"] = to_daily(D, ari, "max")
    hbtc = {"h0": hrs[0], "v": [_r(v, 0) for v in btc[r0:n]]}
    try:  # the hours Binance has not published yet, from the live books
        ext = live_extend(D, R)
        hb = apply_live(out, D, R, lo, ext)
        if hb:
            hbtc = hb
        if not ext or not ext.get("k"):
            for it in out:
                if it["id"] in ("liq", "resonance"):
                    it["chartNote"] = CHART_NOTE + " " + LIVE_PENDING
    except Exception as e:
        print(f"live hours skipped: {type(e).__name__}: {e}", flush=True)
    return out, hbtc


# ------------------------------------------------------------------ ALT Resilient Index (ours)
def ari_item(item, D, R, lo, r0, hrs):
    """CoinKarma: ARI compares altcoins with BTC to find relatively safe entries in sharp corrections (the ALT/BTC
    ratio rebounds as market makers buy back the altcoins they sold); benchmark 3. Formula not published.
    Ours: a market-cap-weighted index of the 23 altcoins' hourly returns against BTC; ARI = the z-score (past 30
    days) of its 24-hour change. Signal: ARI >= 3 while BTC is at least 10% below its 30-day high."""
    n = D.n
    btc = D.close["BTC"]
    num, den = np.zeros(n), np.zeros(n)
    for c in COINS:
        if c == "BTC":
            continue
        p = D.close[c]
        with np.errstate(invalid="ignore", divide="ignore"):
            lr = np.concatenate([[np.nan], np.log(p[1:] / p[:-1])])
        w = D.mcap[c]
        ok = D.eligible(c) & ~np.isnan(lr) & ~np.isnan(w)  # the 60-day rule and a price, whether or not the book file exists
        num += np.where(ok, w * np.nan_to_num(lr), 0.0)
        den += np.where(ok, w, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        alt_lr = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)
        b_lr = np.concatenate([[np.nan], np.log(btc[1:] / btc[:-1])])
    step = alt_lr - b_lr
    step[~np.isfinite(step)] = 0.0  # an hour without both sides moves neither
    rel = np.cumsum(step)  # log(alt index / BTC)
    d24 = np.full(n, np.nan)
    d24[24:] = rel[24:] - rel[:-24]
    d24[:lo] = np.nan
    ari = rolling_z(d24, 720, 360)
    hi30 = rolling_max(btc, 720)
    with np.errstate(invalid="ignore"):
        dd30 = btc / hi30 - 1
        sig = (ari >= 3) & (dd30 <= -0.10)
        sig_any = ari >= 3
    known = ~np.isnan(ari) & ~np.isnan(dd30)
    ev = first_hours(sig, 72, known, lo)
    ev_any = first_hours(sig_any, 72, ~np.isnan(ari), lo)
    H = 168
    st = [x for x in (ev_stats_h(btc, ev, H, lo, "ARI ≥ 3，且 BTC 比 30 天高點低 10% 以上"), ev_stats_h(btc, ev_any, H, lo, "ARI ≥ 3（不看跌幅）")) if x]
    last = n - 1
    while last > 0 and np.isnan(ari[last]):
        last -= 1
    v = ari[last]
    return item(id="ari", group="訂單簿流動性", name="ALT Resilient Index", zh="山寨韌性指標（ARI）", fidelity="ours", res="h", chartNote=CHART_NOTE,
                what="下跌中山寨幣相對 BTC 撐不撐得住：山寨幣對 BTC 的比值突然反彈，代表造市商在買回先前賣出的山寨幣。",
                ck="基準 3；在多頭中的急跌（BTC 跌 10–20%）最有用，可以拿來過濾 LIQ 的進場訊號。觸發不多，也不保證是最低點。沒有公開公式。",
                ours="23 個山寨幣依市值加權的每小時報酬減 BTC 報酬、累加成「山寨／BTC」指數；ARI = 它 24 小時變化的 Z 分數（過去 30 天）。訊號：ARI ≥ 3、且 BTC 比過去 30 天高點低 10% 以上（前 72 小時沒有），看之後 7 天 BTC 的表現。",
                diff="公式是我們照描述做的；門檻 3 沿用 CoinKarma。", unit="σ", dec=2,
                lines=[{"k": "v", "label": "ARI（Z 分數）"}],
                series={"h0": hrs[0], "n": len(hrs), "v": [_r(x, 2) for x in ari[r0:n]]},
                zones=[{"from": 3, "to": None, "tone": "good", "label": "≥ 3"}],
                marks=[{"d": D.hour(i), "label": "ARI 訊號", "tone": "good"} for i in ev if i >= r0],
                now={"d": D.hour(last), "v": _r(v, 2), "state": f"BTC 距 30 天高點 {dd30[last] * 100:.0f}%" if not np.isnan(dd30[last]) else "",
                     "tone": "good" if v >= 3 else "mid"},
                stats=st, statsKind="labeled", baseLabel="隨機一小時", src="Binance 合約 K 線、Coin Metrics 市值"), ari


# ------------------------------------------------------------------ Triton (CoinKarma's long-only strategy on the resonance)
CK_ILI_RED = ["2021-11-08", "2025-10-05"]  # CoinKarma's published ILI red (cycle-top) signals
FEE = 0.0005  # per side on the notional (OKX perpetual taker)
SLOT, LEV, MAXPOS = 0.09375, 2, 8


def triton(D, R, W, lo, etf30d=None, btc_daily=None, filters=True, funding=None):
    """Hour by hour: enter each coin at the close of the hour it resonates (higher LIQ first, at most 8 open,
    9.375% of equity as margin at 2x), exit everything at the close of the hour the wave ends. Filters: after
    a red ILI signal (until BTC closes above the top it marked), enter only while the ETF 30-day net flow > 0;
    BTC down 12% from its 48-hour high: sell everything, no entries for 24 hours."""
    n = D.n
    joins, ends = {}, {}
    for k, w in enumerate(W):
        if w["start"] < lo:
            continue
        for c, j in w["join"].items():
            joins.setdefault(j, []).append((float(R["liq"][c][j]), c, k))
        if w["end"] is not None:
            ends.setdefault(w["end"], []).append(k)
    btc = D.close["BTC"]
    # bear regime per day: from a red ILI signal until BTC closes above its highest close of the 30 days
    # after that signal (the top it marked)
    bear = {}
    if filters and btc_daily:
        ds = sorted(btc_daily)
        for r in CK_ILI_RED:
            r30 = (dt.date.fromisoformat(r) + dt.timedelta(days=30)).isoformat()
            top = max((btc_daily[d] for d in ds if r <= d <= r30), default=None)
            if top is None:
                continue
            for d in ds:
                if d < r:
                    continue
                bear[d] = True  # also on the day whose close breaks the top: that close is known only at its end
                if d > r30 and btc_daily[d] > top:
                    break
    hi48 = rolling_max(btc, 48)
    last_px = {c: np.nan for c in COINS}
    pos = {}  # coin -> [entry price, notional, entry hour, wave]
    realized, eq = 1.0, np.full(n, np.nan)
    trades, cool, guard_hits, skipped_bear, paid = [], -1, 0, 0, 0.0

    def close_all(i, why):
        nonlocal realized
        for c, (p0, notional, i0, k) in list(pos.items()):
            p1 = last_px[c]
            r = p1 / p0 - 1 if p0 > 0 and p1 > 0 else 0.0
            pnl = notional * r - notional * (1 + r) * FEE
            realized += pnl
            trades.append({"coin": c, "in": i0, "out": i, "ret": r, "pnl": pnl, "why": why})
        pos.clear()

    for i in range(lo, n):
        for c in COINS:
            v = D.close[c][i]
            if v > 0:
                last_px[c] = v
        # crash guard
        if filters and btc[i] > 0:
            hi = hi48[i]
            if hi > 0 and btc[i] / hi - 1 <= -0.12:
                if pos:
                    close_all(i, "BTC 48 小時跌 12%")
                    guard_hits += 1
                cool = i + 24
        if i in ends and pos:
            close_all(i, "波段結束")
        if i in joins and i >= cool:
            day = D.hour(i)[:10]
            lag = 1 if (i + D.h0) % 24 >= 14 else 2  # a US day's ETF flows are complete around 14:00 UTC the next day
            prev = (dt.date.fromisoformat(day) - dt.timedelta(days=lag)).isoformat()
            if filters and bear.get(day) and prev in (etf30d or {}) and not etf30d[prev] > 0:  # no ETF yet (before 2024): no filter
                skipped_bear += len(joins[i])
            else:
                mtm = realized + sum(nt * (last_px[c] / p0 - 1) for c, (p0, nt, i0, k) in pos.items() if p0 > 0)
                for z, c, k in sorted(joins[i], reverse=True):
                    if len(pos) >= MAXPOS or c in pos or not last_px[c] > 0:
                        continue
                    notional = mtm * SLOT * LEV
                    realized -= notional * FEE
                    pos[c] = [last_px[c], notional, i, k]
        if pos:  # funding paid by longs, hourly share of the day's 8-hour rate (BTC's OI-weighted rate as the proxy)
            f8 = (funding or {}).get((dt.date.fromisoformat(D.hour(i)[:10]) - dt.timedelta(days=1)).isoformat(), 0.01)
            cost = sum(nt for p0, nt, i0, k in pos.values()) * f8 / 100 / 8
            realized -= cost
            paid += cost
        eq[i] = realized + sum(nt * (last_px[c] / p0 - 1) for c, (p0, nt, i0, k) in pos.items() if p0 > 0)
    return {"eq": eq, "trades": trades, "open": dict(pos), "guard_hits": guard_hits, "skipped_bear": skipped_bear, "funding_paid": paid}


def perf(eq, D, lo):
    x = eq[lo:]
    ok = ~np.isnan(x)
    x = x[ok]
    if len(x) < 48:
        return None
    years = len(x) / (24 * 365.25)
    peak = np.maximum.accumulate(x)
    return {"ret": float(x[-1] / x[0] - 1), "cagr": float((x[-1] / x[0]) ** (1 / years) - 1) if years > 0.2 else None,
            "mdd": float((x / peak - 1).min()), "years": years}


def triton_item(item, D, R, W, lo, ctx):
    btc_daily = ctx.get("btc") or {}
    etf30d = ctx.get("etf30d") or {}
    pack = ctx.get("pack")
    funding = (ctx.get("daily") or {}).get("funding")
    T = triton(D, R, W, lo, etf30d, btc_daily, True, funding)
    T0 = triton(D, R, W, lo, etf30d, btc_daily, False, funding)
    btc = D.close["BTC"]
    hold = btc / btc[lo] if btc[lo] > 0 else np.full(D.n, np.nan)
    pf, pf0, pb = perf(T["eq"], D, lo), perf(T0["eq"], D, lo), perf(hold, D, lo)
    tr = T["trades"]
    wins = [t for t in tr if t["ret"] > 0]
    # daily equity (the last hour of each UTC day)
    days, v, v0, vb = [], [], [], []
    for i in range(lo, D.n):
        h = D.hour(i)
        if h.endswith("T23") or i == D.n - 1:
            days.append(h[:10])
            v.append(_r(T["eq"][i], 4))
            v0.append(_r(T0["eq"][i], 4))
            vb.append(_r(hold[i], 4))
    # by calendar year
    yrows = []
    for y in sorted({d[:4] for d in days}):
        idx = [k for k, d in enumerate(days) if d[:4] == y]
        k0 = idx[0] - 1 if idx[0] > 0 else None  # the first year starts from the backtest's start (all series = 1)
        k1 = idx[-1]
        f = lambda a: f"{(a[k1] / (a[k0] if k0 is not None else 1.0) - 1) * 100:+.0f}%" if a[k1] and (k0 is None or a[k0]) else "—"  # noqa: E731
        ntr = sum(1 for t in tr if D.hour(t["out"])[:4] == y)
        yrows.append([y + ("（至今）" if y == days[-1][:4] else ""), f(v), f(v0), f(vb), ntr])
    fmtp = lambda p, k: (f"{p[k] * 100:+.0f}%" if p and p.get(k) is not None else "—")  # noqa: E731
    rows = [["總報酬", fmtp(pf, "ret"), fmtp(pf0, "ret"), fmtp(pb, "ret")],
            ["年化", fmtp(pf, "cagr"), fmtp(pf0, "cagr"), fmtp(pb, "cagr")],
            ["最大回撤", fmtp(pf, "mdd"), fmtp(pf0, "mdd"), fmtp(pb, "mdd")],
            ["交易筆數", len(tr), len(T0["trades"]), "—"],
            ["勝率（單筆）", f"{len(wins) / len(tr) * 100:.0f}%" if tr else "—",
             f"{sum(1 for t in T0['trades'] if t['ret'] > 0) / len(T0['trades']) * 100:.0f}%" if T0["trades"] else "—", "—"],
            ["單筆報酬中位數", f"{statistics.median([t['ret'] for t in tr]) * 100:+.1f}%" if tr else "—",
             f"{statistics.median([t['ret'] for t in T0['trades']]) * 100:+.1f}%" if T0["trades"] else "—", "—"]]
    recent = [[short(D.label(t["in"])), short(D.label(t["out"])), t["coin"], f"{t['ret'] * 100:+.1f}%", t["why"]] for t in tr[-12:][::-1]]
    open_now = "、".join(T["open"]) or "空手"
    series = pack(days, {"v": v, "v0": v0, "b": vb}, {"v": 4, "v0": 4, "b": 4}) if pack else {"d": days, "v": v}
    return item(id="triton", group="訂單簿流動性", name="Triton Signal（回測）", zh="Triton 策略（共振做多）回測", fidelity="proxy",
                what="CoinKarma 的跟單策略：在流動性共振時做多永續合約，波段結束全部賣出。這裡用我們的共振照公開規則回測。",
                ck="共振就買（同一小時先買 LIQ 高的），每波每幣一次，最多同時 8 個、每個用 9.375% 資金當保證金、建議 2 倍；Overall LIQ 跌破 0（波段結束）全部賣出，沒有個別停損停利。ILI 紅色背離之後，只在 ETF 30 日淨流入 > 0 時交易；BTC 48 小時內跌 12% 就全部賣出、24 小時內不開新倉。",
                ours="每小時收盤成交，手續費每邊 0.05%（OKX 吃單）；資金費用 BTC 的持倉加權費率估算（多單付費）。ILI 紅色訊號用 CoinKarma 公布的日期（2021/11/08、2025/10/05），「之後」我們假設持續到 BTC 收盤突破訊號後 30 天內的最高收盤為止；ETF 淨流量用前一天的 30 日合計。CoinKarma 說 ETF 流入很強時會晚一點出場，但沒有門檻，這裡沒做。",
                diff="共振是我們用合約深度做的版本，和 CoinKarma 的訊號不會完全一樣；CoinKarma 的回測數字（2022/09–2026/09）只在他們網站的分頁上，文字裡沒有，無法對照。",
                unit="倍（起始 = 1）", dec=2, ydec=1, range="all",
                chartNote="回測圖固定顯示全部期間（不受「全部／近一年」切換影響）。",
                lines=[{"k": "v", "label": "Triton（含濾網）"}, {"k": "v0", "label": "不含濾網"}, {"k": "b", "label": "BTC 持有"}],
                series=series,
                now={"d": days[-1], "v": v[-1], "state": f"持倉：{open_now}", "tone": "good" if T["open"] else "mid"},
                tables=[{"title": f"回測結果（{D.hour(lo)[:10]} 起，{pf['years']:.1f} 年）" if pf else "回測結果", "head": ["", "Triton（含濾網）", "不含濾網", "BTC 持有"], "rows": rows, "text": []},
                        {"title": "各年度", "head": ["年", "Triton（含濾網）", "不含濾網", "BTC 持有", "交易筆數"], "rows": yrows, "text": []},
                        {"title": "最近的交易（台灣時間）", "head": ["進場", "出場", "幣", "報酬", "出場原因"], "rows": recent, "text": [0, 1, 4]}],
                src="Binance 合約公開資料（bookDepth、K 線）、cryptoetf.today",
                notes={"guard_hits": T["guard_hits"], "skipped_bear": T["skipped_bear"], "funding_paid": _r(T["funding_paid"], 4)})


def basket(D, R, i0, i1):
    """Equal-weight return of every coin that counts at i0, from i0's close to i1's close."""
    rs = []
    for c in COINS:
        if R["elig"][c][i0] and D.close[c][i0] > 0 and D.close[c][i1] > 0:
            rs.append(D.close[c][i1] / D.close[c][i0] - 1)
    return statistics.mean(rs) if rs else None


def resonance_item(item, D, R, W, lo, r0, hrs, diff_common):
    O = R["overall"]
    n = D.n
    fin = [w for w in W if w["end"] is not None and w["start"] >= lo]
    st = wave_stats(D, W, lo=lo)
    # returns: resonance coins (each from its own resonance hour to the wave's end) against every coin over the same
    # hours, and against every coin over random hours of the same length
    rng = np.random.default_rng(7)
    own, same, rand = [], [], []
    for w in fin:
        if not w["ret"]:
            continue
        own.append(statistics.mean(w["ret"].values()))
        b = basket(D, R, w["start"], w["end"])
        if b is not None:
            same.append(b)
        for _ in range(5):
            i0 = int(rng.integers(lo, max(lo + 1, n - 1 - w["hours"])))
            b2 = basket(D, R, i0, i0 + w["hours"])
            if b2 is not None:
                rand.append(b2)
    pct = lambda xs, f: f"{f(xs) * 100:+.1f}%" if xs else "—"  # noqa: E731
    up = lambda xs: f"{sum(x > 0 for x in xs) / len(xs) * 100:.0f}%" if xs else "—"  # noqa: E731
    ret_rows = [["共振的幣（各自從共振那小時到波段結束）", len(own), pct(own, statistics.mean), pct(own, statistics.median), up(own)],
                ["同一段時間的全部幣", len(same), pct(same, statistics.mean), pct(same, statistics.median), up(same)],
                ["隨機時段、同樣長度的全部幣", len(rand), pct(rand, statistics.mean), pct(rand, statistics.median), up(rand)]]
    ck_rows = []
    if st:
        t = CK_STATS
        ck_rows = [["每月幾波", f"約 {t['per_month']}", f"{st['per_month']:.1f}"],
                   ["波段長度（中位數）", f"約 {t['median_days']} 天", f"{st['median_days']:.1f} 天"],
                   ["一天內結束的比例", f"約 {t['short_share'] * 100:.0f}%", f"{st['short_share'] * 100:.0f}%"],
                   ["超過 7 天的比例", f"約 {t['long_share'] * 100:.0f}%", f"{st['long_share'] * 100:.0f}%"],
                   ["每波幾個幣（中位數）", f"{t['median_coins']}", f"{st['median_coins']:.0f}"],
                   ["24 小時內加入的幣（中位數）", f"約 {t['joined_24h']}", f"{st['joined_24h']:.0f}"]]
    ex_rows = []
    for m in match_ck(D, W):
        o = m.get("ours")
        dur = lambda h: f"{h // 24} 天 {h % 24} 小時" if h >= 24 else f"{h} 小時"  # noqa: E731
        if m.get("end"):
            ck_desc = f"{short(m['start'])} → {short(m['end'])}"
        else:
            ck_desc = f"{short(m['start'] + ' ')[:-1]}，{dur(m['hours'])}"
        ck_desc += f"；{m['coins']} 幣" + (f"，平均 {m['avg']:+.1f}%" if m.get("avg") is not None else "") + (f"；先 {'、'.join(m['first'])}" if m.get("first") else "")
        if o:
            ours = f"{short(o['start'])} → {short(o['end']) if o['end'] else '進行中'}（{dur(o['hours'])}）；{o['coins']} 幣" \
                + (f"，平均 {o['avg']:+.1f}%" if o.get("avg") is not None else "") + f"；先 {'、'.join(o['first'][:4])}"
        else:
            ours = "同一時間沒有波段"
        ex_rows.append([ck_desc, ours])
    spans, recent, last, o_now, state, cur = wave_view(D, W, O, r0)
    vst = None
    if own and rand:
        vst = {"n": len(own), "h": int(statistics.median([w["hours"] for w in fin])) if fin else 0, "unit": "h", "hl": "到波段結束",
               "ret": _r(statistics.median(own) * 100, 1), "base_ret": _r(statistics.median(rand) * 100, 1),
               "up_share": _r(sum(x > 0 for x in own) / len(own) * 100, 0), "base_up_share": _r(sum(x > 0 for x in rand) / len(rand) * 100, 0)}
    return item(id="resonance", group="訂單簿流動性", name="Liquidity Resonance", zh="流動性共振（Resonance）", fidelity="proxy", res="h", verdict_stat=vst, chartNote=CHART_NOTE,
                what="某個幣的 LIQ ≥ 3、同時 Overall LIQ ≥ 2 的小時叫「共振」；第一個共振開啟一個波段，波段結束前陸續加入的幣都算在裡面。",
                ck="Overall ≥ 2 時新的幣可以加入；0–2 之間波段延續、不再加入；跌破 0 波段結束，所有幣同時結束。新幣要有 60 天資料。2022/10–2026/09 約每月 3.5 波、長度中位數約 1.4 天、每波中位數 11 個幣。",
                ours=f"照 CoinKarma 的規則在我們的 LIQ 上跑（{D.hour(lo)[:10]} 起），報酬用 Binance 永續合約 1 小時收盤：每個幣從它共振那小時的收盤，到波段結束那小時的收盤。時間是台灣時間、K 線收盤。",
                diff=diff_common, unit="分", dec=2,
                lines=[{"k": "v", "label": "Overall LIQ（陰影＝波段）"}],
                series={"h0": hrs[0], "n": len(hrs), "v": [_r(v, 2) for v in O[r0:n]]},
                zones=[{"from": 2, "to": None, "tone": "good", "label": "≥ 2"}, {"from": None, "to": 0, "tone": "bad", "label": "< 0"}],
                spans=spans,
                now={"d": D.hour(last), "v": _r(o_now, 2), "state": state, "tone": "good" if cur else "mid"},
                tables=[{"title": "和 CoinKarma 公布的統計比", "head": ["", "CoinKarma（2022/10–2026/09）", f"我們（{D.hour(lo)[:7].replace('-', '/')} 起）"], "rows": ck_rows, "text": [1, 2]},
                        {"title": "CoinKarma 公布的波段（文件範例與貼文，台灣時間），我們同一時間的波段", "head": ["CoinKarma", "我們"], "rows": ex_rows, "text": [1]},
                        {"title": "波段的報酬（每波取平均）", "head": ["", "波數", "平均", "中位數", "上漲比例"], "rows": ret_rows, "text": [0]},
                        {"title": "最近的波段", "head": ["開始（台灣時間）", "結束", "長度", "幣數", "先後順序", "平均報酬"], "rows": recent, "text": [0, 1, 2, 4]}],
                src="Binance 合約公開資料（bookDepth、K 線）、Coin Metrics 市值",
                waves={"n": len(fin), "stats": st})


def screener_item(item, D, R, lo, diff_common):
    """CoinKarma's Token Screener: RSI_Dif (coin RSI - BTC RSI) against the coin's LIQ percentile over the past month."""
    L = R["liq"]
    n = D.n
    rs = {c: rsi(D.close[c]) for c in COINS}
    rb = rs["BTC"]

    def pctile(c, i, win=720):
        x = L[c][max(0, i - win + 1):i + 1]
        x = x[~np.isnan(x)]
        if len(x) < 200 or np.isnan(L[c][i]):
            return None
        return float((x < L[c][i]).mean() * 100 + (x == L[c][i]).mean() * 50)

    last = n - 1
    while last > 0 and np.isnan(R["overall"][last]):
        last -= 1
    rows = []
    for c in COINS:
        if c == "BTC" or not R["elig"][c][last] or np.isnan(rs[c][last]):  # BTC is the reference (it sits on the vertical axis)
            continue
        d = rs[c][last] - rb[last]
        p = pctile(c, last)
        if p is None:
            continue
        q = "I 強勢" if d > 0 and p > 50 else "III 弱勢" if d <= 0 and p < 50 else "II" if d <= 0 else "IV"
        rows.append((q, -d, c, d, p))
    rows.sort(key=lambda r: ({"I 強勢": 0, "IV": 1, "II": 2, "III 弱勢": 3}[r[0]], r[1]))
    qname = {"I 強勢": "I 強勢（RSI 差 > 0、LIQ 百分位 > 50）", "IV": "IV（RSI 差 > 0、LIQ 百分位 ≤ 50）",
             "II": "II（RSI 差 ≤ 0、LIQ 百分位 ≥ 50）", "III 弱勢": "III 弱勢（RSI 差 ≤ 0、LIQ 百分位 < 50）"}
    table = []
    for q in ("I 強勢", "IV", "II", "III 弱勢"):
        xs = [f"{c}（{round(d):+d}／{p:.0f}）" for qq, _, c, d, p in rows if qq == q]
        table.append([qname[q], len(xs), "、".join(xs) or "—"])
    bp = pctile("BTC", last)
    # backtest: once a day (00:00 UTC), quadrant I and quadrant III coins, their next 72 hours against BTC
    q1, q3, allc = [], [], []
    for i in range(lo + 720, n - H_FWD, 24):
        if np.isnan(rb[i]) or not D.close["BTC"][i] > 0 or np.isnan(D.close["BTC"][i + H_FWD]):
            continue
        b = D.close["BTC"][i + H_FWD] / D.close["BTC"][i] - 1
        for c in COINS:
            if c == "BTC" or not R["elig"][c][i] or np.isnan(rs[c][i]) or not D.close[c][i] > 0 or np.isnan(D.close[c][i + H_FWD]):
                continue
            p = pctile(c, i)
            if p is None:
                continue
            rel = D.close[c][i + H_FWD] / D.close[c][i] - 1 - b
            allc.append(rel)
            d = rs[c][i] - rb[i]
            if d > 0 and p > 50:
                q1.append(rel)
            elif d <= 0 and p < 50:
                q3.append(rel)
    bt = []
    for lab, xs in (("第 I 象限（強勢）", q1), ("第 III 象限（弱勢）", q3), ("全部幣", allc)):
        if xs:
            bt.append([lab, len(xs), f"{statistics.mean(xs) * 100:+.2f}%", f"{statistics.median(xs) * 100:+.2f}%", f"{sum(x > 0 for x in xs) / len(xs) * 100:.0f}%"])
    vst = None
    if q1 and allc:
        vst = {"n": len(q1), "h": H_FWD, "unit": "h", "hl": "72 小時（相對 BTC）", "ret": _r(statistics.median(q1) * 100, 1),
               "base_ret": _r(statistics.median(allc) * 100, 1), "up_share": _r(sum(x > 0 for x in q1) / len(q1) * 100, 0),
               "base_up_share": _r(sum(x > 0 for x in allc) / len(allc) * 100, 0)}
    n1 = sum(1 for r in rows if r[0] == "I 強勢")
    n3 = sum(1 for r in rows if r[0] == "III 弱勢")
    return item(id="screener", group="訂單簿流動性", name="Token Screener", zh="山寨幣篩選器（Token Screener）", fidelity="proxy", res="table", verdict_stat=vst,
                what="把每個幣放進四個象限：橫軸是它的 RSI 減 BTC 的 RSI（相對強弱），縱軸是它的 LIQ 在過去一個月的百分位（買盤支撐）。",
                ck="第 I 象限（相對強、LIQ 高）是強勢幣、第 III 象限是弱勢幣；BTC 在縱軸上越高，強勢訊號越可靠，越低則弱勢訊號越可靠。每小時更新。",
                ours="RSI 用 1 小時收盤、14 期（Wilder）；LIQ 百分位用過去 30 天的每小時 LIQ。回測：每天 00:00（UTC）分象限，看之後 72 小時相對 BTC 的報酬。",
                diff="CoinKarma 沒寫 RSI 的週期與時間框架，也沒寫象限的分界；這裡用 0 與第 50 百分位。" + diff_common,
                unit="個強勢幣", dec=0, lines=[], series={"d": []},
                now={"d": D.hour(last), "v": n1, "state": f"弱勢 {n3} 個；BTC 的 LIQ 百分位 {bp:.0f}" if bp is not None else f"弱勢 {n3} 個",
                     "tone": "mid"},
                tables=[{"title": f"目前（{short(D.label(last))} 台灣時間）：幣（RSI 差／LIQ 百分位）", "head": ["象限", "幣數", "幣"], "rows": table, "text": [2]},
                        {"title": "回測：之後 72 小時相對 BTC 的報酬", "head": ["", "次數", "平均", "中位數", "贏過 BTC 的比例"], "rows": bt, "text": [0]}],
                src="Binance 合約公開資料（bookDepth、K 線）")


if __name__ == "__main__":
    D = Data()
    if not D.ok:
        sys.exit("no order-book data yet")
    print("grid", hstr(D.h0), "->", hstr(D.h1), "coins with data:", {c: D.hour(D.first[c]) for c in COINS if D.first[c] is not None})
    cal = calibrate(D)
    for r in cal[:12]:
        st = r["stats"] or {}
        print(f"{r['norm']} depth {r['depth']}% window {r['window']:5d}h {r['mode']}: score {r['score']:.2f} examples {r['examples']}/4 "
              + " ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in st.items()))
    import json
    with open(os.path.join(ROOT, "research", "ck_liq_calibration.json"), "w") as f:
        json.dump({"built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "data": [D.hour(0), D.hour(D.n - 1)],
                   "coinkarma": CK_STATS, "chosen": PARAMS,
                   "grid": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items() if k != "stats"}
                            | {"stats": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in (r["stats"] or {}).items()}} for r in cal]},
                  f, indent=1, ensure_ascii=False)
    R = compute(D, **PARAMS)
    W = waves(D, R)
    for m in match_ck(D, W):
        print(m)
    for w in W[-12:]:
        print(D.label(w["start"]), "->", D.label(w["end"]) if w["end"] is not None else "now", f"{w['hours']}h", len(w["join"]), w["order"][:6],
              f"avg {statistics.mean(w['ret'].values()) * 100:+.1f}%" if w["ret"] else "")
