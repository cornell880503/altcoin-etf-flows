#!/usr/bin/env python3
"""The indicator library: open copies of CoinKarma's indicators (docs.coinkarma.co/english/
indicators-guide), computed from free data (scripts/fetch_ck.py, data/flows, data/oi), with
CoinKarma's documented thresholds and, where an indicator gives signals, what BTC did afterwards.
Writes data/ck/indicators.json for the site's 指標庫 tab.

Each item says how close it is to CoinKarma's version:
  close     same inputs and rule as documented
  proxy     same idea, a substitute input (named in "diff")
  ours      CoinKarma does not publish the formula; this is our construction of the stated idea
  forward   no free history: recorded from 2026-10 on
"""
import bisect
import datetime as dt
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oi_store as store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "ck", "raw")
OUT = os.path.join(ROOT, "data", "ck", "indicators.json")
CHART_FROM = "2021-01-01"
DAILY_FROM_DAYS = 420  # the last ~14 months daily, weekly before that (keeps the page small)


def s2d(s):
    return dt.date.fromisoformat(s)


def d2s(d):
    return d.isoformat()


def shift(s, k):
    return d2s(s2d(s) + dt.timedelta(days=k))


def raw(name):
    try:
        j = json.load(open(os.path.join(RAW, f"{name}.json")))
    except (OSError, ValueError):
        return {}
    f = j.get("fields", [])
    return {r[0]: dict(zip(f, r)) for r in j.get("rows", [])}


def r2(v, k=2):
    return None if v is None or v != v else round(v, k)


# ------------------------------------------------------------------ BTC price
def btc_close():
    """BTC daily close (UTC day): Coin Metrics (complete days, from 2021-06), Binance spot BTCUSDT where
    the basis data has it (from 2020-08), and the site's own 00:00 UTC close for days still missing."""
    px = {}
    for r in store.read_all(os.path.join(ROOT, "data", "oi", "cm")):
        if r[1] == "btc" and r[3]:
            px[r[0]] = float(r[3])
    for d, r in raw("basis").items():
        if r.get("spot"):
            px[d] = r["spot"]
    try:
        rows = json.load(open(os.path.join(ROOT, "data", "coin_px", "BTC.json")))["rows"]
        for d, v in rows.items():
            if v and len(v) >= 3 and v[2]:
                px.setdefault(d, v[2])
    except (OSError, ValueError, KeyError):
        pass
    return px


BTC = {}
DAILY = {}  # daily series other items reuse (the market-pulse composite): name -> {date: value}


def fwd(d, H):
    """BTC over the H days after day d's close: return, max drawdown, max rise (fractions)."""
    b = BTC.get(d)
    path = [BTC.get(shift(d, k)) for k in range(1, H + 1)]
    if not b or any(v is None for v in path):
        return None
    return {"ret": path[-1] / b - 1, "dd": min(0.0, min(path) / b - 1), "up": max(0.0, max(path) / b - 1)}


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def event_stats(days, H=30, lo=None):
    """What BTC did in the H days after each event day, against every day from `lo` (the start of
    the indicator's data) on."""
    lo = lo or min(days, default=None)
    allo = [fwd(d, H) for d in sorted(BTC) if lo and d >= lo]
    allo = [o for o in allo if o]
    ev = [fwd(d, H) for d in days]
    ev = [o for o in ev if o]
    if not ev or not allo:
        return None
    return {"n": len(ev), "h": H,
            "ret": r2(med(o["ret"] for o in ev) * 100, 1), "up_share": r2(sum(o["ret"] > 0 for o in ev) / len(ev) * 100, 0),
            "dd": r2(med(o["dd"] for o in ev) * 100, 1), "upmax": r2(med(o["up"] for o in ev) * 100, 1),
            "base_ret": r2(med(o["ret"] for o in allo) * 100, 1), "base_up_share": r2(sum(o["ret"] > 0 for o in allo) / len(allo) * 100, 0),
            "base_dd": r2(med(o["dd"] for o in allo) * 100, 1), "base_upmax": r2(med(o["up"] for o in allo) * 100, 1)}


def first_days(flags, gap=14):
    """Event days: a day the condition holds after at least `gap` days without it."""
    out, last = [], None
    for d in sorted(flags):
        if flags[d]:
            if last is None or (s2d(d) - s2d(last)).days > gap:
                out.append(d)
            last = d
    return out


def pack(dates, cols, decimals=None):
    """Chart series: daily for the last DAILY_FROM_DAYS days, the last value of each week before that."""
    if not dates:
        return {"d": []}
    cut = shift(dates[-1], -DAILY_FROM_DAYS)
    keep = []
    for i, d in enumerate(dates):
        if d < CHART_FROM:
            continue
        if d >= cut or i == len(dates) - 1 or s2d(dates[i + 1]).isocalendar()[1] != s2d(d).isocalendar()[1]:
            keep.append(i)
    out = {"d": [dates[i] for i in keep]}
    for k, vals in cols.items():
        dec = (decimals or {}).get(k, 4)
        out[k] = [r2(vals[i], dec) if vals[i] is not None else None for i in keep]
    out["btc"] = [r2(BTC.get(out["d"][j]), 0) for j in range(len(out["d"]))]
    return out


def now_of(dates, vals):
    for d, v in zip(reversed(dates), reversed(vals)):
        if v is not None:
            return d, v
    return None, None


def item(**kw):
    kw["marks"] = [m for m in kw.get("marks", []) if m["d"] >= CHART_FROM]
    kw.setdefault("zones", [])
    kw.setdefault("stats", [])
    return kw


# ------------------------------------------------------------------ ETF
def etf_v30():
    """US spot BTC ETFs' net flow over the last 30 calendar days, in thousands of BTC, by day."""
    try:
        rows = json.load(open(os.path.join(ROOT, "data", "flows", "BTC.json")))["rows"]
    except (OSError, ValueError, KeyError):
        return {}
    days = sorted((r["date"], r) for r in rows if r.get("total") is not None)
    if not days:
        return {}
    flow = {d: r["total"] for d, r in days}
    price = {d: r.get("btcPrice") or BTC.get(d) for d, r in days}
    cal, d, end = [], s2d(days[0][0]), s2d(days[-1][0])
    while d <= end:
        cal.append(d2s(d))
        d += dt.timedelta(days=1)
    bf = [(flow[x] * 1e6 / price[x]) if x in flow and price.get(x) else 0.0 for x in cal]
    return {cal[i]: sum(bf[i - 29:i + 1]) / 1000 for i in range(29, len(cal))}


