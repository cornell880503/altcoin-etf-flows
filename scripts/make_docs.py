#!/usr/bin/env python3
"""Turn data/daily.json + ETF closes into the dashboard database documents
(collection "daily", one document per US trading day) and a short summary.

Usage
  python3 scripts/make_docs.py --out OUT [--days 8 | --all] [--ibkr DIR] [--db DIR]

  --ibkr DIR  closes from Interactive Brokers, one <TICKER>.json per ETF holding the
              get_price_history reply ({"time": [...], "close": [...]}); they override
              stored closes for the same dates.
  --db DIR    export of the existing documents (ArtifactData list with out_dir): supplies
              the closes history for the statistics; unchanged documents are skipped and
              existing ones are flagged "needs_if_version".

Outputs (in OUT)
  docs/<date>.json   document bodies
  batch_<k>.json     ArtifactData batch entries (<= 50 each); entries for documents that
                     already exist need "if_version" added from the database listing
  summary.txt        Traditional Chinese summary for the push notification
  report.json        what was written, missing closes, latest date
"""
import argparse
import datetime as dt
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats as S  # noqa: E402
import strategies as T  # noqa: E402
from calendar_us import trading_days, prev_trading_day  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)  # noqa: E731
SGT = dt.timezone(dt.timedelta(hours=8))
ALTS = ["SOL", "XRP", "HYPE", "LINK", "DOGE", "AVAX", "HBAR", "LTC", "SUI", "DOT", "NEAR", "ZEC", "BNB", "TRX"]
REFS = ["BTC", "ETH"]
WEEK = "一二三四五六日"


def db_export(db_dir):
    """{date: document} from an ArtifactData export (list/query with out_dir)."""
    out = {}
    if not db_dir:
        return out
    for f in glob.glob(os.path.join(db_dir, "**", "*.json"), recursive=True):
        try:
            j = json.load(open(f))
        except (OSError, ValueError):
            continue
        if isinstance(j, dict) and isinstance(j.get("date"), str) and isinstance(j.get("coins"), dict):
            out[j["date"]] = j
    return out


def load_closes(daily, ibkr_dir=None, existing=None):
    """Closes per coin: repo files (if any) < stored documents < fresh IBKR replies."""
    closes = {}
    for sym, c in daily["coins"].items():
        t = c["etf"]
        m = {}
        try:
            j = json.load(open(P("data", "prices", f"{t}.json")))
            m.update(dict(zip(j["dates"], j["close"])))
        except (OSError, ValueError, KeyError):
            pass
        for d, doc in (existing or {}).items():
            v = (doc["coins"].get(sym) or {}).get("close")
            if isinstance(v, (int, float)):
                m[d] = float(v)
        if ibkr_dir:
            f = os.path.join(ibkr_dir, f"{t}.json")
            if os.path.exists(f):
                j = json.load(open(f))
                for ts, v in zip(j.get("time", []), j.get("close", [])):
                    if v is not None:
                        m[str(ts)[:10]] = float(v)
        cut = max(c["launch"], daily["window_start"])
        closes[sym] = {d: v for d, v in m.items() if d >= cut}
    return closes


def coin_maps(daily):
    out = {}
    for sym, c in daily["coins"].items():
        pend = set((c.get("pending") or {}).get("dates", []))
        flows = {d: v for d, v in zip(c["dates"], c["flow"])}
        funds = {}
        for tk, arr in (c.get("funds") or {}).items():
            for d, v in zip(c["dates"], arr):
                if v not in (None, 0, 0.0):
                    funds.setdefault(d, {})[tk] = v
        mcap = {d: v for d, v in zip(c["dates"], c.get("mcap") or []) if isinstance(v, (int, float)) and v > 0}
        out[sym] = {"flow": flows, "funds": funds, "pending": pend, "mcap": mcap,
                    "pending_fund": (c.get("pending") or {}).get("fund")}
    return out


def build_docs(daily, closes, dates):
    maps = coin_maps(daily)
    now = dt.datetime.now(SGT).isoformat(timespec="seconds")
    docs = {}
    for d in dates:
        coins = {}
        for sym in ALTS + REFS:
            if sym not in maps:
                continue
            e = {}
            m = maps[sym]
            if d in m["flow"] and m["flow"][d] is not None:
                e["flow"] = m["flow"][d]
            if d in m["funds"]:
                e["funds"] = m["funds"][d]
            if d in m["pending"]:
                e["pending"] = m["pending_fund"]
            if d in closes.get(sym, {}):
                e["close"] = round(closes[sym][d], 4)
            if d in m["mcap"]:
                e["mcap"] = m["mcap"][d]  # US$ millions, about the previous US close
            if e:
                coins[sym] = e
        if coins:
            docs[d] = {"date": d, "coins": coins, "schema": 2, "updated_at": now}
    return docs


