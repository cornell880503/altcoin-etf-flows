"""The "Bitfinex whale" pair CoinKarma points at (their tweet of 2026-11-23: when BTC broke 60K, Bitfinex BTCUSD LONGS
jumped by about 1,000 BTC and the Bitfinex premium went to an extreme, "which has often meant a relative bottom"):

- longs: Bitfinex's BTC/USD margin long positions in BTC (data/ck/raw/bitfinex.json, the day's last value, from
  2021; today's row is refreshed every hour by fetch_ck.py);
- premium: Bitfinex BTC/USD against Coinbase BTC/USD in %, from the hourly closes (data/ck/raw/bitfinex_1h.json,
  coinbase_1h.json), as the mean of the last 4 hours. Against the "Bitfinex Premium" in CoinKarma's Telegram
  tables (1,943 tables, 2025-02 .. 2026-11): r 0.97, slope 0.99, mean difference 0.013; they colour it at 0.25.

The event ("whale"): on a UTC day the longs end at least 1 % above the day before and the premium reached 0.40
during the day; another one within 5 days is the same episode. 2026-11-21: longs +1,092 BTC (+1.2 %; the second
day of 2026 with +1,000), premium up to 0.81 (the third highest since 2021, after 1.69 in the LUNA collapse and
1.65 in mid-2021; FTX was 0.75). What followed the earlier ones is in research/bfx_whale_backtest.py."""
import datetime as dt
import json
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "ck", "raw")
UTC = dt.timezone.utc
LONGS_JUMP = 0.01  # the longs' rise over one UTC day
PREM_HI = 0.40  # % : the day's highest 4-hour-mean premium
PREM_CK = 0.25  # % : CoinKarma's own colour level
COOL_D = 5  # days: a repeat within this many days belongs to the same episode
TREND_UP = 0.02  # the longs' rise over 7 days that counts as "still adding"


class Data:
    """days / longs: the daily series. t0, px, prem, prem4: hourly arrays from t0 (epoch seconds, candle opens); px is
    Bitfinex's close (short gaps carried forward), prem4 the 4-hour mean of the premium ending at that hour."""

    def __init__(self, upto=None):
        b = json.load(open(os.path.join(RAW, "bitfinex.json")))
        il = b["fields"].index("btcusd_long")
        rows = [(r[0], float(r[il])) for r in b["rows"] if r[il]]
        self.days = [d for d, _ in rows]
        self.longs = np.array([v for _, v in rows])
        self.k = {d: i for i, d in enumerate(self.days)}
        bh = {int(t): float(c) for t, c in json.load(open(os.path.join(RAW, "bitfinex_1h.json")))["rows"] if c}
        ch = {int(t): float(c) for t, c in json.load(open(os.path.join(RAW, "coinbase_1h.json")))["rows"] if c}
        if upto is not None:  # only the hours that had closed by `upto` (epoch seconds)
            bh = {t: c for t, c in bh.items() if t + 3600 <= upto}
            ch = {t: c for t, c in ch.items() if t + 3600 <= upto}
        self.t0, t1 = min(bh), max(bh)
        self.n = (t1 - self.t0) // 3600 + 1
        self.px = np.full(self.n, np.nan)
        self.prem = np.full(self.n, np.nan)
        for t, c in bh.items():
            self.px[(t - self.t0) // 3600] = c
        for t in set(bh) & set(ch):
            self.prem[(t - self.t0) // 3600] = (bh[t] / ch[t] - 1) * 100
        for i in range(1, self.n):
            if not np.isfinite(self.px[i]):
                self.px[i] = self.px[i - 1]
        ok = np.isfinite(self.prem)
        c1 = np.concatenate([[0.0], np.cumsum(np.where(ok, self.prem, 0.0))])
        c0 = np.concatenate([[0], np.cumsum(ok)])
        lo = np.maximum(np.arange(self.n) - 3, 0)
        cnt = c0[np.arange(self.n) + 1] - c0[lo]
        with np.errstate(invalid="ignore", divide="ignore"):
            self.prem4 = np.where(cnt > 0, (c1[np.arange(self.n) + 1] - c1[lo]) / cnt, np.nan)

    def end(self, d):
        """The index of the hour that closes at the end of UTC day d (may be beyond the data for today)."""
        t = int(dt.datetime.fromisoformat(d).replace(tzinfo=UTC).timestamp()) + 23 * 3600
        return (t - self.t0) // 3600

    def chg(self, d, w=1):
        k = self.k[d]
        return self.longs[k] / self.longs[k - w] - 1 if k >= w else float("nan")

    def pmax(self, d):
        """The highest 4-hour-mean premium during UTC day d (as far as the hours go)."""
        i = min(self.end(d), self.n - 1)
        j = max(self.end(d) - 23, 0)
        seg = self.prem4[j:i + 1]
        return float(np.nanmax(seg)) if j <= i and np.isfinite(seg).any() else float("nan")

    def is_event(self, d):
        return self.k[d] >= 1 and self.chg(d) >= LONGS_JUMP and self.pmax(d) >= PREM_HI

    def events(self, cool=COOL_D):
        out, last = [], None
        for d in self.days[1:]:
            if self.is_event(d) and (last is None or (dt.date.fromisoformat(d) - dt.date.fromisoformat(last)).days >= cool):
                out.append(d)
                last = d
        return out


def view(now):
    """What the watch shows: the longs, their changes, the premium now and over the last 24 hours, and the latest
    event (its day, the price at that day's end or the latest price if it is today, the change since)."""
    D = Data(upto=int(now.timestamp()))
    d = D.days[-1]
    k = len(D.days) - 1
    i = D.n - 1
    p24 = D.prem4[max(0, i - 23):i + 1]
    ev = D.events()
    last = ev[-1] if ev else None
    out = {"day": d, "longs": float(D.longs[-1]), "d1": float(D.longs[-1] - D.longs[-2]), "r1": float(D.chg(d)), "r7": float(D.chg(d, 7)),
           "r30": float(D.chg(d, 30)), "prem": float(D.prem4[i]), "prem24": float(np.nanmax(p24)) if np.isfinite(p24).any() else float("nan"),
           "px": float(D.px[i]), "event": last, "today_event": bool(D.is_event(d)), "k": k}
    if last:
        j = min(D.end(last), i)
        out.update({"event_px": float(D.px[j]), "event_r1": float(D.chg(last)), "event_d1": float(D.longs[D.k[last]] - D.longs[D.k[last] - 1]),
                    "event_prem": D.pmax(last), "since": float(D.px[i] / D.px[j] - 1),
                    "event_age": (dt.date.fromisoformat(d) - dt.date.fromisoformat(last)).days})
    return out