def etf_items():
    try:
        rows = json.load(open(os.path.join(ROOT, "data", "flows", "BTC.json")))["rows"]
    except (OSError, ValueError, KeyError):
        return []
    days = sorted((r["date"], r) for r in rows if r.get("total") is not None)
    flow = {d: r["total"] for d, r in days}
    ibit = {d: r.get("blackrock") for d, r in days}
    price = {d: r.get("btcPrice") or BTC.get(d) for d, r in days}
    out = []
    # 30-day net flow in BTC (calendar days: about 21 trading days)
    cal = []
    d = s2d(days[0][0])
    end = s2d(days[-1][0])
    while d <= end:
        cal.append(d2s(d))
        d += dt.timedelta(days=1)
    btc_flow = {d: (flow[d] * 1e6 / price[d]) if d in flow and price.get(d) else 0.0 for d in cal}
    v30 = {}
    for i, d in enumerate(cal):
        if i >= 29:
            v30[d] = sum(btc_flow[cal[j]] for j in range(i - 29, i + 1)) / 1000  # thousands of BTC
    ds = sorted(v30)
    green = {d: v30[d] >= 60 for d in ds}
    red = {d: v30[d] <= -20 for d in ds}
    # CoinKarma's statistics: BTC's daily change compounded over the green days and over the red days
    # (the day's own close-to-close move; "two days late" uses the label of two days before), and the
    # all-time-high closes that fell on green days. Also over CoinKarma's own span (2024-02-10 to 2026-09-18).
    lr = lambda d: math.log(BTC[d] / BTC[shift(d, -1)]) if BTC.get(d) and BTC.get(shift(d, -1)) else 0.0  # noqa: E731
    def cum(flag, lag=0, lo="0", hi="9"):
        return r2((math.exp(sum(lr(d) for d in ds if lo <= d <= hi and flag.get(shift(d, -lag)))) - 1) * 100, 0)
    ath, top = set(), 0
    for d in sorted(BTC):
        if BTC[d] > top:
            top = BTC[d]
            if d >= ds[0]:
                ath.add(d)
    def block(lo, hi):
        span = [d for d in ds if lo <= d <= hi]
        a = [d for d in ath if lo <= d <= hi]
        return {"days": len(span), "green": sum(green[d] for d in span), "red": sum(red[d] for d in span),
                "g": cum(green, 0, lo, hi), "r": cum(red, 0, lo, hi), "g2": cum(green, 2, lo, hi), "r2": cum(red, 2, lo, hi),
                "ath": len(a), "ath_green": sum(1 for d in a if green.get(d)),
                "move": r2((BTC[span[-1]] / BTC[span[0]] - 1) * 100, 0) if span and BTC.get(span[0]) and BTC.get(span[-1]) else None}
    ckp, allp = block("2024-02-10", "2026-09-18"), block(ds[0], ds[-1])
    stats = [{"k": "期間", "ck": "2024/02/10–2026/09/18（952 天）", "ours": f"同期間（{ckp['days']} 天）", "all": f"{ds[0]}–{ds[-1]}（{allp['days']} 天）"},
             {"k": "綠色天數", "ck": "145 天（15%）", "ours": f"{ckp['green']} 天", "all": f"{allp['green']} 天"},
             {"k": "紅色天數", "ck": "154 天", "ours": f"{ckp['red']} 天", "all": f"{allp['red']} 天"},
             {"k": "BTC 在綠色日子累計", "ck": "+83%", "ours": f"{ckp['g']:+.0f}%", "all": f"{allp['g']:+.0f}%"},
             {"k": "BTC 在紅色日子累計", "ck": "−40%", "ours": f"{ckp['r']:+.0f}%", "all": f"{allp['r']:+.0f}%"},
             {"k": "晚兩天才行動：綠／紅", "ck": "+77% / −37%", "ours": f"{ckp['g2']:+.0f}% / {ckp['r2']:+.0f}%", "all": f"{allp['g2']:+.0f}% / {allp['r2']:+.0f}%"},
             {"k": "收盤創新高的日子落在綠色", "ck": "35 / 40 天", "ours": f"{ckp['ath_green']} / {ckp['ath']} 天", "all": f"{allp['ath_green']} / {allp['ath']} 天"},
             {"k": "整段期間 BTC", "ck": "+69%", "ours": f"{ckp['move']:+.0f}%" if ckp['move'] is not None else "—", "all": f"{allp['move']:+.0f}%" if allp['move'] is not None else "—"}]
    dn, vn = now_of(ds, [v30[d] for d in ds])
    out.append(item(id="etf30d", group="ETF 與機構", name="BTC ETF 30D Net Flow", zh="BTC ETF 30 日淨流量", fidelity="close",
                    what="美國現貨 BTC ETF 過去 30 天合計淨買進（＋）或淨賣出（−）了多少顆 BTC。",
                    ck="≥ 6 萬顆為大量流入（綠），≤ −2 萬顆為持續流出（紅）；綠色多半是主升段，紅色不加槓桿。門檻約為 2024/02 以來的第 85、15 百分位。",
                    ours="每天的總淨流量（美元）用當天 BTC 價格換算成顆數，加總過去 30 個日曆天。",
                    diff="資料和算法與 CoinKarma 相同；流量來自 cryptoetf.today。", unit="千顆 BTC", dec=1,
                    lines=[{"k": "v", "label": "30 日淨流量（千顆）", "bar": True}], series=pack(ds, {"v": [v30[d] for d in ds]}, {"v": 1}),
                    zones=[{"from": 60, "to": None, "tone": "good", "label": "大量流入"}, {"from": None, "to": -20, "tone": "bad", "label": "持續流出"}],
                    now={"d": dn, "v": r2(vn, 1), "state": "大量流入" if vn >= 60 else "持續流出" if vn <= -20 else "中性",
                         "tone": "good" if vn >= 60 else "bad" if vn <= -20 else "mid"},
                    stats=stats, statsKind="compare", src="cryptoetf.today（每日 ETF 淨流量與 BTC 價格）"))
    # daily net flow, IBIT separately; CoinKarma: IBIT outflows or a halt in inflows often near short-term bottoms
    ds2 = [d for d, _ in days]
    ib5, v5 = {}, {}
    for i, d in enumerate(ds2):
        xs = [ibit[ds2[j]] for j in range(max(0, i - 4), i + 1) if ibit.get(ds2[j]) is not None]
        ib5[d] = sum(xs) if len(xs) == 5 else None
        v5[d] = sum(flow[ds2[j]] for j in range(i - 4, i + 1)) if i >= 4 else None
    ev = first_days({d: (v is not None and v < 0) for d, v in ib5.items()}, gap=20)
    st = event_stats(ev, 30, lo=ds2[0])
    dn, vn = now_of(ds2, [flow[d] for d in ds2])
    out.append(item(id="etfflow", group="ETF 與機構", name="BTC ETF Net Flow", zh="BTC ETF 每日淨流量（IBIT 單看）", fidelity="close",
                    what="美國現貨 BTC ETF 每天的淨流入流出（美元）。圖上畫過去 5 個交易日的合計（全部 ETF 與 IBIT 單看），目前值是最新一天。",
                    ck="IBIT 出現淨流出或停止流入時，常落在短線底部；套利盤（買 ETF、空 CME）讓流入多半是期貨情緒，不是直接的方向。",
                    ours="標出 IBIT 5 日合計轉為淨流出的第一天（前 20 天內沒有出現過），看之後 30 天 BTC 的表現。",
                    diff="資料相同；「停止流入」沒有明確定義，這裡用 5 日合計 < 0。", unit="百萬美元", dec=0,
                    lines=[{"k": "v5", "label": "全部 ETF 5 日合計（百萬美元）", "bar": True}, {"k": "ib5", "label": "IBIT 5 日合計"}],
                    series=pack(ds2, {"v5": [v5[d] for d in ds2], "ib5": [ib5[d] for d in ds2]}, {"v5": 0, "ib5": 0}),
                    marks=[{"d": d, "label": "IBIT 轉淨流出"} for d in ev],
                    now={"d": dn, "v": r2(vn, 0), "state": f"IBIT 5 日 {r2(ib5.get(dn), 0)}", "tone": "mid"},
                    stats=[st] if st else [], src="cryptoetf.today"))
    return out