def series_for(sym, daily, closes, cal):
    m = coin_maps(daily)[sym]
    F = [m["flow"].get(d) for d in cal]
    Pc = [closes[sym].get(d) for d in cal]
    pend = [d in m["pending"] for d in cal]
    R = [None] + [math.log(Pc[i] / Pc[i - 1]) if Pc[i] and Pc[i - 1] else None for i in range(1, len(cal))]
    # flow as a share of the market cap at about the previous close, in basis points
    MC = [m["mcap"].get(d) for d in cal]
    X = [None] + [F[i] / MC[i] * 1e4 if (F[i] is not None and MC[i] and not pend[i]) else None for i in range(1, len(cal))]
    return F, Pc, R, X, pend


CANARY_PART = {"SOL": "SOLC", "XRP": "XRPC", "SUI": "SUIS"}
CANARY_ONLY = {"LTC", "HBAR", "TRX"}
LT_KEYS = ["same_day", "next_day", "tradable_1d", "tradable_5d", "chase_1d", "chase_5d"]


def known_flows(sym, daily, cal):
    """Flow as first published (same rule as the dashboard): Canary parts post a day late."""
    m = coin_maps(daily)[sym]
    out = []
    for d in cal:
        f = m["flow"].get(d)
        if f is None:
            out.append(None)
        elif sym in CANARY_PART:
            out.append(f - ((m["funds"].get(d) or {}).get(CANARY_PART[sym]) or 0.0))
        else:
            out.append(None if d in m["pending"] else f)
    return out


def significance(daily, closes, cal):
    """Two summary lines: who leads / lags (q < 0.10 across all coins) and the rule tests."""
    ser = {}
    for s in ALTS + REFS:
        F, Pc, R, X, pend = series_for(s, daily, closes, cal)
        ser[s] = dict(P=Pc, R=R, X=X, K=known_flows(s, daily, cal), lag=2 if s in CANARY_ONLY else 1)
    RB = ser["BTC"]["R"]
    coins = ALTS + REFS
    lt = {s: S.leadlag_tests(ser[s]["X"], ser[s]["R"], RB, ser[s]["lag"], "abn", s == "BTC") for s in coins}
    q = {}
    for k in LT_KEYS:
        qs = S.bh([(lt[s].get(k) or {}).get("p") for s in coins])
        q[k] = dict(zip(coins, qs))

    def sig(s, k, sign):
        t = lt[s].get(k)
        return t is not None and q[k][s] is not None and q[k][s] < 0.10 and (t["r"] > 0 if sign > 0 else t["r"] < 0)
    lead = [s for s in coins if sig(s, "tradable_1d", 1) or sig(s, "tradable_5d", 1)]
    rev = [s for s in coins if sig(s, "tradable_1d", -1) or sig(s, "tradable_5d", -1)]
    sync = [s for s in coins if sig(s, "same_day", 1)]
    chase = [s for s in coins if sig(s, "chase_1d", 1) or sig(s, "chase_5d", 1)]
    line1 = "領先檢定：公布後還能預測幣價的幣：" + ("、".join(lead) if lead else "無")
    line1 += "｜同步：" + ("、".join(sync) if sync else "無") + "｜追漲：" + ("、".join(chase) if chase else "無")
    if rev:
        line1 += "｜流入後反轉：" + "、".join(rev)
    # rule tests (raw next-day returns), same family as the dashboard's trading tab
    tests = []
    for s in ALTS:
        c = ser[s]
        sg = T.signals(c["K"], c["P"], c["lag"])
        for rule in T.RULES:
            net, _ = T.backtest(sg[rule], c["R"])
            if T.perf(net) is None:
                continue
            tt = T.timing(sg[rule], c["R"])
            if tt is not None:
                tests.append(tt["p"])
    Rb = T.basket([ser[s]["R"] for s in ALTS])
    n = len(cal)
    agg = [None] * n
    for d in range(1, n):
        v = [ser[s]["K"][d - 1] for s in ALTS if ser[s]["lag"] == 1 and ser[s]["K"][d - 1] is not None]
        agg[d] = sum(v) if v else None
    btc = ser["BTC"]["K"]
    lvl, cum = [None] * n, 0.0
    for i in range(n):
        if Rb[i] is not None:
            cum += Rb[i]
        lvl[i] = math.exp(cum)
    bs = {k: [None] * n for k in ("ALTFLOW5", "BTCFLOW5", "TR20", "TF")}
    for d in range(n):
        w = agg[max(0, d - 4):d + 1]
        if d >= 5 and all(v is not None for v in w):
            bs["ALTFLOW5"][d] = 1 if sum(w) > 0 else 0
        if d >= 5 and all(v is not None for v in btc[d - 5:d]):
            bs["BTCFLOW5"][d] = 1 if sum(btc[d - 5:d]) > 0 else 0
        if d >= 20:
            bs["TR20"][d] = 1 if lvl[d] / lvl[d - 20] > 1 else 0
        if bs["TR20"][d] is not None and bs["ALTFLOW5"][d] is not None:
            bs["TF"][d] = bs["TR20"][d] * bs["ALTFLOW5"][d]
    for k in ("ALTFLOW5", "BTCFLOW5", "TR20", "TF"):
        tt = T.timing(bs[k], Rb)
        if tt is not None:
            tests.append(tt["p"])
    qs = [v for v in S.bh(tests) if v is not None]
    n_sig = sum(1 for v in qs if v < 0.10)
    wb = btc[n - 5:n]
    nxt = ""
    if len(wb) == 5 and all(v is not None for v in wb):
        tot5 = sum(wb)
        nxt = f"；BTC ETF 近 5 日 {money(tot5)}，籃子規則下一個交易日「{'持有' if tot5 > 0 else '空手'}」（研究用）"
    line2 = f"交易規則：{len(tests)} 個擇時檢定，校正後顯著 {n_sig} 個" + nxt
    return [line1, line2]


