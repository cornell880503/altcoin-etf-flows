#!/usr/bin/env python3
"""A watch for a BTC rebound from the LIQ angle, printed as a short report in Chinese. The collector job runs it
every hour (research/liq_watch_latest.txt); the scheduled task "LIQ 監看" reads that file every hour and relays the whole report only when the first line says the state changed, else one line. The state is
our hourly estimate (Binance spot books on CoinKarma's scale, with hysteresis) combined with the published
Overall LIQ status: the verdict tier, the estimated green arrow on or off, Overall LIQ into or out of
Caution/Sell, Overall Liquidity crossing +-3,000 BTC. The last hour's state is kept in
research/liq_watch_state.json.

What it reads (all in the repo, kept current by the workflows):
- CoinKarma's published tables (data/ck/pulse/pulse.csv, three a day at 00/08/16 UTC): Overall LIQ, BTC LIQ,
  Accumulated BTC LIQ, Overall Liquidity in BTC, Pulse Index and their statuses;
- our Binance spot order books within 10 % of the price, hourly means (data/ck/live/binspot, every 10 s);
- the other venues' books every 10 minutes and OKX's BTC closes (data/ck/live/hours).

The estimate of CoinKarma's BTC LIQ "now": their BTC LIQ against our log(bids / asks) within 10 % at their update
hours (our candle U-1), fitted a + b x on the overlap since the collector's BTC book filled in (2026-10-31 10:00
UTC); the same for their Overall Liquidity in BTC against our all-coin 10 % difference in BTC. Their Overall LIQ
cannot be estimated well from our books (r 0.17 on the overlap) and is taken from the table only.

The verdict applies the historical results of research/ck_pulse_backtest.py and the session's analysis of
2026-11-04 (CoinKarma's own values 2025-02 .. 2026-11, BTC's return over the next 72 h / 7 d):
- green arrow published (Overall and BTC LIQ both Buy): 72 h median +1.2 %, 62-66 % up; after a dip (>= 5 % below
  the 7-day high) and the arrow fresh: 7 d 60 % up, a further 5 % fall within a week in 25-32 % of cases;
- green arrow re-lit 2-7 days after a spell that failed, with the price below that spell's lows: 10 cases, 72 h
  median -1.6 %, 60 % fell 5 % or more within a week;
- dip with Overall Buy but BTC LIQ not Buy: 72 h +0.4 %, 7 d -1.1 % (41 % up), 61 % fell 5 % or more within a week;
- Overall LIQ Caution / Sell: the next 3 days weak (Sell: 72 h -0.6 %).
"""
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
import ck_liq as C  # noqa: E402

STATE = os.path.join(ROOT, "research", "liq_watch_state.json")  # the last table's state, to report changes
FIT_FROM = C.hidx("2026-10-31T10")  # the collector's BTC book had filled in (a day after it started)
UTC8 = dt.timedelta(hours=8)