# ------------------------------------------------------------------ stablecoins
def stablecoin_items():
    s = raw("stablecoins")
    ds = sorted(d for d in s if s[d].get("usdt_usd") and s[d].get("usdc_usd"))
    if len(ds) < 100:
        return []
    usdt = [s[d]["usdt_usd"] / 1e9 for d in ds]
    usdc = [s[d]["usdc_usd"] / 1e9 for d in ds]
    iss = {ds[i]: (usdc[i] - usdc[i - 1]) for i in range(1, len(ds))}
    # CoinKarma: USDC issued against the price move (BTC down) may mark a bottom; > $1B issuance with
    # Overall LIQ green often makes a short-term range bottom (LIQ: see the LIQ items)
    ev = first_days({d: v >= 1.0 and BTC.get(d) and BTC.get(shift(d, -7)) and BTC[d] < BTC[shift(d, -7)] for d, v in iss.items()}, gap=10)
    st = event_stats(ev, 30, lo=ds[0])
    dn = ds[-1]
    t30 = usdt[-1] - usdt[-31] if len(usdt) > 31 else None
    return [item(id="stables", group="資金流", name="Stablecoin Supply", zh="穩定幣供給（USDT、USDC）", fidelity="close",
                 what="USDT 與 USDC 的流通市值；USDT 增加代表資金進場、流動性變好，減少則相反。",
                 ck="USDT 市值走勢與 BTC 同向（2022 減少、2023–24 增加）；USDC 逆勢增發可能是底部，增發超過 10 億美元又遇到 Overall LIQ 綠柱，常是短線區間底。",
                 ours="DefiLlama 的每日流通量。標出 USDC 單日增發 ≥ 10 億美元、而 BTC 比 7 天前低的日子（逆勢增發），看之後 30 天。",
                 diff="資料相同；Overall LIQ 的條件要等 LIQ 系列做出來才加上。", unit="十億美元", dec=1,
                 lines=[{"k": "usdt", "label": "USDT（十億美元）"}, {"k": "usdc", "label": "USDC"}],
                 series=pack(ds, {"usdt": usdt, "usdc": usdc}, {"usdt": 1, "usdc": 1}),
                 marks=[{"d": d, "label": "USDC 逆勢增發 ≥ 10 億"} for d in ev],
                 now={"d": dn, "v": r2(usdt[-1], 1), "state": f"USDT 30 天 {t30:+.1f}B，USDC {usdc[-1]:.1f}B" if t30 is not None else "",
                      "tone": "good" if (t30 or 0) > 0 else "bad"},
                 stats=[st] if st else [], src="DefiLlama stablecoins")]


def usdc_premium_items():
    s = raw("usdc_usdt")
    ds = sorted(d for d in s if s[d].get("mean"))
    if len(ds) < 100:
        return []
    prem = [(s[d]["mean"] - 1) * 100 for d in ds]  # % ; below 0: USDC cheaper than USDT
    DAILY["usdcprem"] = dict(zip(ds, prem))
    flags = {d: p < 0 for d, p in zip(ds, prem)}
    ev = first_days(flags, gap=14)
    st = event_stats(ev, 30, lo=ds[0])
    dn, vn = ds[-1], prem[-1]
    return [item(id="usdcprem", group="資金流", name="USDC/USDT Premium", zh="USDC/USDT 溢價", fidelity="close",
                 what="USDC 相對 USDT 的價差。大戶先在 BTC/USDT 賣出時，套利把 USDC 換回 USDT，溢價就跌到 0 以下。",
                 ck="沒有外部增量資金時，溢價跌破 0 之後市場常短線見頂。",
                 ours="Binance 現貨 USDCUSDT 每小時收盤的日平均，減 1 換成百分比；標出跌破 0 的第一天（前 14 天沒有），看之後 30 天。",
                 diff="資料來源同為交易所價差；CoinKarma 沒寫交易所與平滑方式。2022/09–2023/03 Binance 下架 USDC，期間沒有資料。",
                 unit="%", dec=3, lines=[{"k": "v", "label": "溢價（%）"}], series=pack(ds, {"v": prem}, {"v": 4}),
                 zones=[{"from": None, "to": 0, "tone": "bad", "label": "負溢價"}],
                 marks=[{"d": d, "label": "跌破 0"} for d in ev],
                 now={"d": dn, "v": r2(vn, 3), "state": "負溢價" if vn < 0 else "正溢價", "tone": "bad" if vn < 0 else "good"},
                 stats=[st] if st else [], src="Binance 現貨 USDCUSDT（data.binance.vision）")]


# ------------------------------------------------------------------ derivatives sentiment
BASIS_ZONES = [(None, 0, "極度悲觀", "bad"), (0, 5, "極度恐慌", "bad"), (5, 10, "中性", "mid"), (10, 15, "微熱", "mid"),
               (15, 20, "狂熱", "warn"), (20, None, "FOMO", "warn")]


def zone_of(v, zones):
    for lo, hi, lab, tone in zones:
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return lab, tone
    return "", "mid"


def basis_items():
    s = raw("basis")
    ds = sorted(d for d in s if s[d].get("apy_pct") is not None)
    if len(ds) < 100:
        return []
    apy = [s[d]["apy_pct"] for d in ds]
    DAILY["basis"] = dict(zip(ds, apy))
    # what BTC did after each zone (30 days), every day counted
    stats = []
    for lo, hi, lab, tone in BASIS_ZONES:
        days = [d for d, v in zip(ds, apy) if (lo is None or v >= lo) and (hi is None or v < hi)]
        ev = event_stats(days, 30, lo=ds[0])
        if ev:
            ev["label"] = f"{lab}（{'< ' + str(hi) if lo is None else '> ' + str(lo) if hi is None else f'{lo}–{hi}'}%）"
            stats.append(ev)
    dn, vn = ds[-1], apy[-1]
    lab, tone = zone_of(vn, BASIS_ZONES)
    return [item(id="basis", group="衍生品情緒", name="Future Basis Rolling APY", zh="季度合約年化基差", fidelity="close",
                 what="BTC 季度交割合約相對現貨的溢價，換算成年化報酬；作用和資金費率類似，越高越樂觀。",
                 ck="< 0% 極度悲觀、0–5% 極度恐慌、5–10% 中性、10–15% 微熱、15–20% 狂熱、> 20% FOMO；現貨 ETF 上市後基差被壓縮，舊門檻可能偏高。",
                 ours="Binance 幣本位最近一季合約（到期前 14 天換下一季）的日收盤 ÷ 現貨 BTCUSDT − 1，× 360 ÷ 剩餘天數。",
                 diff="算法與 CoinKarma 的文件相同；他們沒寫用哪家交易所、何時換月。", unit="%", dec=1,
                 lines=[{"k": "v", "label": "年化基差（%）"}], series=pack(ds, {"v": apy}, {"v": 2}),
                 zones=[{"from": lo, "to": hi, "tone": t, "label": l} for lo, hi, l, t in BASIS_ZONES if t != "mid"],
                 now={"d": dn, "v": r2(vn, 1), "state": lab, "tone": tone},
                 stats=stats, statsKind="zones", src="Binance 季度合約與現貨（data.binance.vision）")]


def oi_btc(venue, sym):
    try:
        rows = store.read_all(os.path.join(ROOT, "data", "oi", venue))
    except Exception:
        return {}
    return {r[0]: float(r[2]) for r in rows if r[1] == sym and r[2]}