def money(v, sign=True):
    if v is None:
        return "—"
    s = "+" if v > 0.05 else ("−" if v < -0.05 else "")
    return f"{s}${abs(v):,.1f}M" if sign else f"${abs(v):,.1f}M"


def api_status():
    """State of the keyed REST API fetch (scripts/fetch_api.py), and a warning line only when the
    key is set but failing. No report file = the key secret is not configured (nothing to say)."""
    try:
        rep = json.load(open(P("data", "flows_api", "_report.json")))
    except (OSError, ValueError):
        return {"configured": False}, None
    assets = rep.get("assets", {})
    errs = {k: v["error"] for k, v in assets.items() if v.get("error")}
    last = rep.get("last_success")
    age_h = None
    if last:
        age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=dt.timezone.utc)).total_seconds() / 3600
    state = {"configured": True, "last_success": last, "run_at": rep.get("run_at"), "errors": errs}
    first = "；".join(f"{k} {v}" for k, v in list(errs.items())[:2])
    if errs and (last is None or rep.get("run_at") != last):
        return state, f"注意：cryptoetf.today API 這次抓取失敗（{first}），金鑰可能無效或過期；流量改用公開資料，更新不受影響"
    if errs:
        return state, f"注意：cryptoetf.today API 有 {len(errs)} 檔沒抓到（{first}）"
    if age_h is not None and age_h > 50:
        return state, f"注意：cryptoetf.today API 已 {age_h / 24:.0f} 天沒有成功抓取"
    return state, None


