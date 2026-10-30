#!/usr/bin/env python3
"""The hourly layer of the indicator library, applied at every site build (scripts/build_site.py): the
latest values of data/ck/live/extra.json (scripts/fetch_live_extras.py) become the "now" of the cards
whose history comes from daily files, and the CVD card is rebuilt from Binance spot hourly candles.
Values older than MAX_AGE_H hours are left out (the card then shows its daily value)."""
import datetime as dt
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRA = os.path.join(ROOT, "data", "ck", "live", "extra.json")
MAX_AGE_H = 6
BASIS_ZONES = [(None, 0, "極度悲觀", "bad"), (0, 5, "極度恐慌", "bad"), (5, 10, "中性", "mid"), (10, 15, "微熱", "mid"),
               (15, 20, "狂熱", "warn"), (20, None, "FOMO", "warn")]


def _age_h(h, now):
    t = dt.datetime.strptime(h, "%Y-%m-%dT%H").replace(tzinfo=dt.timezone.utc)
    return (now - t).total_seconds() / 3600


def _zone(v, zones):
    for lo, hi, lab, tone in zones:
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return lab, tone
    return "", "mid"


def patch(ck, now=None):
    try:
        ex = json.load(open(EXTRA))
    except (OSError, ValueError):
        return ck
    now = now or dt.datetime.now(dt.timezone.utc)
    items = {it["id"]: it for it in ck.get("items", [])}

    def fresh(name):
        x = ex.get(name)
        return x if isinstance(x, dict) and x.get("h") and x.get("v") is not None and _age_h(x["h"], now) <= MAX_AGE_H else None

    def note(it, text):
        base = it.get("liveBase")
        if base is None:
            base = it["liveBase"] = it.get("src", "")
        it["src"] = base + "；" + text

    f = fresh("funding")
    if f and "funding" in items:
        v = f["v"]
        items["funding"]["now"] = {"d": f["h"], "v": round(v, 4), "state": "偏熱" if v >= 0.03 else "負費率" if v < 0 else "中性",
                                   "tone": "warn" if v >= 0.03 else "bad" if v < 0 else "mid"}
        note(items["funding"], "目前值每小時更新：OKX 與 Hyperliquid 的當期費率依持倉加權（Binance 的即時費率抓不到）")
    b = fresh("basis")
    if b and "basis" in items:
        lab, tone = _zone(b["v"], BASIS_ZONES)
        items["basis"]["now"] = {"d": b["h"], "v": round(b["v"], 1), "state": lab, "tone": tone}
        note(items["basis"], f"目前值每小時更新：OKX {b.get('contract')}（剩 {b.get('days_left')} 天）對 OKX 指數")
    u = fresh("usdcprem")
    if u and "usdcprem" in items:
        v = u["v"]
        items["usdcprem"]["now"] = {"d": u["h"], "v": round(v, 3), "state": "負溢價" if v < 0 else "正溢價", "tone": "bad" if v < 0 else "good"}
        note(items["usdcprem"], "目前值每小時更新（上一小時收盤）")
    p = fresh("bfxprem")
    if p and "bfxprem" in items:
        v = p["v"]
        items["bfxprem"]["now"] = {"d": p["h"], "v": round(v, 3), "state": "偏多" if v > 0.25 else "偏空" if v < -0.25 else "中性", "tone": "mid"}
        note(items["bfxprem"], "目前值每小時更新（上一小時收盤的溢價）")
    lg = fresh("bfxlongs")
    if lg and "bfxlongs" in items:
        it = items["bfxlongs"]
        s = it.get("series") or {}
        ch = None
        target = (now - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        prev = [(d, v) for d, v in zip(s.get("d", []), s.get("v", [])) if v is not None and d <= target]
        if prev:
            ch = lg["v"] - prev[-1][1]
        it["now"] = {"d": lg["h"], "v": round(lg["v"], 1), "state": f"30 天 {ch:+.1f} 千顆" if ch is not None else "",
                     "tone": "good" if (ch or 0) > 0 else "bad"}
        note(it, "目前值每小時更新")
    d = fresh("dvol")
    if d and "skew" in items:
        sk = d.get("skew25_30d")
        items["skew"]["now"] = {"d": d["h"], "v": round(d["v"], 1), "state": f"Skew {sk:+.1f}" if sk is not None else "",
                                "tone": "good" if (sk or 0) > 8 else "warn" if (sk or 0) < -8 else "mid"}
        note(items["skew"], "目前值每小時更新")
    c = ex.get("cvd")
    if isinstance(c, dict) and c.get("delta") and "cvd" in items and _age_h(c["h"], now) <= MAX_AGE_H:
        it = items["cvd"]
        cum, run = [], 0.0
        for x in c["delta"]:
            run += x
            cum.append(round(run))
        v24 = sum(c["delta"][-24:])
        it["series"] = {"h0": c["h0"], "n": len(cum), "v": cum, "btc": c.get("close")}
        it["now"] = {"d": c["h"], "v": round(v24), "state": "市價買盤較強" if v24 > 0 else "市價賣盤較強", "tone": "good" if v24 > 0 else "bad"}
        it["ours"] = ("Binance 現貨 BTCUSDT 每小時 K 線的主動買進成交額 × 2 − 總成交額，從圖的起點累加（百萬美元）；"
                      "目前值是最近 24 小時的合計。每小時更新。")
        it["diff"] = "CoinKarma 沒寫用現貨還是合約、哪些交易所；這裡是 Binance 現貨（data-api.binance.vision 的即時 K 線）。"
        it["src"] = "Binance 現貨 K 線（data-api.binance.vision），每小時更新"
        it.pop("marks", None)
    ck["live_asof"] = ex.get("asof")
    return ck


if __name__ == "__main__":
    j = patch(json.load(open(os.path.join(ROOT, "data", "ck", "indicators.json"))))
    for it in j["items"]:
        print(f"{it['id']:10s} {it.get('now', {}).get('d')} {it.get('now', {}).get('v')} {it.get('now', {}).get('state')}")