def funding_items():
    s = raw("funding")
    ds = sorted(s)
    if len(ds) < 100:
        return []
    w = {"binance_pct8h": oi_btc("binance", "BTCUSDT"), "okx_pct8h": oi_btc("okx", "BTC-USDT-SWAP"),
         "hyperliquid_pct8h": oi_btc("hyperliquid", "BTC")}
    vals, used, est_days = [], [], []
    for d in ds:
        pairs, plain = [], []
        for k, oi in w.items():
            v = s[d].get(k)
            if v is None and k == "binance_pct8h" and s[d].get("binance_est_pct8h") is not None:
                v = s[d]["binance_est_pct8h"]  # the month's file is not out yet: Binance's formula on its premium index
                est_days.append(d)
            if v is None:
                continue
            plain.append(v)
            o = oi.get(d) or oi.get(shift(d, -1))
            if o:
                pairs.append((v, o))
        if pairs:  # OI-weighted over the venues with both numbers
            vals.append(sum(v * o for v, o in pairs) / sum(o for v, o in pairs))
        elif plain:  # before the OI data (2020-21): Binance alone
            vals.append(sum(plain) / len(plain))
        else:
            vals.append(None)
        used.append(len(pairs) or len(plain))
    DAILY["funding"] = {d: v for d, v in zip(ds, vals) if v is not None}
    hot = {d: (v is not None and v >= 0.03) for d, v in zip(ds, vals)}
    neg = {d: (v is not None and v < 0) for d, v in zip(ds, vals)}
    st_hot, st_neg = event_stats(first_days(hot, 14), 30, lo=ds[0]), event_stats(first_days(neg, 14), 30, lo=ds[0])
    for st, lab in ((st_hot, "≥ 0.03%（偏熱）"), (st_neg, "< 0（空方付費）")):
        if st:
            st["label"] = lab
    dn, vn = now_of(ds, vals)
    return [item(id="funding", group="衍生品情緒", name="OI-Weighted Funding Rate", zh="持倉加權資金費率", fidelity="proxy",
                 what="BTC 永續合約的資金費率，依各交易所持倉加權平均；反映多空槓桿的熱度。",
                 ck="上漲中資金費率高是多頭過度延伸；下跌中驟升可能是反轉。現貨 ETF 讓費率被壓縮：以前 0.1% 以上才是極度狂熱，現在 0.05–0.08% 就可能過度槓桿；參考基準 0 與 0.03%。",
                 ours="每 8 小時費率的日平均：Binance BTCUSDT、Hyperliquid BTC（每小時 × 8）、OKX BTC-USDT-SWAP，用各自的 BTC 持倉加權。",
                 diff="CoinKarma 用「主要交易所」；這裡沒有 Bybit。Binance 的費率檔每月初才公布，" + (f"最近 {len(est_days)} 天（{est_days[0]} 起）的 Binance 費率是用它公布的公式、由溢價指數估算。" if est_days else "最近一個月只有 Hyperliquid 和 OKX。"),
                 unit="% / 8h", dec=4, lines=[{"k": "v", "label": "持倉加權費率（%/8h）"}], series=pack(ds, {"v": vals}, {"v": 4}),
                 zones=[{"from": 0.05, "to": None, "tone": "warn", "label": "過度槓桿"}, {"from": None, "to": 0, "tone": "bad", "label": "負費率"}],
                 now={"d": dn, "v": r2(vn, 4), "state": "偏熱" if (vn or 0) >= 0.03 else "負費率" if (vn or 0) < 0 else "中性",
                      "tone": "warn" if (vn or 0) >= 0.03 else "bad" if (vn or 0) < 0 else "mid"},
                 stats=[x for x in (st_hot, st_neg) if x], statsKind="labeled", src="Binance、Hyperliquid、OKX；持倉來自槓桿雷達的資料")]


LSUR_COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "LINK", "AVAX", "SUI", "LTC", "DOT", "TRX", "BCH", "NEAR", "AAVE",
              "UNI", "HYPE", "ZEC", "ARB"]


def lsur_items():
    rows = store.read_all(os.path.join(ROOT, "data", "oi", "binance"))
    by = {}
    for r in rows:
        if r[1].endswith("USDT") and r[1][:-4] in LSUR_COINS and r[5] and r[2]:
            o, v = float(r[2]), float(r[5])
            if o > 0 and v > 0:
                by.setdefault(r[0], []).append((o, v))
    ds = sorted(d for d in by if len(by[d]) >= 8)
    if len(ds) < 60:
        return []
    ls = [sum(o * v for o, v in by[d]) / sum(o for o, v in by[d]) for d in ds]
    DAILY["lsur"] = dict(zip(ds, ls))
    z = []
    for i in range(len(ds)):
        win = ls[max(0, i - 13):i + 1]
        if len(win) < 14:
            z.append(None)
            continue
        m, sd = statistics.mean(win), statistics.pstdev(win)
        z.append((ls[i] - m) / sd if sd > 0 else 0.0)
    ev = first_days({d: (v is not None and v <= -2) for d, v in zip(ds, z)}, gap=10)
    st = event_stats(ev, 14, lo=ds[0])
    st30 = event_stats(ev, 30, lo=ds[0])
    dn, vn = now_of(ds, z)
    return [item(id="lsur", group="衍生品情緒", name="LSUR Z Index", zh="多空人數比 Z 值", fidelity="proxy",
                 what="主流幣永續合約的多空人數比（做多帳戶 ÷ 做空帳戶），依持倉加權，再看偏離 14 天平均幾個標準差。",
                 ck="低於 −2 代表大幅偏空，要搭配 LIQ 確認；過去多空比 < 1 常是短線底，近年關係減弱甚至反轉。",
                 ours="Binance 20 個主流幣 USDT 永續的每日平均多空帳戶比，依當天持倉加權；Z = (今天 − 14 日平均) ÷ 14 日標準差。標出跌破 −2 的第一天。",
                 diff="CoinKarma 可能是每小時、多家交易所；這裡是 Binance、每日。", unit="σ", dec=2,
                 lines=[{"k": "z", "label": "Z 值"}, {"k": "ls", "label": "多空比（右軸）", "right": True}],
                 series=pack(ds, {"z": z, "ls": ls}, {"z": 2, "ls": 3}),
                 zones=[{"from": None, "to": -2, "tone": "good", "label": "大幅偏空"}, {"from": 2, "to": None, "tone": "bad", "label": "大幅偏多"}],
                 marks=[{"d": d, "label": "Z ≤ −2"} for d in ev],
                 now={"d": dn, "v": r2(vn, 2), "state": f"多空比 {ls[-1]:.2f}", "tone": "good" if (vn or 0) <= -2 else "mid"},
                 stats=[x for x in (st, st30) if x], src="Binance 合約公開資料（data.binance.vision metrics）")]


# ------------------------------------------------------------------ positioning
CFTC_GROUPS = {"am": "資產管理機構", "lev": "槓桿基金", "dealer": "交易商", "other": "其他申報者", "nonrept": "散戶（未申報）"}


def cftc_items():
    s = raw("cftc")
    ds = sorted(s)
    if len(ds) < 50:
        return []
    net = {g: [s[d][f"{g}_long"] - s[d][f"{g}_short"] for d in ds] for g in CFTC_GROUPS}
    # CoinKarma's example: around October 2024 the position rose from 1,800 to 12,000 contracts, the
    # largest holding and addition on record. Which group? Each group's peak in Sep-Nov 2024 and its low
    # in the 8 weeks before that peak, against 1,800 -> 12,000.
    fit = {}
    for g in CFTC_GROUPS:
        win = [(net[g][i], d) for i, d in enumerate(ds) if "2024-09-01" <= d <= "2024-11-30"]
        peak, pd = max(win)
        low = min(net[g][i] for i, d in enumerate(ds) if shift(pd, -56) <= d <= pd)
        fit[g] = (low, peak, pd)
    pick = min(fit, key=lambda g: abs(fit[g][0] - 1800) + abs(fit[g][1] - 12000))
    v = net[pick]
    flips = [ds[i] for i in range(1, len(ds)) if v[i - 1] > 0 >= v[i]]
    st = event_stats(flips, 30, lo=ds[0])
    dn, vn = ds[-1], v[-1]
    return [item(id="cme", group="ETF 與機構", name="CME Smart Money", zh="CME 聰明錢部位", fidelity="proxy",
                 what="CME 比特幣期貨上，某一群歷史上方向較準的交易者的淨部位（一口 = 5 BTC），每週更新。",
                 ck="0 軸以上淨多、以下淨空；2022–23 年轉為淨空常是局部頂。2024 年 10 月部位從 1,800 口加到 12,000 口，之後 BTC 單邊上漲破 10 萬。",
                 ours=f"CFTC 的 TFF 週報，CME Bitcoin；CoinKarma 沒說是哪一群，用他們 2024 年 10 月的例子比對，吻合的是「{CFTC_GROUPS[pick]}」（Other Reportables：{fit[pick][0]:,} 口 → {s2d(fit[pick][2]).month}/{s2d(fit[pick][2]).day} 的 {fit[pick][1]:,} 口，也是有紀錄以來最高）。標出由淨多轉淨空的週。",
                 diff="交易者分類是我們依例子推的；資料與更新時間（週六公布週二的部位）相同。", unit="口", dec=0,
                 lines=[{"k": "v", "label": f"{CFTC_GROUPS[pick]}淨部位（口）", "bar": True}]
                       + [{"k": g, "label": CFTC_GROUPS[g], "hidden": True} for g in CFTC_GROUPS if g != pick],
                 series=pack(ds, {"v": v, **{g: net[g] for g in CFTC_GROUPS if g != pick}}, {k: 0 for k in list(CFTC_GROUPS) + ["v"]}),
                 marks=[{"d": d, "label": "轉為淨空"} for d in flips],
                 now={"d": dn, "v": vn, "state": "淨多" if vn > 0 else "淨空", "tone": "good" if vn > 0 else "bad"},
                 stats=[st] if st else [], src="CFTC Traders in Financial Futures（CME Bitcoin）", group_fit=fit, group_pick=pick)]