def summary(daily, closes):
    allds = sorted({d for c in daily["coins"].values() for d in c["dates"]})
    cal = trading_days(daily["window_start"], allds[-1])
    _, _, RB, _, _ = series_for("BTC", daily, closes, cal)
    D = max(daily["coins"][s]["dates"][-1] for s in ALTS if daily["coins"][s]["dates"])
    di = cal.index(D)
    lines = []
    dd = dt.date.fromisoformat(D)
    lines.append(f"山寨幣 ETF 資金流｜{dd.month}/{dd.day}（{WEEK[dd.weekday()]}）美股收盤")
    tot, n_in, n_data, five = 0.0, 0, 0, 0.0
    missing = []
    per = {}
    for s in ALTS + REFS:
        F, Pc, R, X, pend = series_for(s, daily, closes, cal)
        v = F[di]
        last5 = [f for f in F[max(0, di - 4):di + 1] if f is not None]
        last20 = [f for f in F[max(0, di - 19):di + 1] if f is not None]
        r60 = S.rolling(X, R, RB, 60, "abn", s == "BTC")
        r30 = S.rolling(X, R, RB, 30, "abn", s == "BTC")
        hist = [f for f in F[:di + 1] if f is not None]
        thr = S.quantile(hist, 0.9) if len(hist) >= 60 else None
        per[s] = dict(v=v, pend=pend[di], s5=sum(last5) if last5 else None, s20=sum(last20) if last20 else None,
                      r60=r60[di], r60w=r60[di - 5] if di >= 5 else None, r30=r30[di], thr=thr)
        if s in ALTS:
            five += per[s]["s5"] or 0
            if v is None:
                missing.append(s)
            else:
                n_data += 1
                tot += v
                n_in += v > 0.05
    lines.append(f"山寨幣合計 {money(tot)}（{n_data} 檔有資料，{n_in} 檔淨流入）；近 5 日 {money(five)}")
    main = []
    for s in ["SOL", "XRP", "HYPE"]:
        p = per[s]
        tag = "（不含 " + daily["coins"][s]["pending"]["fund"] + "，次日補）" if p["pend"] else ""
        main.append(f"{s} {money(p['v'])}{tag}，5 日 {money(p['s5'])}")
    lines.append("｜".join(main))
    others = [f"{s} {money(per[s]['v'])}" for s in ALTS[3:] if per[s]["v"] is not None and abs(per[s]["v"]) >= 2]
    if others:
        lines.append("其他 |流量| ≥ $2M：" + "、".join(others))
    lines.append(f"BTC {money(per['BTC']['v'])}｜ETH {money(per['ETH']['v'])}")
    cs = []
    for s in ["SOL", "XRP", "HYPE", "BTC"]:
        p = per[s]
        if p["r60"] is None:
            continue
        ch = "" if p["r60w"] is None else f"（一週前 {p['r60w']:.2f}）"
        cs.append(f"{s} {p['r60']:.2f}{ch}")
    if cs:
        lines.append("60 日相關（流量 vs 扣除 BTC 後報酬）：" + "、".join(cs))
    big = [s for s in ALTS if per[s]["thr"] is not None and per[s]["v"] is not None and per[s]["v"] > 0 and per[s]["v"] >= per[s]["thr"]]
    if big:
        lines.append("大額流入（上市以來前 10%）：" + "、".join(big))
    if missing:
        lines.append("尚未公布：" + "、".join(missing) + "（Canary 基金晚一天）" * any(m in ("LTC", "HBAR", "TRX") for m in missing))
    try:
        gen = dt.datetime.strptime(daily["generated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age_h = (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() / 3600
        if age_h > 8:
            lines.append(f"注意：流量資料最後抓取於 {gen.astimezone(SGT):%m/%d %H:%M}（新加坡時間），已超過 {age_h:.0f} 小時，自動抓取可能失敗")
    except (KeyError, ValueError):
        pass
    try:
        api_line = api_status()[1]
        if api_line:
            lines.append(api_line)
    except Exception:  # the API check must never block the summary
        pass
    try:
        lines.extend(significance(daily, closes, cal))
    except Exception as e:  # never block the daily summary on the statistics
        lines.append(f"（顯著性檢定這次沒有算出來：{type(e).__name__}）")
    lines.append("儀表板：山寨幣 ETF 資金流")
    return "\n".join(lines), D, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ibkr")
    ap.add_argument("--db")
    a = ap.parse_args()
    daily = json.load(open(P("data", "daily.json")))
    existing = db_export(a.db)
    closes = load_closes(daily, a.ibkr, existing)
    allds = sorted({d for c in daily["coins"].values() for d in c["dates"]})
    cal = trading_days(daily["window_start"], allds[-1])
    dates = cal if a.all else cal[-a.days:]
    docs = build_docs(daily, closes, dates)
    os.makedirs(os.path.join(a.out, "docs"), exist_ok=True)
    entries, unchanged = [], []
    for d, doc in sorted(docs.items()):
        old = existing.get(d)
        if old is not None and {k: v for k, v in old.items() if k not in ("updated_at", "seeded_by")} == \
                {k: v for k, v in doc.items() if k != "updated_at"}:
            unchanged.append(d)
            continue
        path = os.path.abspath(os.path.join(a.out, "docs", f"{d}.json"))
        json.dump(doc, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
        e = {"op": "set", "collection": "daily", "doc_id": d, "file_path": path}
        if old is not None:
            e["needs_if_version"] = True
        entries.append(e)
    for k in range(0, len(entries), 50):
        json.dump(entries[k:k + 50], open(os.path.join(a.out, f"batch_{k // 50 + 1}.json"), "w"), indent=1)
    text, D, per = summary(daily, closes)
    if existing and D in existing and not entries:
        dd = dt.date.fromisoformat(D)
        text = (f"山寨幣 ETF 資金流：沒有新的美股交易日（最新仍是 {dd.month}/{dd.day}），資料已核對、沒有修正。\n"
                + "\n".join(l for l in text.split("\n") if l.startswith(("注意", "領先檢定", "交易規則"))))
    open(os.path.join(a.out, "summary.txt"), "w").write(text + "\n")
    last_closes = {s: (closes[s].get(D) is not None) for s in daily["coins"]}
    report = {"latest_date": D, "written": [e["doc_id"] for e in entries], "unchanged": unchanged,
              "existing_need_version": [e["doc_id"] for e in entries if e.get("needs_if_version")],
              "missing_close_on_latest": [daily["coins"][s]["etf"] for s, ok in last_closes.items() if not ok],
              "api": api_status()[0]}
    json.dump(report, open(os.path.join(a.out, "report.json"), "w"), indent=1, ensure_ascii=False)
    short = dict(report, written=f"{len(report['written'])} docs" + (f" ({report['written'][0]} .. {report['written'][-1]})" if report["written"] else ""),
                 unchanged=f"{len(unchanged)} docs")
    print(json.dumps(short, ensure_ascii=False))
    print(text)


if __name__ == "__main__":
    main()