def tw(h):
    """Our hour index (candle open, UTC) -> 'M/D HH:00' Taiwan time of the candle's open."""
    t = dt.datetime.combine(dt.date.fromordinal(C.EPOCH + h // 24), dt.time(h % 24)) + UTC8
    return f"{t.month}/{t.day} {t.hour:02d}:00"


def tw_update(U):
    """A table's update hour U (UTC) in Taiwan time."""
    return tw(U)


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


def main():
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    P = C.ck_pulse()
    rows = {C.hidx(h): d for h, d in bws.read_hours().items()}
    L = live_rows()
    # ------------------------------------------------------------ prices (OKX hourly closes)
    closes = {h: float(d["px:BTC"][3]) for h, d in L.items() if "px:BTC" in d and d["px:BTC"][3]}
    hc = max(closes)
    px = closes[hc]
    last24 = [closes[h] for h in range(hc - 23, hc + 1) if h in closes]
    wk = [closes[h] for h in range(hc - 167, hc + 1) if h in closes]
    lo24, hi7, lo7 = min(last24), max(wk), min(wk)
    chg24 = px / closes[hc - 24] - 1 if hc - 24 in closes else float("nan")
    mid_now = rows.get(max(rows), {}).get("BTC", {}).get("mid")
    # ------------------------------------------------------------ CoinKarma's latest table
    hs = P["hours"]
    j = len(hs) - 1
    U = int(hs[j])
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
    prev = {k: P["v"][k][j - 1] for k in P["v"]}
    # a green spell that ended in the last 7 days, and whether the price is below its lows
    spell_js = [k for k in range(j, -1, -1) if hs[j] - hs[k] <= 7 * 24 and P["st"]["overall_liq"][k] == "Buy" and P["st"]["btc_liq"][k] == "Buy"]
    failed_below = False
    if spell_js and not all(P["st"]["overall_liq"][k] == "Buy" and P["st"]["btc_liq"][k] == "Buy" for k in range(min(spell_js), j + 1)):
        spell_px = [closes[int(hs[k]) - 1] for k in spell_js if int(hs[k]) - 1 in closes]
        failed_below = bool(spell_px) and px < min(spell_px)
    # ------------------------------------------------------------ the estimate from our spot books
    def xb(h):
        r = rows.get(h, {}).get("BTC")
        return math.log(r["bidq10"] / r["askq10"]) if r and r.get("bidq10") and r.get("askq10") else None

    def xt(h):
        d = rows.get(h, {})
        cs = [c for c in C.COINS if c != "ZEC" and c in d and d[c].get("bidn10") is not None and d[c].get("askn10") is not None]
        m = d.get("BTC", {}).get("mid")
        return sum(d[c]["bidn10"] - d[c]["askn10"] for c in cs) / m if len(cs) >= 20 and m else None

    fits = {}
    for key, fx, ck in (("btc", xb, "btc_liq"), ("tot", xt, "overall_liquidity_btc")):
        pts = [(fx(int(h) - 1), P["v"][ck][k]) for k, h in enumerate(hs) if int(h) - 1 >= FIT_FROM]
        pts = [(x, y) for x, y in pts if x is not None and np.isfinite(y)]
        if len(pts) >= 6:
            x, y = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
            b, a = np.polyfit(x, y, 1)
            fits[key] = (a, b, float((y - (a + b * x)).std()), len(pts), float(np.corrcoef(x, y)[0, 1]))
    hb = max(h for h in rows if rows[h].get("BTC"))
    est = []
    for h in range(hb - 2, hb + 1):
        r = rows.get(h, {}).get("BTC")
        if not r or "btc" not in fits:
            continue
        x = xb(h)
        e = fits["btc"][0] + fits["btc"][1] * x if x is not None else None
        t = xt(h)
        et = fits["tot"][0] + fits["tot"][1] * t if t is not None and "tot" in fits else None
        est.append((h, r["mid"], r["bidn10"] / 1e6, r["askn10"] / 1e6, math.exp(x) if x is not None else None, e, et))
    e_now = est[-1][5] if est else None
    e_prev = est[-2][5] if len(est) > 1 else None
    # other venues: the latest hour's ratios against the hour before
    hv = max(h for h, d in L.items() if "okx:BTC" in d)
    venues = []
    for key, name in (("okx:BTC", "OKX 合約"), ("cb:BTC", "Coinbase"), ("hl:BTC", "Hyperliquid")):
        r1, r0 = ratio(L.get(hv, {}).get(key)), ratio(L.get(hv - 1, {}).get(key))
        if r1:
            venues.append(f"{name} {r1:.2f}" + (f"（前一小時 {r0:.2f}）" if r0 else ""))
    # ------------------------------------------------------------ the report
    t_tw = now + UTC8
    lines.append(f"LIQ 監看 {t_tw.month}/{t_tw.day} {t_tw.hour:02d}:{t_tw.minute:02d}（台灣時間）")
    lines.append(f"BTC {px:,.0f}（{tw(hc)} 收盤" + (f"；最新均價 {mid_now:,.0f}" if mid_now else "") + f"）｜24 小時 {chg24:+.1%}｜距 7 天高點 {px / hi7 - 1:+.1%}、距 7 天低點 {px / lo7 - 1:+.1%}")
    s = lambda k: st[k] or "?"  # noqa: E731
    lines.append(f"CoinKarma 公布（{tw_update(U)}）：Overall {v['overall_liq']:.2f} {st['overall_liq']}、BTC LIQ {v['btc_liq']:.2f} {st['btc_liq']}"
                 f"（上一張 {prev['overall_liq']:.2f}／{prev['btc_liq']:.2f}）、累積 {v['acc_btc_liq']:.0f} {s('acc_btc_liq')}、"
                 f"全市場掛單 {v['overall_liquidity_btc']:,.0f} BTC {s('overall_liquidity_btc')}、Pulse {v['pulse_index']:.0f} {s('pulse_index')}；"
                 f"綠色箭頭{'亮著' if green else '沒亮'}")
    if est:
        e_txt = "、".join(f"{tw(h)[-5:]} {e:+.2f}" if e is not None else f"{tw(h)[-5:]} —" for h, _, _, _, _, e, _ in est)
        h, mid, bb, aa, rr, e, et = est[-1]
        lines.append(f"即時估計（我們的 Binance 現貨 ±10%）：BTC LIQ 估 {e_txt}（誤差約 ±{fits['btc'][2]:.1f}，門檻 3）；"
                     f"買單 {bb:.0f}M／賣單 {aa:.0f}M（{rr:.2f} 倍）" + (f"；全市場掛單估 {et:,.0f} BTC（門檻 3,000）" if et is not None else ""))
    if venues:
        lines.append(f"其他交易所 ±5% 買賣比（{tw(hv)} 起這小時）：" + "、".join(venues))
    # ------------------------------------------------------------ the hourly state (our estimates, with hysteresis)
    # The published table is 8 hours apart; between tables the state follows our Binance spot books mapped onto
    # CoinKarma's scale. A flag turns on above its threshold and off only below threshold - 10 % (so one noisy hour
    # does not flip it); a fresh table overrides the flags with the published status.
    prev = {}
    try:
        with open(STATE) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass
    fresh_table = prev.get("table") != int(U)
    et_now = est[-1][6] if est else None
    btc_on = bool(prev.get("btc_on", False))
    if fresh_table:
        btc_on = st["btc_liq"] == "Buy"
    if e_now is not None:
        btc_on = True if e_now >= 3.0 else False if e_now < 2.7 else btc_on
    ovliq = prev.get("ovliq", "Hold")
    if fresh_table:
        ovliq = st["overall_liquidity_btc"] or "Hold"
    if et_now is not None:
        ovliq = "Buy" if et_now >= 3000 else "Sell" if et_now <= -3000 else ("Hold" if -2700 < et_now < 2700 else ovliq)
    overall_pub = st["overall_liq"] or "Hold"
    overall_bad = overall_pub in ("Caution", "Sell")
    green_est = overall_pub == "Buy" and btc_on  # the green arrow as our hourly estimate sees it
    stopped = px >= lo24 * 1.01
    if overall_bad:
        tier = "買單被吃"
    elif green_est and stopped and not failed_below:
        tier = "成立"
    elif green_est:
        tier = "部分成立"
    else:
        tier = "未成立"
    # ------------------------------------------------------------ the verdict
    why = []
    src = "（公布）" if green else "（即時估計，公布值還沒跟上）"
    if tier == "買單被吃":
        verdict = f"反彈條件未成立：Overall LIQ 轉 {overall_pub}，買單在被吃掉；這種狀態歷史上之後 3 天偏弱。"
    elif tier == "成立":
        verdict = f"反彈條件成立{src}：Overall ≥ 2 且 BTC LIQ ≥ 3，價格也止跌了。歷史上之後 72 小時中位數 +1.2%、62–66% 上漲；一週內再跌 5% 的比例 25–32%。"
    elif tier == "部分成立" and failed_below:
        verdict = f"部分成立{src}：綠色箭頭亮著，但這是上一段綠燈失敗、價格跌破那段低點之後再亮。歷史 10 次，72 小時中位數 −1.6%，60% 一週內再跌 5% 以上。"
    elif tier == "部分成立":
        verdict = f"部分成立{src}：綠色箭頭亮著，但價格還沒止跌（仍在 24 小時低點 1% 內）。買單厚不等於有人往上推。"
    else:
        verdict = (f"反彈條件未成立：Overall {overall_pub}、BTC LIQ {st['btc_liq']}（估 {e_now:.1f}，門檻 3）。"
                   if e_now is not None else f"反彈條件未成立：Overall {overall_pub}、BTC LIQ {st['btc_liq']}。")
        if overall_pub == "Buy":
            why.append("這組「大盤買單厚、BTC 自己不夠厚」歷史上之後 3 天 +0.4%、一週 −1.1%，61% 一週內再跌 5% 以上。")
    if green_est and not overall_bad:
        strong = []
        if ovliq == "Buy":
            strong.append("全市場掛單 ≥ 3,000 BTC（這組合歷史 72 小時中位數 +2.5%、68% 上漲、一週 +3.0%）")
        if stat_f == "Buy":
            strong.append("資金費率年化 ≤ 5%（歷史 72 小時 +1.9%、68% 上漲）")
        why.append("加分項：" + "；".join(strong) + "。" if strong else "沒有加分項（全市場掛單未達 3,000 BTC、資金費率不低）。")
    if not stopped and not overall_bad:
        why.append(f"價格還在 24 小時低點（{lo24:,.0f}）附近。")
    if e_now is not None and e_prev is not None:
        why.append(f"BTC LIQ 估計比上一小時{'升' if e_now > e_prev else '降'} {abs(e_now - e_prev):.2f}。")
    lines.append("判斷：" + verdict + ("" if not why else "".join(why)))
    # ------------------------------------------------------------ freshness
    age_b = (now - dt.datetime.combine(dt.date.fromordinal(C.EPOCH + hb // 24), dt.time(hb % 24), dt.timezone.utc)).total_seconds() / 3600 - 1
    age_t = (now - dt.datetime.combine(dt.date.fromordinal(C.EPOCH + U // 24), dt.time(U % 24), dt.timezone.utc)).total_seconds() / 3600
    stale = []
    if age_b > 2.5:
        stale.append(f"現貨訂單簿最新只到 {tw(hb)}（{age_b:.0f} 小時前）")
    if age_t > 9.5:
        stale.append(f"CoinKarma 的表最新只到 {tw_update(U)}（{age_t:.0f} 小時前）")
    if stale:
        lines.append("注意：" + "；".join(stale) + "。")
    lines.append("歷史統計，不是投資建議。")
    # ------------------------------------------------------------ state changes (the scheduled task relays the report only then)
    state = {"table": int(U), "hour": int(hb), "green": bool(green), "btc_on": btc_on, "green_est": green_est, "overall": overall_pub,
             "ovliq": ovliq, "tier": tier, "stopped": bool(stopped)}
    events = []
    if prev:
        if state["tier"] != prev.get("tier"):
            events.append(f"判斷從「{prev.get('tier')}」變成「{tier}」")
        if state["green_est"] != prev.get("green_est"):
            events.append("綠色箭頭（即時估計）" + ("亮起" if green_est else "熄滅"))
        if state["green"] != prev.get("green"):
            events.append("CoinKarma 公布的綠色箭頭" + ("亮起" if green else "熄滅"))
        was_bad = prev.get("overall") in ("Caution", "Sell")
        if overall_bad and (not was_bad or prev.get("overall") != overall_pub):
            events.append(f"Overall LIQ 轉 {overall_pub}（買單被吃）")
        elif was_bad and not overall_bad:
            events.append(f"Overall LIQ 離開 {prev.get('overall')}，回到 {overall_pub}")
        if ovliq != prev.get("ovliq") and (ovliq in ("Buy", "Sell") or prev.get("ovliq") in ("Buy", "Sell")):
            events.append({"Buy": "全市場掛單 ≥ 3,000 BTC", "Sell": "全市場掛單 ≤ −3,000 BTC"}.get(ovliq, "全市場掛單回到 ±3,000 內"))
    head = "【狀態改變】" + "；".join(events) if events else ("【初始化】" if not prev else "【無變化】")
    brief = (f"判斷「{tier}」；估 BTC LIQ {e_now:+.1f}" + (f"、全市場掛單 {et_now:,.0f} BTC" if et_now is not None else "")
             + f"；公布（{tw_update(U)}）Overall {v['overall_liq']:.2f} {overall_pub}、BTC LIQ {v['btc_liq']:.2f} {st['btc_liq']}；BTC {px:,.0f}")
    print("\n".join([head, "摘要：" + brief] + lines))
    with open(STATE, "w") as f:
        json.dump(state, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