def bitfinex_items():
    s = raw("bitfinex")
    ds = sorted(d for d in s if s[d].get("btcusd_long"))
    out = []
    if len(ds) >= 100:
        longs = [s[d]["btcusd_long"] / 1000 for d in ds]
        ethbtc = [(s[d].get("ethbtc_long") or 0) / 1000 or None for d in ds]
        margin = [((s[d].get("fusd_used") or 0) + (s[d].get("fust_used") or 0)) / 1e9 or None for d in ds]
        # accumulation: 30-day change of BTCUSD longs
        ch30 = [longs[i] - longs[i - 30] if i >= 30 else None for i in range(len(ds))]
        dn = ds[-1]
        out.append(item(id="bfxlongs", group="大戶部位", name="Bitfinex BTCUSD LONGS", zh="Bitfinex 槓桿多單", fidelity="close",
                        what="Bitfinex 上 BTC/USD 的槓桿多單總量（BTC），被視為幣圈老牌大戶的部位；另畫 ETH/BTC 多單與保證金借款總額。",
                        ck="大戶在下跌中持續加倉、累積到一定水準是偏多訊號（2022 年跌破 3.5 萬後一路買到 1.6 萬；2024 年 3 月 7.3 萬前大致賣完）；大戶平倉 ETH/BTC 多單後，ETH/BTC 較可能見頂。適合趨勢盤，看長週期。",
                        ours="Bitfinex 公開統計（每分鐘），取每天最後一筆。",
                        diff="資料相同；CoinKarma 沒有給數字門檻。", unit="千顆 BTC", dec=1,
                        lines=[{"k": "v", "label": "BTCUSD 多單（千顆）"}, {"k": "ethbtc", "label": "ETH/BTC 多單（千顆 ETH）", "right": True},
                               {"k": "margin", "label": "USD+USDT 融資（十億美元）", "hidden": True}],
                        series=pack(ds, {"v": longs, "ethbtc": ethbtc, "margin": margin}, {"v": 1, "ethbtc": 1, "margin": 2}),
                        now={"d": dn, "v": r2(longs[-1], 1), "state": f"30 天 {ch30[-1]:+.1f} 千顆" if ch30[-1] is not None else "",
                             "tone": "good" if (ch30[-1] or 0) > 0 else "bad"},
                        src="Bitfinex 公開 API（stats1）"))
    # premium versus Coinbase, hourly
    bh, ch = raw("bitfinex_1h"), raw("coinbase_1h")
    hours = sorted(set(bh) & set(ch))
    if len(hours) > 500:
        prem = {t: (bh[t]["close"] / ch[t]["close"] - 1) * 100 for t in hours if ch[t]["close"]}
        days = {}
        for t, p in prem.items():
            days.setdefault(dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), []).append(p)
        dd = sorted(d for d in days if len(days[d]) >= 12)
        mean = [statistics.mean(days[d]) for d in dd]
        hi = [max(days[d]) for d in dd]
        lo = [min(days[d]) for d in dd]
        up_ev = first_days({d: h > 0.25 for d, h in zip(dd, hi)}, gap=7)
        dn_ev = first_days({d: l < -0.25 for d, l in zip(dd, lo)}, gap=7)
        s_up, s_dn = event_stats(up_ev, 14, lo=dd[0]), event_stats(dn_ev, 14, lo=dd[0])
        if s_up:
            s_up["label"] = "溢價 > +0.25%（某一小時）"
        if s_dn:
            s_dn["label"] = "溢價 < −0.25%"
        out.append(item(id="bfxprem", group="大戶部位", name="Bitfinex Premium", zh="Bitfinex 溢價（對 Coinbase）", fidelity="close",
                        what="Bitfinex BTC/USD 相對 Coinbase BTC/USD 的溢價；正值代表 Bitfinex 大戶買得比市場積極。",
                        ck="> +0.25% 偏多（大戶買盤強），< −0.25% 偏空；越過門檻常是區間的底或頂，適合震盪盤。",
                        ours="兩邊每小時收盤的價差（%）。圖上是每天的平均，標出某一小時越過 ±0.25% 的第一天（前 7 天沒有），看之後 14 天。",
                        diff="資料相同；CoinKarma 沒寫用幾分鐘或幾小時的 K 線。", unit="%", dec=3,
                        lines=[{"k": "v", "label": "日平均溢價（%）"}, {"k": "hi", "label": "當日最高", "hidden": True}, {"k": "lo", "label": "當日最低", "hidden": True}],
                        series=pack(dd, {"v": mean, "hi": hi, "lo": lo}, {"v": 3, "hi": 3, "lo": 3}),
                        zones=[{"from": 0.25, "to": None, "tone": "good", "label": "> +0.25%"}, {"from": None, "to": -0.25, "tone": "bad", "label": "< −0.25%"}],
                        marks=[{"d": d, "label": "> +0.25%"} for d in up_ev] + [{"d": d, "label": "< −0.25%", "tone": "bad"} for d in dn_ev],
                        now={"d": dd[-1], "v": r2(mean[-1], 3), "state": "偏多" if mean[-1] > 0.25 else "偏空" if mean[-1] < -0.25 else "中性", "tone": "mid"},
                        stats=[x for x in (s_up, s_dn) if x], statsKind="labeled", src="Bitfinex、Coinbase 每小時 K 線"))
    # borrowing rates: Bitfinex (crypto-native whales) against OKX (proxy for Binance's retail)
    ok = raw("okx_lending")
    rd = sorted(d for d in s if s[d].get("fust_rate") or s[d].get("fusd_rate"))
    if len(rd) > 100:
        bfx = [((s[d].get("fust_rate") or s[d].get("fusd_rate")) or 0) * 365 * 100 for d in rd]
        okx = [ok.get(d, {}).get("usdt_rate_pct_apr") for d in rd]
        # the two rates are different products (OKX's is a savings rate and sits lower), so the level of one against the
        # other says little; what moves is the spread: today's 7-day spread against its own history
        bfx7 = [statistics.mean(bfx[max(0, i - 6):i + 1]) for i in range(len(rd))]
        sp = [(d, b - o) for d, b, o in zip(rd, bfx7, okx) if o is not None]
        state = None
        if len(sp) > 60:
            d0, s0 = sp[-1]
            vals = sorted(v for _, v in sp)
            pctl = bisect.bisect_left(vals, s0) / len(vals) * 100
            state = (f"利差 {s0:+.1f} 個百分點，{sp[0][0][:4]} 年以來第 {pctl:.0f} 百分位"
                     + ("：Bitfinex 相對偏高（偏底部）" if pctl >= 70 else "：Bitfinex 相對偏低（偏頂部）" if pctl <= 30 else ""))
        out.append(item(id="rates", group="資金流", name="USD/USDT Interest Rate", zh="現貨借貸利率（Bitfinex 對交易所散戶）", fidelity="proxy",
                        what="中心化交易所的 USD／USDT 現貨借貸利率；Bitfinex 多是幣圈原生大戶，Binance 多是散戶。",
                        ck="Bitfinex 利率高於 Binance，常出現在底部；低於 Binance，常出現在頂部。",
                        ours="Bitfinex USDT（fUST）30 天期資金利率的日收盤 × 365；對照 OKX USDT 出借利率（年化）。OKX 的是存款利率、本來就比較低，所以狀態看兩者利差（Bitfinex 7 日平均 − OKX）在自己歷史中的位置。",
                        diff="Binance 的融資利率要 API 金鑰、而且擋美國 IP，改用 OKX 當散戶交易所的代表；OKX 的出借利率歷史從 2024/07 起。", unit="% 年化", dec=1,
                        lines=[{"k": "v", "label": "Bitfinex USDT（%/年）"}, {"k": "okx", "label": "OKX USDT 出借（%/年）"}],
                        series=pack(rd, {"v": bfx, "okx": okx}, {"v": 2, "okx": 2}),
                        now={"d": rd[-1], "v": r2(bfx[-1], 1), "state": state or "OKX 資料還在累積", "tone": "mid"},
                        src="Bitfinex 公開 API、OKX 公開 API"))
    return out


