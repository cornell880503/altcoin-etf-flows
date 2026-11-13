#!/usr/bin/env python3
"""A watch for a BTC rebound from the LIQ angle, printed as a short report in Chinese. The collector job
(scripts/binance_spot_ws.py) runs it every hour at :09:30 and commits the output as research/liq_watch_latest.txt;
the scheduled task "LIQ 監看" reads that file at :15 and relays the whole report only when the first line says
【狀態改變】, else nothing. The state is kept in research/liq_watch_state.json between runs.

What it reads (all in the repo, kept current by the workflows):
- CoinKarma's published tables (data/ck/pulse/pulse.csv, three a day at 00/08/16 UTC, read by ck-pulse at about
  :07): Overall LIQ, BTC LIQ, Accumulated BTC LIQ, Overall Liquidity in BTC, Pulse Index and their statuses.
  A table updated at hour U describes the book at the end of hour U-1;
- our Binance spot order books within 10 % of the price (data/ck/live/binspot, every 10 s): the hour's last
  sample where the files have it (from 2026-11-04 22:00 UTC+8 on), its mean before that;
- the other venues' books every 10 minutes and OKX's BTC closes (data/ck/live/hours).

The estimate of CoinKarma's BTC LIQ "now": their BTC LIQ against our log(bids / asks) within 10 % at their table
hours, fitted a + b x on the overlap since the collector's BTC book filled in (2026-10-31 10:00 UTC; r 0.96,
residual sd 0.4), plus half of the latest table's residual (published minus fitted at that table's hour) carried
forward: the residuals are autocorrelated (lag-1 0.3-0.5), and half of the last one lowers the error at the next
table (walk-forward: BTC LIQ rmse 0.48 -> 0.47, Overall Liquidity 305 -> 268 BTC). The same for their Overall
Liquidity in BTC against our all-coin 10 % difference in BTC. Their Overall LIQ cannot be estimated from our books
(r 0.17) and is taken from the table only.

The hourly state and when the first line says it changed:
- the estimated BTC LIQ flag: set to the published status whenever a new table arrives; between tables it turns
  on when the estimate is >= 3 (CoinKarma's Buy) for two hours running and off when it is below 2.5 for two hours
  running (the fit's error is about 0.4, so one hour near the threshold is noise). Over 2026-11-04..12 the
  anchored estimate for a table's own hour was on the published side of 3 for 26 of 26 tables (walk-forward; just
  assuming the previous table's status: 20 of 26), and this flag, as it stood the hour before a table, matched the
  table 22 of 24 times, with four between-table flips (the old one-hour rule with a 2.7 cut-off flipped nine
  times and raised an event for every change of the price-based verdict tier too: 29 notices in eight days);
- the Overall Liquidity flag likewise (>= 3,000 BTC on, below 2,500 off; Sell the mirror image), reported only
  while the green arrow is on, where it is the "加分項";
- events: the published green arrow (Overall and BTC LIQ both Buy) on or off; a published table that does not
  confirm an estimated flip; an estimated flip between tables; Overall LIQ into or out of Caution/Sell.

The verdict applies the historical results of research/ck_pulse_backtest.py (CoinKarma's own values 2025-02 ..
2026-11, BTC's return over the next 72 h / 7 d):
- green arrow published: 72 h median +1.2 %, 62-66 % up; after a dip (>= 5 % below the 7-day high) and the arrow
  fresh: 7 d 60 % up, a further 5 % fall within a week in 25-32 % of cases; with Overall Liquidity >= 3,000 BTC:
  72 h +2.5 %, 68 % up, 7 d +3.0 %; with funding <= 5 % a year: 72 h +1.9 %, 68 % up;
- green arrow re-lit 2-7 days after a spell that failed, with the price below that spell's lows: 10 cases, 72 h
  median -1.6 %, 60 % fell 5 % or more within a week;
- dip with Overall Buy but BTC LIQ not Buy: 72 h +0.4 %, 7 d -1.1 % (41 % up), 61 % fell 5 % or more within a week;
- Overall LIQ Caution / Sell: the next 3 days weak (Sell: 72 h -0.6 %).

The ETF side (CoinKarma's "BTC ETF 30D Net Flow", their tweet of 2026-11-13; scripts/btc_etf30.py reproduces it):
the US spot bitcoin ETFs' net flow over the last 30 days in BTC, as of the latest final session (a session counts
from 09:00 UTC the next day, when BlackRock's figure is in). Zones: net inflow (>= 0), net outflow, red (< -20K BTC,
their colour); a zone is left only 3K past its threshold, so a value wobbling around 0 does not notify every day.
A zone change is an event; a flip to net inflow says whether Overall LIQ is Buy at the same time (their
"resonance"). The verdict quotes research/etf_flow_backtest.json (2024-02 .. now): after entering red BTC fell
another 16-24 % before bottoming in each of the 4 completed cycles, and the flip back to inflow came 6-14 % above
the low; CoinKarma's green arrow did well only while the 30D flow was net inflow (newly lit: 14 d +5.2 %, 71 % up)
and was close to a coin toss while it was negative (in red: 14 d +0.4 %, 53 % up).

Usage: liq_watch.py [--table-wait]   (--table-wait: at a table hour, exit 3 instead of reporting while the
table due this hour has not been read yet and it is less than 25 minutes past the hour; the collector then
tries again in three minutes)."""
import argparse
import csv
import datetime as dt
import glob
import gzip
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import binance_spot_ws as bws  # noqa: E402
import btc_etf30 as E  # noqa: E402
import ck_liq as C  # noqa: E402