# ------------------------------------------------------------------ institutional liquidity (ours)
def pct_rank_series(ds, vals, window=730, min_n=180):
    """Each value's percentile among the values of the past `window` days (itself included)."""
    out, srt, q = [], [], []  # srt: the window's values sorted; q: (date, value) in arrival order
    k = 0
    for d, v in zip(ds, vals):
        if v is None:
            out.append(None)
            continue
        q.append((d, v))
        bisect.insort(srt, v)
        dd = s2d(d)
        while (dd - s2d(q[k][0])).days > window:
            del srt[bisect.bisect_left(srt, q[k][1])]
            k += 1
        if len(srt) < min_n:
            out.append(None)
            continue
        lo, hi = bisect.bisect_left(srt, v), bisect.bisect_right(srt, v)
        out.append((lo + 0.5 * (hi - lo)) / len(srt) * 100)
    return out


def ili_items():
    fred = raw("fred")
    if not fred or len(BTC) < 500:
        return []
    days = sorted(d for d in BTC if d >= "2020-01-01")
    # 1) net USD liquidity = Fed balance sheet - Treasury General Account - reverse repo (US$ trillions), 13-week change
    last = {"WALCL": None, "WTREGEN": None, "RRPONTSYD": None}
    netliq = {}
    fd = sorted(fred)
    j = 0
    for d in days:
        while j < len(fd) and fd[j] <= d:
            for k in last:
                if fred[fd[j]].get(k) is not None:
                    last[k] = fred[fd[j]][k]
            j += 1
        if all(v is not None for v in last.values()):
            netliq[d] = (last["WALCL"] - last["WTREGEN"]) / 1e6 - last["RRPONTSYD"] / 1e3  # WALCL, WTREGEN in $M; RRP in $B
    c1 = [netliq[d] - netliq[shift(d, -91)] if d in netliq and shift(d, -91) in netliq else None for d in days]
    # 2) spot ETF flows: 30-day net flow in BTC
    etf = {}
    try:
        rows = json.load(open(os.path.join(ROOT, "data", "flows", "BTC.json")))["rows"]
        fl = {r["date"]: r["total"] * 1e6 / (r.get("btcPrice") or BTC.get(r["date"]) or 1) for r in rows if r.get("total") is not None}
        for d in days:
            if d >= "2024-02-10":
                etf[d] = sum(fl.get(shift(d, -k), 0.0) for k in range(30)) / 1000
    except (OSError, ValueError, KeyError):
        pass
    c2 = [etf.get(d) for d in days]
    # 3) corporate treasury demand: MSTR against BTC (premium proxy), 60-day change of the MSTR/BTC ratio
    m = raw("mstr")
    ratio = {d: m[d]["close"] / BTC[d] for d in days if d in m and m[d].get("close") and BTC.get(d)}
    rd = sorted(ratio)
    c3 = []
    for d in days:
        i = bisect.bisect_right(rd, d) - 1
        k = bisect.bisect_right(rd, shift(d, -60)) - 1
        c3.append(math.log(ratio[rd[i]] / ratio[rd[k]]) if i >= 0 and k >= 0 and (s2d(d) - s2d(rd[i])).days <= 5 else None)
    p = [pct_rank_series(days, c) for c in (c1, c2, c3)]
    ili = []
    for i in range(len(days)):
        xs = [p[k][i] for k in range(3) if p[k][i] is not None]
        ili.append(sum(xs) / len(xs) if xs else None)
    # smooth 7 days
    sm = []
    for i in range(len(days)):
        xs = [ili[k] for k in range(max(0, i - 6), i + 1) if ili[k] is not None]
        sm.append(sum(xs) / len(xs) if len(xs) >= 4 else None)
    # signals (CoinKarma's rules, our parameters): segments of new rolling 30-day highs; at the start of a
    # new segment near the 1-year high, ILI clearly below its level at the previous segment's high -> local
    # top; a later close above the all-time high in the same segment with ILI < 50 -> cycle top
    hi30 = {d: BTC[d] >= max(BTC.get(shift(d, -k), 0) for k in range(30)) for d in days}
    local, cycle = [], []
    seg, last_hi, prev_peak_ili = 0, None, None
    peak = None  # (close, ILI) at the highest close of the current segment
    local_seg = {}
    ath = 0.0
    for i, d in enumerate(days):
        if hi30[d]:
            if last_hi is None or (s2d(d) - s2d(last_hi)).days > 10:  # a new segment of 30-day highs
                if peak is not None:
                    prev_peak_ili = peak[1]
                seg += 1
                peak = None
                y_hi = max(BTC.get(shift(d, -k), 0) for k in range(365))
                if prev_peak_ili is not None and sm[i] is not None and BTC[d] >= 0.9 * y_hi and sm[i] < prev_peak_ili - 15:
                    local.append(d)
                    local_seg[seg] = d
            if peak is None or BTC[d] >= peak[0]:
                peak = (BTC[d], sm[i] if sm[i] is not None else (peak[1] if peak else None))
            last_hi = d
            if ath > 0 and BTC[d] > ath and seg in local_seg and sm[i] is not None and sm[i] < 50 \
                    and (not cycle or (s2d(d) - s2d(cycle[-1])).days > 180):
                cycle.append(d)
        ath = max(ath, BTC[d])
    ck_dates = ["2021-11-08", "2023-07-03", "2024-05-20", "2025-01-17", "2025-07-09", "2025-10-01", "2025-10-05"]
    near = lambda d, xs: min((abs((s2d(d) - s2d(x)).days) for x in xs), default=999)  # noqa: E731
    match = sum(1 for d in ck_dates if near(d, local + cycle) <= 14)
    st_l, st_c = event_stats(local, 60, lo="2021-01-01"), event_stats(cycle, 60, lo="2021-01-01")
    if st_l:
        st_l["label"] = "局部頂"
    if st_c:
        st_c["label"] = "週期頂"
    dn, vn = now_of(days, sm)
    zone = "充沛" if (vn or 0) >= 60 else "中性" if (vn or 0) >= 40 else "收縮" if (vn or 0) >= 25 else "枯竭"
    return [item(id="ili", group="ETF 與機構", name="Institutional Liquidity Index", zh="機構流動性指標（ILI）", fidelity="ours",
                 what="機構資金對 BTC 的支撐力道（0–100）：企業財庫買盤、美元淨流動性、現貨 ETF 資金流三條管道合成。",
                 ck="核心是背離：價格在一年高點附近、機構流動性卻明顯轉弱。局部頂（黃）：新一段創高開始時，ILI 明顯弱於上一段高點；週期頂（紅）：之後收盤破歷史新高而 ILI < 50。2021–2026 共 7 次訊號。",
                 ours="三條管道各自換成過去兩年的百分位再平均（7 日平滑）：聯準會淨流動性（資產負債表 − TGA − 逆回購）13 週變化、ETF 30 日淨流量（顆數）、MSTR 相對 BTC 的 60 天漲跌（溢價的代理）。訊號照 CoinKarma 的規則，門檻是我們定的：新一段 = 隔超過 10 天再創 30 日新高；明顯轉弱 = 比上一段高點低 15 分以上；一年高點附近 = 90% 以內。",
                 diff=f"CoinKarma 沒公開公式與門檻；MSTR 的 mNAV 歷史沒有免費來源，改用 MSTR/BTC 比值。他們公布的 7 次訊號，我們有 {match} 次在 14 天內也有訊號。",
                 unit="分", dec=0, lines=[{"k": "v", "label": "ILI（我們的版本，0–100）"}],
                 series=pack(days, {"v": sm}, {"v": 1}),
                 zones=[{"from": 60, "to": None, "tone": "good", "label": "充沛"}, {"from": None, "to": 25, "tone": "bad", "label": "枯竭"}],
                 marks=[{"d": d, "label": "局部頂", "tone": "warn"} for d in local] + [{"d": d, "label": "週期頂", "tone": "bad"} for d in cycle]
                       + [{"d": d, "label": "CoinKarma 訊號", "tone": "ref"} for d in ck_dates],
                 now={"d": dn, "v": r2(vn, 0), "state": zone, "tone": "good" if zone == "充沛" else "bad" if zone in ("收縮", "枯竭") else "mid"},
                 stats=[x for x in (st_l, st_c) if x], statsKind="labeled", src="FRED、cryptoetf.today、Yahoo Finance（MSTR）",
                 ck_match={"ck": ck_dates, "ours_local": local, "ours_cycle": cycle, "matched": match})]


# ------------------------------------------------------------------ forward-only snapshots and options
def forward_items():
    out = []
    a = raw("appstore")
    ds = sorted(a)
    if ds:
        v = [a[d].get("coinbase") for d in ds]
        dn, vn = now_of(ds, v)
        out.append(item(id="appstore", group="散戶情緒", name="Exchange App iOS Ranking", zh="Coinbase App 排名（美國）", fidelity="forward",
                        what="Coinbase App 在美國 App Store 財經類免費榜的名次，代表大眾對加密貨幣的關注度（擦鞋童效應）。",
                        ck="排進前 10、超過 Interactive Brokers 或 Yahoo Finance 時，市場可能短線過熱；在前 10 待越久，越接近週期高點。",
                        ours="每天抓一次 Apple 的財經類排行榜（前 100 名），記下 Coinbase 的名次；不在榜上記為空。",
                        diff="App 排名的歷史要付費資料（Sensor Tower 等），只能從 2026/10 開始累積。", unit="名", dec=0,
                        lines=[{"k": "v", "label": "Coinbase 名次（越小越熱）", "invert": True}], series=pack(ds, {"v": v}, {"v": 0}),
                        zones=[{"from": None, "to": 10, "tone": "warn", "label": "前 10"}],
                        now={"d": dn, "v": vn, "state": "前 10：過熱" if (vn or 99) <= 10 else "不熱", "tone": "warn" if (vn or 99) <= 10 else "mid"},
                        src="Apple iTunes RSS（美國、財經、免費榜）"))
    dv, sn = raw("deribit_dvol"), raw("deribit_snap")
    ds = sorted(dv)
    if len(ds) > 100:
        v = [dv[d]["dvol"] for d in ds]
        sk = [sn.get(d, {}).get("skew25_30d") for d in ds]
        dn = ds[-1]
        last_sk = next((x for x in reversed(sk) if x is not None), None)
        out.append(item(id="skew", group="選擇權", name="BTC 25Δ Skew / Volatility", zh="選擇權偏斜與隱含波動率", fidelity="forward",
                        what="25Δ Skew：一個月期 25 delta 賣權減買權的隱含波動率，> 0 偏恐懼；DVOL：Deribit 的 30 天隱含波動率指數。",
                        ck="Skew > +8 極度恐懼（常是短線情緒底，仍需價格確認），< −8 極度樂觀（之後常回調）。期限結構：短天期 IV 高於長天期（倒掛）常出現在崩跌或底部反轉前。",
                        ours="每天抓一次 Deribit 選擇權報價，用 Black-Scholes 由標記 IV 算 delta，內插出 30 天的 25Δ Skew 與 30／90／180 天 ATM IV；DVOL 有 2021 年起的歷史。",
                        diff="Skew 與期限結構沒有免費歷史，從 2026/10 起累積；DVOL 只是 30 天 IV，不是偏斜。", unit="vol", dec=1,
                        lines=[{"k": "v", "label": "DVOL（30 天 IV）"}, {"k": "sk", "label": "25Δ Skew（30 天）", "right": True}],
                        series=pack(ds, {"v": v, "sk": sk}, {"v": 1, "sk": 2}),
                        now={"d": dn, "v": r2(v[-1], 1), "state": f"Skew {last_sk:+.1f}" if last_sk is not None else "Skew 累積中",
                             "tone": "good" if (last_sk or 0) > 8 else "warn" if (last_sk or 0) < -8 else "mid"},
                        term=[sn[d] for d in sorted(sn)][-60:], src="Deribit 公開 API"))
    return out


# ------------------------------------------------------------------ market pulse (ours)
PULSE = [  # name, label, +1 if a high value is "hot" (CoinKarma's sell side), -1 if a high value is a buy signal
    ("basis", "季度合約年化基差", 1), ("funding", "持倉加權資金費率", 1), ("lsur", "多空人數比", 1),
    ("usdcprem", "USDC/USDT 溢價", -1), ("liq", "Overall LIQ（日平均）", -1), ("liqacc", "BTC 累積 LIQ（日平均）", -1),
    ("ari", "ARI（日最高）", -1)]