STATE = os.environ.get("LIQ_WATCH_STATE") or os.path.join(ROOT, "research", "liq_watch_state.json")
FIT_FROM = C.hidx("2026-10-31T10")  # the collector's BTC book had filled in (a day after it started)
UTC8 = dt.timedelta(hours=8)
ON_BTC, OFF_BTC = 3.0, 2.5  # the estimated BTC LIQ flag: on at CoinKarma's Buy threshold, off a fit error below it
ON_TOT, OFF_TOT = 3000.0, 2500.0  # the Overall Liquidity (BTC) flag
CONFIRM_H = 2  # a flip needs the condition this hour and the hour before
ANCHOR = 0.5  # the share of the latest table's residual carried forward
TABLE_WAIT_MIN = 25  # --table-wait: how long after a table hour the table is waited for
ETF_HIST = os.path.join(ROOT, "research", "etf_flow_backtest.json")


def tw(h):
    """Our hour index (candle open, UTC) -> 'M/D HH:00' Taiwan time of the candle's open."""
    t = dt.datetime.combine(dt.date.fromordinal(C.EPOCH + h // 24), dt.time(h % 24)) + UTC8
    return f"{t.month}/{t.day} {t.hour:02d}:00"


def live_rows():
    """{hour index: {key: row}} from data/ck/live/hours for the BTC rows (px, okx, cb, hl)."""
    out = {}
    for p in sorted(glob.glob(os.path.join(C.LIVE, "*.csv.gz"))):
        with gzip.open(p, "rt", newline="") as f:
            rd = csv.reader(f)
            next(rd, None)
            for r in rd:
                if len(r) > 10 and r[1] in ("px:BTC", "okx:BTC", "cb:BTC", "hl:BTC"):
                    out.setdefault(C.hidx(r[0]), {})[r[1]] = r
    return out


def ratio(r):
    try:
        b, a = float(r[9]), float(r[10])
        return b / a if a > 0 else None
    except (TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------- the estimate: our books on CoinKarma's scale
def x_btc(rows, h):
    """log(bids / asks) of the BTC book within 10 % at the end of hour h (the hour's mean where no last sample)."""
    r = rows.get(h, {}).get("BTC")
    if not r:
        return None
    bq, aq = r.get("bidq10_last") or r.get("bidq10"), r.get("askq10_last") or r.get("askq10")
    return math.log(bq / aq) if bq and aq else None


def x_tot(rows, h):
    """(bids - asks) within 10 % over all coins but ZEC, in BTC, at the end of hour h."""
    d = rows.get(h, {})
    for sb, sa, sm in (("bidn10_last", "askn10_last", "mid_last"), ("bidn10", "askn10", "mid")):
        cs = [c for c in C.COINS if c != "ZEC" and c in d and d[c].get(sb) is not None and d[c].get(sa) is not None]
        m = d.get("BTC", {}).get(sm)
        if len(cs) >= 20 and m:
            return sum(d[c][sb] - d[c][sa] for c in cs) / m
    return None


def book(rows, h):
    """(mid, bids US$, asks US$) of the BTC book within 10 % at the end of hour h."""
    r = rows.get(h, {}).get("BTC")
    if not r:
        return None
    return r.get("mid_last") or r.get("mid"), r.get("bidn10_last") or r.get("bidn10"), r.get("askn10_last") or r.get("askn10")


def fit(rows, P, fx, key, upto=None):
    """a + b x over the tables since FIT_FROM (with update hour <= upto when given): (a, b, sd, n, r), or None."""
    pts = []
    for k, h in enumerate(P["hours"]):
        U = int(h)
        if U - 1 < FIT_FROM or (upto is not None and U > upto):
            continue
        x, y = fx(rows, U - 1), P["v"][key][k]
        if x is not None and np.isfinite(y):
            pts.append((x, y))
    if len(pts) < 6:
        return None
    x, y = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    b, a = np.polyfit(x, y, 1)
    return a, b, float((y - (a + b * x)).std()), len(pts), float(np.corrcoef(x, y)[0, 1])


def offset(rows, P, fx, key, f, U):
    """ANCHOR * (the table U's published value - the fitted value at its hour), 0 without data."""
    for k, h in enumerate(P["hours"]):
        if int(h) == U:
            x, y = fx(rows, U - 1), P["v"][key][k]
            return ANCHOR * (y - (f[0] + f[1] * x)) if f and x is not None and np.isfinite(y) else 0.0
    return 0.0


def est_at(rows, fx, f, off, h):
    x = fx(rows, h)
    return f[0] + f[1] * x + off if f and x is not None else None


def skill(rows, P, fx, key, st_key, thr):
    """Walk-forward: at each table from the 11th on, was our anchored estimate for the hour the table describes (fit
    and anchor from the tables before it) on the published side of thr? (hits, n, hits of 'same as the previous
    table'). Our value for that hour is there at the hour's end, the table a few minutes later; the point is that
    the estimate reproduces their status, so the hours between tables can be trusted."""
    hs = [int(h) for h in P["hours"]]
    ks = [k for k, U in enumerate(hs) if U - 1 >= FIT_FROM and fx(rows, U - 1) is not None and np.isfinite(P["v"][key][k])]
    hits = base = n = 0
    for i in range(10, len(ks)):
        k, kp = ks[i], ks[i - 1]
        f = fit(rows, P, fx, key, upto=hs[kp])
        e = est_at(rows, fx, f, offset(rows, P, fx, key, f, hs[kp]), hs[k] - 1)
        if e is None:
            continue
        pub = P["st"][st_key][k] == "Buy"
        n += 1
        hits += (e >= thr) == pub
        base += (P["st"][st_key][kp] == "Buy") == pub
    return hits, n, base


# ---------------------------------------------------------------- the ETF side: the 30-day net flow
def md(d):
    """'2026-11-12' -> '11/12'"""
    return f"{int(d[5:7])}/{int(d[8:10])}"


def etf_view(now):
    """The 30D net flow as of the latest final session, its zone and what it would take to change; None without data."""
    try:
        rows = E.load()
    except (OSError, ValueError, KeyError):
        return None
    final = [r for r in rows if E.settled_at(r[0]) <= now]
    if len(final) < 40:
        return None
    f30 = E.flow30(final)
    ds = [r[0] for r in final]
    zs = E.zones(f30, ds)
    D, zone = zs[-1]
    k = len(zs) - 1
    while k > 0 and zs[k - 1][1] == zone:
        k -= 1
    since = zs[k][0]
    v, v1 = f30[D], f30[ds[-2]]
    start = (dt.date.fromisoformat(D) - dt.timedelta(days=E.WINDOW_D)).isoformat()
    usd = sum(r[1] for r in final if start <= r[0] <= D)
    last = final[-1]
    prelim = rows[-1] if rows[-1][0] > D else None
    path = [(t, v - lv) for t, lv in E.leaving(final, E.WINDOW_D + 1)]  # the 30D on day t if every later session were 0
    low_t, low_v = min(path, key=lambda p: p[1])
    out_red = next((t for t, x in path if x >= E.RED), None) if v < E.RED else None
    t14 = (dt.date.fromisoformat(D) + dt.timedelta(days=14)).isoformat()
    n14 = sum(1 for i in range(1, 15) if (dt.date.fromisoformat(D) + dt.timedelta(days=i)).weekday() < 5)
    need14 = -dict(path)[t14]  # thousand BTC of net inflow needed by then for the 30D to reach 0
    return {"date": D, "zone": zone, "since": since, "v": v, "v1": v1, "usd": usd, "flow": last[1], "flow_k": last[2], "price": last[3],
            "prelim": prelim, "low_t": low_t, "low_v": low_v, "out_red": out_red, "need14": need14, "n14": n14, "days": (dt.date.fromisoformat(D) - dt.date.fromisoformat(since)).days}


def etf_hist():
    """The backtest's numbers for the texts (research/etf_flow_backtest.json), None if it is missing."""
    try:
        H = json.load(open(ETF_HIST))
    except (OSError, ValueError):
        return None
    cyc, seen = [], set()
    for c in H.get("cycles", []):  # completed cycles, one per episode (a re-entry into red before the flip is the same one)
        if "flip_up" in c and c["flip_up"] not in seen:
            seen.add(c["flip_up"])
            cyc.append(c)
    return {"cycles": cyc, "green": H.get("green_by_zone", {}), "zc": H.get("zone_changes", {}),
            "flip_liq": H.get("events", {}).get("flip_up_liq_buy", {}).get("list", [])}


def zc_text(H, key, what):
    """'過去 n 次<what>，7 天後 x% 上漲（中位數 ..）、14 天 ..' from the zone-change statistics."""
    s = (H or {}).get("zc", {}).get(key, {}).get("summary") if H else None
    if not s or "7d" not in s:
        return ""
    def one(h, k):
        up = s[k]["up"]
        word = "全部下跌" if up == 0 else "全部上漲" if up == 1 else f"{up:.0%} 上漲"
        return f"{h} 天後{word}（中位數 {s[k]['median']:+.1%}）"
    parts = [one(h, k) for h, k in (("7", "7d"), ("14", "14d"), ("30", "30d")) if k in s]
    return f"過去有結果的 {s['7d']['n']} 次{what}，" + "、".join(parts)


def etf_event(prev_zone, ev, pub_ov, H):
    """The notice for a zone change."""
    z, v = ev["zone"], ev["v"]
    if z == "in":
        txt = f"ETF 30 天淨流量翻正（{md(ev['date'])} 收 {v:+.1f}K BTC，{ {'out': '淨流出', 'red': '紅區'}.get(prev_zone, prev_zone)} → 淨流入）"
        if pub_ov == "Buy":
            fl = (H or {}).get("flip_liq") or []
            hist = "、".join(f"{e['date'][:7]} 14 天 {e['14d']:+.0%}" for e in fl if e.get("14d") is not None)
            txt += "，Overall LIQ 同時 Buy：CoinKarma 說的共振成立" + (f"（2025-02 以來這樣的 {len(fl)} 次：{hist}）" if hist else "")
        else:
            txt += f"，Overall LIQ 目前 {pub_ov}，還沒共振"
        t = zc_text(H, "any>in", "翻正")
    elif z == "red":
        txt = f"ETF 30 天淨流出跌破 2 萬 BTC（進紅區，{md(ev['date'])} 收 {v:+.1f}K）"
        t = zc_text(H, "out>red", "進紅區")
    elif prev_zone == "red":
        txt = f"ETF 30 天淨流出回到 1.7 萬 BTC 以內（離開紅區，{md(ev['date'])} 收 {v:+.1f}K）"
        t = zc_text(H, "red>out", "離開紅區")
    else:
        txt = f"ETF 30 天淨流量轉為淨流出（{md(ev['date'])} 收 {v:+.1f}K）"
        t = zc_text(H, "in>out", "轉淨流出")
    return txt + (f"；{t}" if t else "")


# ---------------------------------------------------------------- the hourly state
def decide(prev, obs):
    """The state after this hour from the saved one; (state, events). obs: table (update hour of the latest table),
    hour, pub_btc / pub_overall / pub_tot (its statuses), e / e1 (BTC LIQ estimate this hour and the hour before),
    t / t1 (the same for Overall Liquidity in BTC)."""
    U = obs["table"]
    fresh = prev.get("table") != U
    pub_btc, pub_ov, pub_tot = obs["pub_btc"], obs["pub_overall"] or "Hold", obs["pub_tot"]
    e, e1, t, t1 = obs["e"], obs["e1"], obs["t"], obs["t1"]
    btc_on = prev.get("btc_on")
    if fresh or btc_on is None:  # the table is the truth for the hour before it
        btc_on = pub_btc == "Buy"
    elif e is not None and e1 is not None:
        if not btc_on and e >= ON_BTC and e1 >= ON_BTC:
            btc_on = True
        elif btc_on and e < OFF_BTC and e1 < OFF_BTC:
            btc_on = False
    tot = prev.get("tot")
    if fresh or tot is None:
        tot = {"Buy": 1, "Sell": -1}.get(pub_tot, 0)
    elif t is not None and t1 is not None:
        if tot != 1 and t >= ON_TOT and t1 >= ON_TOT:
            tot = 1
        elif tot != -1 and t <= -ON_TOT and t1 <= -ON_TOT:
            tot = -1
        elif tot == 1 and t < OFF_TOT and t1 < OFF_TOT:
            tot = 0
        elif tot == -1 and t > -OFF_TOT and t1 > -OFF_TOT:
            tot = 0
    green_pub = pub_ov == "Buy" and pub_btc == "Buy"
    green_est = pub_ov == "Buy" and btc_on
    bad = pub_ov in ("Caution", "Sell")
    state = {"table": int(U), "hour": int(obs["hour"]), "green_pub": green_pub, "btc_on": btc_on, "green_est": green_est,
             "overall": pub_ov, "tot": tot, "e": None if e is None else round(e, 2), "t": None if t is None else round(t)}
    ev = obs.get("etf")
    if ev:
        state.update({"etf_date": ev["date"], "etf_zone": ev["zone"]})
    elif prev.get("etf_zone"):  # no flow data this run: keep what we had
        state.update({"etf_date": prev.get("etf_date"), "etf_zone": prev["etf_zone"]})
    events = []
    if prev:
        if fresh:
            if green_pub != prev.get("green_pub"):
                events.append(f"CoinKarma 公布的綠色箭頭{'亮起' if green_pub else '熄滅'}（{tw(U)} 的表：Overall {pub_ov}、BTC LIQ {pub_btc}）")
            elif green_est != prev.get("green_est"):
                events.append(f"CoinKarma {tw(U)} 的表（BTC LIQ {obs['pub_btc_v']:.2f} {pub_btc}）沒有確認即時估計的"
                              f"{'熄滅' if green_est else '亮起'}，綠色箭頭（估）{'回到亮起' if green_est else '熄滅'}")
            was_bad = prev.get("overall") in ("Caution", "Sell")
            if bad and (not was_bad or prev.get("overall") != pub_ov):
                events.append(f"Overall LIQ 轉 {pub_ov}（買單被吃）")
            elif was_bad and not bad:
                events.append(f"Overall LIQ 離開 {prev.get('overall')}，回到 {pub_ov}")
        elif btc_on != prev.get("btc_on"):
            if btc_on:
                events.append(f"即時估計 BTC LIQ 連續 {CONFIRM_H} 小時 ≥ {ON_BTC:g}" + ("，綠色箭頭（估）亮起" if pub_ov == "Buy" else f"，但 Overall 是 {pub_ov}，綠色箭頭還不亮"))
            else:
                events.append(f"即時估計 BTC LIQ 連續 {CONFIRM_H} 小時 < {OFF_BTC:g}" + ("，綠色箭頭（估）熄滅" if pub_ov == "Buy" else ""))
        if tot != prev.get("tot") and (green_est or green_pub or prev.get("green_est")):
            events.append({1: "全市場掛單 ≥ 3,000 BTC（加分項成立）", -1: "全市場掛單 ≤ −3,000 BTC"}.get(tot, "全市場掛單回到 ±3,000 內（加分項消失）")
                          + ("（公布）" if fresh else "（即時估計）"))
        if ev and prev.get("etf_zone") and ev["zone"] != prev["etf_zone"]:  # a state from before 2026-11-14 has no zone: set silently
            events.append(etf_event(prev["etf_zone"], ev, pub_ov, obs.get("etf_hist")))
    return state, events


def pct(v):
    return f"{v:+.1%}"


def reading(rows, hb, e_now, e_3):
    """One line on what the BTC book did over the last three hours, against the price (CoinKarma's kind of
    insight: bids stacking under a falling price, or being eaten)."""
    b1, b0 = book(rows, hb), book(rows, hb - 3)
    if not b1 or not b0 or not all(b1) or not all(b0):
        return None
    dp, db, da = b1[0] / b0[0] - 1, b1[1] / b0[1] - 1, b1[2] / b0[2] - 1
    base = f"過去 3 小時（到 {tw(hb)[-5:]} 收）價格 {pct(dp)}，±10% 買單 {pct(db)}、賣單 {pct(da)}"
    # typical 3-hour moves (2026-11): bids and asks +-2-3 %, one in ten beyond 6-8 %; the price 0.3 %, one in ten beyond 0.6 %
    tilt = db - da
    if dp <= -0.005:
        if db >= 0.05:
            why = "：價格跌、買單反而增加，下方有人掛單接（CoinKarma 說的 liquidity building），LIQ 往上"
        elif db <= -0.05 and tilt <= -0.03:
            why = "：價格跌、買單減得比賣單快，買單被吃掉或撤走，支撐在變薄"
        elif da <= -0.05 and tilt >= 0.03:
            why = "：價格跌、賣單撤得比買單快，買賣比往買方傾斜（LIQ 往上），但不是新買單進場"
        elif db <= -0.05:
            why = "：價格跌、兩邊掛單都在減少，盤變薄"
        else:
            why = "：價格跌，掛單沒什麼變化，還看不出有人接"
    elif dp >= 0.005:
        if da <= -0.05 and tilt >= 0.03:
            why = "：價格漲、賣單減少，上方的賣單被吃掉，阻力變薄"
        elif da >= 0.05 and tilt <= -0.03:
            why = "：價格漲但賣單增加，上方有人加掛賣單，反彈遇壓"
        elif db >= 0.05 and tilt >= 0.03:
            why = "：價格漲、買單跟著往上掛，有人追價接"
        else:
            why = "：價格漲，掛單沒什麼變化"
    elif tilt >= 0.05:
        why = "：價格橫盤，買賣比往買方傾斜"
    elif tilt <= -0.05:
        why = "：價格橫盤，買賣比往賣方傾斜"
    else:
        why = "：價格與掛單都沒什麼變化"
    if e_now is not None and e_3 is not None:
        why += f"；BTC LIQ 估計 3 小時 {e_now - e_3:+.2f}"
    return "盤面解讀：" + base + why + "。"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table-wait", action="store_true", help="at a table hour, exit 3 while the table due this hour is not read yet")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    P = C.ck_pulse()
    rows = {C.hidx(h): d for h, d in bws.read_hours().items()}
    L = live_rows()
    # ------------------------------------------------------------ CoinKarma's latest table
    hs = [int(h) for h in P["hours"]]
    j = len(hs) - 1
    U = hs[j]
    h_now = C.hidx(now.strftime("%Y-%m-%dT%H"))
    if args.table_wait and h_now % 8 == 0 and now.minute < TABLE_WAIT_MIN and U < h_now:
        print(f"the table due at {C.hstr(h_now)} is not in pulse.csv yet (latest {C.hstr(U)})", file=sys.stderr)
        return 3
    v = {k: P["v"][k][j] for k in P["v"]}
    st = {k: P["st"][k][j] for k in P["st"]}
    green = st["overall_liq"] == "Buy" and st["btc_liq"] == "Buy"  # as published
    stat_f = ""
    try:  # the funding row is not in ck_pulse(): read it from the csv
        for r in csv.DictReader(open(C.PULSE_CSV, newline="")):
            u = r.get("update_at") or ""
            if len(u) >= 13 and u[:13] == C.hstr(U)[:13]:
                stat_f = r.get("oi_funding_apy_st") or ""
    except OSError:
        pass
    prev_v = {k: P["v"][k][j - 1] for k in P["v"]}
    # ------------------------------------------------------------ prices (OKX hourly closes)
    closes = {h: float(d["px:BTC"][3]) for h, d in L.items() if "px:BTC" in d and d["px:BTC"][3]}
    hc = max(closes)
    px = closes[hc]
    last24 = [closes[h] for h in range(hc - 23, hc + 1) if h in closes]
    wk = [closes[h] for h in range(hc - 167, hc + 1) if h in closes]
    lo24, hi7, lo7 = min(last24), max(wk), min(wk)
    chg24 = px / closes[hc - 24] - 1 if hc - 24 in closes else float("nan")
    mid_now = rows.get(max(rows), {}).get("BTC", {}).get("mid")
    # a green spell that ended in the last 7 days, and whether the price is below its lows
    spell_js = [k for k in range(j, -1, -1) if hs[j] - hs[k] <= 7 * 24 and P["st"]["overall_liq"][k] == "Buy" and P["st"]["btc_liq"][k] == "Buy"]
    failed_below = False
    if spell_js and not all(P["st"]["overall_liq"][k] == "Buy" and P["st"]["btc_liq"][k] == "Buy" for k in range(min(spell_js), j + 1)):
        spell_px = [closes[hs[k] - 1] for k in spell_js if hs[k] - 1 in closes]
        failed_below = bool(spell_px) and px < min(spell_px)
    # ------------------------------------------------------------ the estimate from our spot books
    fb = fit(rows, P, x_btc, "btc_liq")
    ft = fit(rows, P, x_tot, "overall_liquidity_btc")
    ob = offset(rows, P, x_btc, "btc_liq", fb, U)
    ot = offset(rows, P, x_tot, "overall_liquidity_btc", ft, U)
    hb = max(h for h in rows if rows[h].get("BTC"))
    e = {h: est_at(rows, x_btc, fb, ob, h) for h in range(hb - 3, hb + 1)}
    t = {h: est_at(rows, x_tot, ft, ot, h) for h in range(hb - 3, hb + 1)}
    e_now, e_prev = e[hb], e[hb - 1]
    t_now, t_prev = t[hb], t[hb - 1]
    bk = book(rows, hb)
    # other venues: the latest hour's ratios against the hour before
    hv = max(h for h, d in L.items() if "okx:BTC" in d)
    venues = []
    for key, name in (("okx:BTC", "OKX 合約"), ("cb:BTC", "Coinbase"), ("hl:BTC", "Hyperliquid")):
        r1, r0 = ratio(L.get(hv, {}).get(key)), ratio(L.get(hv - 1, {}).get(key))
        if r1:
            venues.append(f"{name} {r1:.2f}" + (f"（前一小時 {r0:.2f}）" if r0 else ""))
    # ------------------------------------------------------------ the state
    prev = {}
    try:
        with open(STATE) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass
    if prev and "green_pub" not in prev and "green" in prev:  # a state written before 2026-11-13
        prev["green_pub"] = prev["green"]
        prev["tot"] = {"Buy": 1, "Sell": -1}.get(prev.get("ovliq"), 0)
    ev = etf_view(now)
    H = etf_hist()
    obs = {"table": U, "hour": hb, "pub_btc": st["btc_liq"], "pub_btc_v": v["btc_liq"], "pub_overall": st["overall_liq"],
           "pub_tot": st["overall_liquidity_btc"], "e": e_now, "e1": e_prev, "t": t_now, "t1": t_prev, "etf": ev, "etf_hist": H}
    state, events = decide(prev, obs)
    btc_on, tot, green_est = state["btc_on"], state["tot"], state["green_est"]
    overall_pub = state["overall"]
    overall_bad = overall_pub in ("Caution", "Sell")
    stopped = px >= lo24 * 1.01
    if overall_bad:
        tier = "買單被吃"
    elif green_est and stopped and not failed_below:
        tier = "成立"
    elif green_est:
        tier = "部分成立"
    else:
        tier = "未成立"
    # ------------------------------------------------------------ the report
    t_tw = now + UTC8
    lines.append(f"LIQ 監看 {t_tw.month}/{t_tw.day} {t_tw.hour:02d}:{t_tw.minute:02d}（台灣時間）")
    lines.append(f"BTC {px:,.0f}（{tw(hc)} 收盤" + (f"；最新均價 {mid_now:,.0f}" if mid_now else "") + f"）｜24 小時 {chg24:+.1%}｜距 7 天高點 {px / hi7 - 1:+.1%}、距 7 天低點 {px / lo7 - 1:+.1%}")
    s = lambda k: st[k] or "?"  # noqa: E731
    fmt = lambda x, spec: format(x, spec) if x is not None and np.isfinite(x) else "—"  # noqa: E731  (a value the OCR did not read)
    lines.append(f"CoinKarma 公布（{tw(U)}）：Overall {fmt(v['overall_liq'], '.2f')} {st['overall_liq']}、BTC LIQ {fmt(v['btc_liq'], '.2f')} {st['btc_liq']}"
                 f"（上一張 {fmt(prev_v['overall_liq'], '.2f')}／{fmt(prev_v['btc_liq'], '.2f')}）、累積 {fmt(v['acc_btc_liq'], '.0f')} {s('acc_btc_liq')}、"
                 f"全市場掛單 {fmt(v['overall_liquidity_btc'], ',.0f')} BTC {s('overall_liquidity_btc')}、Pulse {fmt(v['pulse_index'], '.0f')} {s('pulse_index')}；"
                 f"綠色箭頭{'亮著' if green else '沒亮'}")
    if fb and e_now is not None:
        e_txt = "、".join(f"{tw(h)[-5:]} {e[h]:+.2f}" if e[h] is not None else f"{tw(h)[-5:]} —" for h in range(hb - 2, hb + 1))
        flag_txt = f"旗標{'亮' if btc_on else '熄'}（≥ {ON_BTC:g} 連續 {CONFIRM_H} 小時才亮、< {OFF_BTC:g} 連續 {CONFIRM_H} 小時才熄，新表到就照公布值）"
        lines.append(f"即時估計（我們的 Binance 現貨 ±10%，小時末，已依 {tw(U)[-5:]} 公布值校正 {ob:+.2f}）：BTC LIQ 估 {e_txt}（誤差約 ±{fb[2]:.1f}，門檻 3）；{flag_txt}"
                     + (f"；買單 {bk[1] / 1e6:.0f}M／賣單 {bk[2] / 1e6:.0f}M（{bk[1] / bk[2]:.2f} 倍）" if bk and all(bk) else "")
                     + (f"；全市場掛單估 {t_now:,.0f} BTC（門檻 3,000；旗標 {({1: 'Buy', -1: 'Sell'}.get(tot, 'Hold'))}）" if t_now is not None else ""))
    rd = reading(rows, hb, e_now, e.get(hb - 3))
    if rd:
        lines.append(rd)
    if venues:
        lines.append(f"其他交易所 ±5% 買賣比（{tw(hv)} 起這小時）：" + "、".join(venues))
    if ev:
        word = "流入" if ev["flow"] >= 0 else "流出"
        txt = (f"ETF 資金（CoinKarma 的 BTC ETF 30D Net Flow，同算法）：{md(ev['date'])} 美股 BTC ETF 淨{word} {abs(ev['flow']):,.0f}M 美元（{ev['flow_k']:+.1f}K BTC）；"
               f"30 天合計 {ev['v']:+.1f}K BTC（前一日 {ev['v1']:+.1f}K，約 {ev['usd'] / 1e3:+.1f}B 美元），{E.ZONE_TW[ev['zone']]}，{md(ev['since'])} 起第 {ev['days'] + 1} 天")
        if ev["prelim"]:
            txt += f"（{md(ev['prelim'][0])} 的初步數字 {ev['prelim'][1]:+,.0f}M，BlackRock 等還沒報完，台灣 17:00 後才算）"
        fut = []
        if ev["low_v"] < ev["v"] - 1:
            fut.append(f"之後若每天都是 0，30 天合計會先降到 {ev['low_v']:+.0f}K（{md(ev['low_t'])}，窗口前段的流入先移出）")
        if ev["out_red"]:
            fut.append(f"{md(ev['out_red'])} 才回到 −2 萬以上")
        if ev["zone"] != "in" and ev["need14"] > 0:
            fut.append(f"兩週內（{ev['n14']} 個交易日）要翻正，平均每天需淨流入約 {ev['need14'] / ev['n14']:.1f}K BTC（約 {ev['need14'] / ev['n14'] * ev['price'] / 1e3:,.0f}M 美元）")
        lines.append(txt + ("；" + "，".join(fut) if fut else "") + "。")
    # ------------------------------------------------------------ the verdict
    why = []
    if green and btc_on:
        src = "（公布，即時估計也同意）"
    elif green:
        src = f"（公布亮著，但即時估計已連續 {CONFIRM_H} 小時 < {OFF_BTC:g}，估已熄）" if not btc_on else "（公布）"
    else:
        src = f"（即時估計，{tw(U)[-5:]} 公布的 BTC LIQ 還是 {v['btc_liq']:.2f} {st['btc_liq']}）"
    next_U = (U // 8 + 1) * 8
    if tier == "買單被吃":
        verdict = f"反彈條件未成立：Overall LIQ 轉 {overall_pub}，買單在被吃掉；這種狀態歷史上之後 3 天偏弱。"
    elif tier == "成立":
        verdict = f"反彈條件成立{src}：Overall ≥ 2 且 BTC LIQ ≥ 3，價格也止跌了。歷史上之後 72 小時中位數 +1.2%、62–66% 上漲；一週內再跌 5% 的比例 25–32%。"
    elif tier == "部分成立" and failed_below:
        verdict = f"部分成立{src}：綠色箭頭亮著，但這是上一段綠燈失敗、價格跌破那段低點之後再亮。歷史 10 次，72 小時中位數 −1.6%，60% 一週內再跌 5% 以上。"
    elif tier == "部分成立":
        verdict = f"部分成立{src}：綠色箭頭亮著，但價格還沒止跌（仍在 24 小時低點 1% 內）。買單厚不等於有人往上推。"
    else:
        verdict = (f"反彈條件未成立：Overall {overall_pub}、BTC LIQ {st['btc_liq']}（估 {e_now:.2f}，門檻 3）。"
                   if e_now is not None else f"反彈條件未成立：Overall {overall_pub}、BTC LIQ {st['btc_liq']}。")
        if green and not btc_on:
            verdict = f"公布的綠色箭頭亮著，但即時估計已連續 {CONFIRM_H} 小時 < {OFF_BTC:g}（估 {e_now:.2f}），下一張表（{tw(next_U)}）很可能轉 Hold。"
        elif overall_pub == "Buy":
            why.append("這組「大盤買單厚、BTC 自己不夠厚」歷史上之後 3 天 +0.4%、一週 −1.1%，61% 一週內再跌 5% 以上。")
            if e_now is not None:
                why.append(f"估計連續 {CONFIRM_H} 小時 ≥ {ON_BTC:g} 才算亮起；下一張公布 {tw(next_U)}。")
    if green_est and not overall_bad:
        strong = []
        if tot == 1:
            strong.append("全市場掛單 ≥ 3,000 BTC（這組合歷史 72 小時中位數 +2.5%、68% 上漲、一週 +3.0%）")
        if stat_f == "Buy":
            strong.append("資金費率年化 ≤ 5%（歷史 72 小時 +1.9%、68% 上漲）")
        why.append("加分項：" + "；".join(strong) + "。" if strong else "沒有加分項（全市場掛單未達 3,000 BTC、資金費率不低）。")
    if not stopped and not overall_bad:
        why.append(f"價格還在 24 小時低點（{lo24:,.0f}）附近。")
    if ev and H:
        g, gi = H["green"].get(f"fresh_{ev['zone']}"), H["green"].get("fresh_in")
        if (green or green_est) and g and gi and "14d" in g and "14d" in gi:
            if ev["zone"] == "in":
                why.append(f"ETF 淨流入中亮綠燈：歷史剛亮起 {g['n']} 次，14 天中位數 {g['14d']['median']:+.1%}、{g['14d']['up']:.0%} 上漲，是綠色箭頭最好的環境。")
            else:
                why.append(f"但 ETF 30 天是{E.ZONE_TW[ev['zone']]}：這時剛亮起的綠色箭頭歷史 {g['n']} 次，14 天中位數 {g['14d']['median']:+.1%}、{g['14d']['up']:.0%} 上漲"
                           f"（ETF 淨流入時 {gi['14d']['median']:+.1%}、{gi['14d']['up']:.0%}），買單撐得住的機率明顯較低。")
        cyc = H["cycles"]
        if ev["zone"] == "red" and cyc:
            lows, dl, df, fv = ([c[k] for c in cyc] for k in ("low_pct", "days_to_low", "days_to_flip", "flip_vs_low"))
            red_h = C.hidx(E.settled_at(ev["since"]).strftime("%Y-%m-%dT%H"))
            p0 = closes.get(red_h)
            since_px = [closes[h] for h in closes if h >= red_h]
            cur = (f"這輪 {md(ev['since'])} 進紅區（{p0:,.0f}），至今最低 {min(since_px):,.0f}（{min(since_px) / p0 - 1:+.1%}），現在 {px / p0 - 1:+.1%}。"
                   if p0 and since_px else "")
            why.append(f"ETF 資金面：紅區第 {ev['days'] + 1} 天。過去 {len(cyc)} 輪進紅區後，BTC 從進紅區的價格再跌 {-max(lows):.0%}–{-min(lows):.0%} 才見底"
                       f"（{min(dl):.0f}–{max(dl):.0f} 天後），翻回淨流入是在進紅區 {min(df):.0f}–{max(df):.0f} 天後、已離低點 {min(fv):.0%}–{max(fv):.0%}。" + cur)
    lines.append("判斷：" + verdict + "".join(why))
    # ------------------------------------------------------------ how good the hourly estimate has been
    sk = skill(rows, P, x_btc, "btc_liq", "btc_liq", ON_BTC)
    if sk[1] >= 8:
        lines.append(f"準確度：同一小時，我們的估計與 CoinKarma 公布的 BTC LIQ 狀態一致 {sk[0]}/{sk[1]} 張（11/4 起；只假設跟上一張一樣會對 {sk[2]}/{sk[1]}）。")
    # ------------------------------------------------------------ freshness
    age_b = (now - dt.datetime.combine(dt.date.fromordinal(C.EPOCH + hb // 24), dt.time(hb % 24), dt.timezone.utc)).total_seconds() / 3600 - 1
    age_t = (now - dt.datetime.combine(dt.date.fromordinal(C.EPOCH + U // 24), dt.time(U % 24), dt.timezone.utc)).total_seconds() / 3600
    stale = []
    if age_b > 2.5:
        stale.append(f"現貨訂單簿最新只到 {tw(hb)}（{age_b:.0f} 小時前）")
    if age_t > 9.5:
        stale.append(f"CoinKarma 的表最新只到 {tw(U)}（{age_t:.0f} 小時前）")
    if stale:
        lines.append("注意：" + "；".join(stale) + "。")
    lines.append("歷史統計，不是投資建議。")
    # ------------------------------------------------------------ the head (the scheduled task relays the report only on a change)
    head = "【狀態改變】" + "；".join(events) if events else ("【初始化】" if not prev else "【無變化】")
    brief = (f"判斷「{tier}」；綠色箭頭 公布{'亮' if green else '熄'}／估{'亮' if green_est else '熄'}；估 BTC LIQ {e_now:+.2f}" if e_now is not None else f"判斷「{tier}」") \
        + (f"、全市場掛單 {t_now:,.0f} BTC" if t_now is not None else "") \
        + f"；公布（{tw(U)}）Overall {v['overall_liq']:.2f} {overall_pub}、BTC LIQ {v['btc_liq']:.2f} {st['btc_liq']}" \
        + (f"；ETF 30D {ev['v']:+.1f}K（{ {'in': '淨流入', 'out': '淨流出', 'red': '紅區'}[ev['zone']]}）" if ev else "") \
        + f"；BTC {px:,.0f}"
    print("\n".join([head, "摘要：" + brief] + lines))
    with open(STATE, "w") as f:
        json.dump(state, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