def pulse_items():
    """CoinKarma's Market Pulse: 0-100 from 11 of their indicators (<= 20 a buying chance for a reversal, >= 80 a
    selling chance); the components and the scoring are not published. Ours: the indicators here whose direction
    CoinKarma states, each as its percentile over the past 365 days (turned so that 100 = the sell side), averaged."""
    comps = [(k, lab, sgn) for k, lab, sgn in PULSE if len(DAILY.get(k, {})) > 200]
    if len(comps) < 4:
        return []
    days = sorted(set().union(*[set(DAILY[k]) for k, _, _ in comps]))
    pr = {}
    for k, lab, sgn in comps:
        ser = DAILY[k]
        ds = sorted(ser)
        vals = [ser[d] * sgn for d in ds]
        pr[k] = dict(zip(ds, pct_rank_series(ds, vals, window=365, min_n=120)))
    mpi, used = [], []
    for d in days:
        xs = [pr[k].get(d) for k, _, _ in comps]
        xs = [x for x in xs if x is not None]
        mpi.append(sum(xs) / len(xs) if len(xs) >= 4 else None)
        used.append(len(xs))
    ok = [(d, v) for d, v in zip(days, mpi) if v is not None]
    if len(ok) < 60:
        return []
    ds = [d for d, _ in ok]
    vs = [v for _, v in ok]
    lo_ev = first_days({d: v <= 20 for d, v in ok}, 14)
    hi_ev = first_days({d: v >= 80 for d, v in ok}, 14)
    s_lo, s_hi = event_stats(lo_ev, 30, lo=ds[0]), event_stats(hi_ev, 30, lo=ds[0])
    if s_lo:
        s_lo["label"] = "≤ 20（買點）"
    if s_hi:
        s_hi["label"] = "≥ 80（賣點）"
    dn, vn = ds[-1], vs[-1]
    now_parts = []
    for k, lab, sgn in comps:
        x = pr[k].get(dn)
        if x is not None:
            now_parts.append(f"{lab} {x:.0f}")
    return [item(id="pulse", group="綜合", name="Market Pulse Index", zh="市場脈動（我們的版本）", fidelity="ours",
                 what="把幾個情緒與流動性指標合成一個 0–100 的分數：越低越接近反轉向上的買點，越高越接近反轉向下的賣點。",
                 ck="由 11 個 CoinKarma 指標算出 0–100；≤ 20 可能是反轉向上的買點，≥ 80 可能是反轉向下的賣點。11 個指標與算法沒有公開。",
                 ours=f"用這裡 {len(comps)} 個 CoinKarma 有寫明方向的指標：{'、'.join(lab for _, lab, _ in comps)}。每個換成過去 365 天的百分位（讓 100 = 偏賣方），再平均；至少 4 個有值的日子才算。訊號取第一次越過 20／80 的日子（前 14 天沒有）。",
                 diff="CoinKarma 用 11 個指標，組成與權重都沒公開，這是我們的組合；LIQ 系列從 2023 年起才有。",
                 unit="分", dec=0, lines=[{"k": "v", "label": "市場脈動（0–100）"}],
                 series=pack(ds, {"v": vs}, {"v": 1}),
                 zones=[{"from": 80, "to": None, "tone": "bad", "label": "≥ 80"}, {"from": None, "to": 20, "tone": "good", "label": "≤ 20"}],
                 marks=[{"d": d, "label": "≤ 20", "tone": "good"} for d in lo_ev] + [{"d": d, "label": "≥ 80", "tone": "bad"} for d in hi_ev],
                 now={"d": dn, "v": r2(vn, 0), "state": "偏買方" if vn <= 20 else "偏賣方" if vn >= 80 else "中間", "tone": "good" if vn <= 20 else "bad" if vn >= 80 else "mid"},
                 stats=[x for x in (s_lo, s_hi) if x], statsKind="labeled", src="本頁各指標", parts=now_parts)]


# ------------------------------------------------------------------ does CoinKarma's reading hold on our data?
CLAIMS = {  # item id -> [(text in the stat's label, or None for the first stat), CoinKarma's direction, signal name]
    "etfflow": [(None, "up", "IBIT 5 日合計轉淨流出")],
    "cme": [(None, "down", "其他申報者轉為淨空")],
    "ili": [("局部頂", "down", "局部頂（我們的 ILI）"), ("週期頂", "down", "週期頂（我們的 ILI）")],
    "stables": [(None, "up", "USDC 逆勢增發 ≥ 10 億美元")],
    "usdcprem": [(None, "down", "溢價跌破 0")],
    "basis": [("FOMO", "down", "年化基差 > 20%（每一天）")],
    "funding": [("0.03", "down", "資金費率 ≥ 0.03%")],
    "lsur": [(None, "up", "Z 值 ≤ −2")],
    "bfxprem": [("> +0.25", "up", "溢價 > +0.25%"), ("< −0.25", "down", "溢價 < −0.25%")],
    "liq": [("綠色", "up", "綠色箭頭"), ("紅色", "down", "紅色箭頭")],
    "liqacc": [("≥", "up", "累積 LIQ ≥ +60"), ("≤", "down", "累積 LIQ ≤ −60")],
    "liqdif": [("≥", "up", "掛單差 ≥ +2,000 BTC"), ("≤", "down", "掛單差 ≤ −2,000 BTC")],
    "liqtotal": [("≥", "up", "全市場掛單差 ≥ +3 億"), ("≤", "down", "全市場掛單差 ≤ −3 億")],
    "resonance": [("__verdict__", "up", "共振的幣，到波段結束")],
    "screener": [("__verdict__", "up", "第 I 象限（強勢）對全部幣")],
    "ari": [("且 BTC", "up", "ARI ≥ 3（BTC 急跌中）")],
    "pulse": [("≤ 20", "up", "市場脈動 ≤ 20"), ("≥ 80", "down", "市場脈動 ≥ 80")],
}


def verdict(st, claim):
    """支持 / 相反 / 看不出差別 / 樣本太少: the signal's median return and up share against the random baseline."""
    if not st or st.get("ret") is None or st.get("base_ret") is None:
        return None
    if st["n"] < 8:
        return "樣本太少"
    days = st["h"] / 24 if st.get("unit") == "h" else st["h"]
    margin = 0.25 * math.sqrt(max(days, 0.5))
    d = st["ret"] - st["base_ret"]
    du = (st.get("up_share") or 0) - (st.get("base_up_share") or 0)
    sgn = 1 if claim == "up" else -1
    if sgn * d >= margin and sgn * du >= 5:
        return "支持"
    if sgn * d <= -margin and sgn * du <= -5:
        return "相反"
    return "看不出差別"


def verdicts(items):
    out = []
    for it in items:
        for key, claim, name in CLAIMS.get(it["id"], []):
            if key == "__verdict__":
                st = it.get("verdict_stat")
            else:
                sts = it.get("stats") or []
                st = sts[0] if key is None and sts else next((x for x in sts if key in (x.get("label") or "")), None)
            v = verdict(st, claim)
            if v is None:
                continue
            out.append({"id": it["id"], "zh": it["zh"], "signal": name, "claim": "偏多" if claim == "up" else "偏空", "n": st["n"],
                        "h": st.get("hl") or f"{st['h']} 天", "ret": st["ret"], "base": st["base_ret"], "up": st.get("up_share"),
                        "base_up": st.get("base_up_share"), "verdict": v})
    return out


def main():
    BTC.update(btc_close())
    items = []
    for fn in (etf_items, cftc_items, ili_items, stablecoin_items, usdc_premium_items, basis_items, funding_items, lsur_items,
               bitfinex_items, forward_items):
        try:
            items += fn()
        except Exception as e:  # one broken item never stops the rest
            import traceback
            print(f"{fn.__name__} failed: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
    hbtc = None
    try:  # the LIQ family (order-book depth; scripts/ck_liq.py)
        import ck_liq
        li, hbtc = ck_liq.items(item, ctx={"pack": pack, "etf30d": etf_v30(), "btc": BTC, "daily": DAILY})
        items += li
    except Exception as e:
        import traceback
        print(f"ck_liq failed: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
    try:
        items += pulse_items()
    except Exception as e:
        import traceback
        print(f"pulse_items failed: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
    order = ["ETF 與機構", "訂單簿流動性", "資金流", "衍生品情緒", "大戶部位", "散戶情緒", "選擇權", "綜合"]
    items.sort(key=lambda it: order.index(it["group"]) if it["group"] in order else len(order))  # stable: keeps the order inside a group
    last = max((it["now"]["d"][:10] for it in items if it.get("now", {}).get("d")), default=None)
    out = {"asof": last, "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items,
           "btc_last": max(BTC) if BTC else None}
    if hbtc:
        out["hbtc"] = hbtc
    out["verdicts"] = verdicts(items)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    os.replace(tmp, OUT)
    print(f"indicators.json: {len(items)} items ({', '.join(it['id'] for it in items)}), {os.path.getsize(OUT) // 1024} KB", flush=True)


if __name__ == "__main__":
    main()
