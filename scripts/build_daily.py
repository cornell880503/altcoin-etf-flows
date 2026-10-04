#!/usr/bin/env python3
"""Clean every coin's daily ETF net flow onto the US trading calendar and write
data/daily.json, the file the dashboard's daily job reads.

Sources (all fetched by this repo's workflow)
- data/flows/<SYM>.json        cryptoetf.today per-issuer history (BTC ETH SOL XRP HYPE)
- data/flows_mcp/<SYM>.json    cryptoetf.today asset totals, last 30 days, accumulated per run
- data/canary/<TICKER>.csv     Canary's own shares outstanding x NAV (XRPC LTCC HBR SOLC SUIS TRXS)

Cleaning rules
1. Nothing before a coin's first ETF trading day (seed capital is not a flow).
2. Canary rows are dated on settlement (T+1): shares outstanding on rate date t
   reflect orders from the previous session (XRPC's US$243m launch-day creation
   appears on 2025-11-14; it listed on 11-13). Canary flows move back one trading day.
   The newest session's Canary figure is therefore not known until the next update.
3. Weekend/holiday rows: all-zero rows are dropped. Non-zero weekend rows belong to
   the previous session (the source splits some Friday sessions over the weekend; the
   parts add up to the Friday figure in an earlier snapshot). A weekday-holiday row
   that repeats the previous session is a duplicate and is dropped.
4. A trading day inside the published range with no row is a zero-flow day.
   Days after the source's latest published session are left out, not zero.
Units: US$ millions, signed (+ = net creation).
"""
import csv
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calendar_us import is_trading_day, next_trading_day, prev_trading_day, trading_days  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)  # noqa: E731
WINDOW_START = "2025-10-28"  # first alt-coin ETF session (BSOL, HBR, LTCC)

# coin -> first ETF trading day, the ETF whose close is used as the price, source plan
COINS = {
    "SOL":  {"launch": "2025-10-28", "etf": "BSOL", "plan": "issuers", "canary": "SOLC"},
    "XRP":  {"launch": "2025-11-13", "etf": "XRPC", "plan": "issuers", "canary": "XRPC"},
    "HYPE": {"launch": "2026-05-12", "etf": "THYP", "plan": "issuers"},
    "LTC":  {"launch": "2025-10-28", "etf": "LTCC", "plan": "canary_only", "canary": "LTCC"},
    "HBAR": {"launch": "2025-10-28", "etf": "HBR",  "plan": "canary_only", "canary": "HBR"},
    "TRX":  {"launch": "2026-09-09", "etf": "TRXS", "plan": "canary_only", "canary": "TRXS"},
    "SUI":  {"launch": "2026-02-18", "etf": "GSUI", "plan": "totals", "canary": "SUIS"},
    "LINK": {"launch": "2025-12-02", "etf": "GLNK", "plan": "totals"},
    "DOGE": {"launch": "2025-11-24", "etf": "GDOG", "plan": "totals"},
    "AVAX": {"launch": "2026-01-26", "etf": "VAVX", "plan": "totals"},
    "DOT":  {"launch": "2026-03-06", "etf": "TDOT", "plan": "totals"},
    "NEAR": {"launch": "2026-09-29", "etf": "NRR",  "plan": "totals"},
    "ZEC":  {"launch": "2026-08-25", "etf": "ZCSH", "plan": "totals"},
    "BNB":  {"launch": "2026-05-29", "etf": "VBNB", "plan": "totals"},
    "BTC":  {"launch": "2024-01-11", "etf": "IBIT", "plan": "issuers", "ref": True},
    "ETH":  {"launch": "2024-07-23", "etf": "ETHA", "plan": "issuers", "ref": True},
}
ISSUER = {"blackrock": "BlackRock", "blackrock2": "BlackRock 2", "fidelity": "Fidelity", "bitwise": "Bitwise",
          "twentyOneShares": "21Shares", "invesco": "Invesco", "franklin": "Franklin", "valkyrie": "CoinShares",
          "vanEck": "VanEck", "wisdomTree": "WisdomTree", "morganStanley": "Morgan Stanley",
          "morganStanleyEth": "Morgan Stanley", "grayscale": "Grayscale", "grayscaleBtc": "Grayscale Mini",
          "grayscaleCrypto": "Grayscale Mini", "canary": "Canary"}
# issuer key in the source -> fund ticker, per coin (same funds as the source's fund list)
TICKERS = {
    "BTC": {"blackrock": "IBIT", "fidelity": "FBTC", "grayscale": "GBTC", "grayscaleBtc": "BTC", "bitwise": "BITB",
            "twentyOneShares": "ARKB", "vanEck": "HODL", "morganStanley": "MSBT", "valkyrie": "BRRR",
            "franklin": "EZBC", "invesco": "BTCO", "wisdomTree": "BTCW"},
    "ETH": {"blackrock": "ETHA", "blackrock2": "ETHB", "fidelity": "FETH", "grayscale": "ETHE",
            "grayscaleCrypto": "ETH", "bitwise": "ETHW", "vanEck": "ETHV", "franklin": "EZET",
            "twentyOneShares": "TETH", "morganStanleyEth": "MSSE", "invesco": "QETH"},
    "SOL": {"bitwise": "BSOL", "fidelity": "FSOL", "grayscale": "GSOL", "vanEck": "VSOL", "twentyOneShares": "TSOL",
            "morganStanley": "MSOL", "franklin": "SOEZ", "canary": "SOLC", "invesco": "QSOL"},
    "XRP": {"bitwise": "XRP", "canary": "XRPC", "franklin": "XRPZ", "grayscale": "GXRP", "twentyOneShares": "TOXR"},
    "HYPE": {"bitwise": "BHYP", "twentyOneShares": "THYP", "grayscale": "HYPG"},
}
PRICE_KEYS = ("Price",)


def r3(x):
    return round(float(x), 3)


def load_json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def published_dates():
    """Latest published session per asset, from the MCP summary (asset code -> date)."""
    out = {}
    try:
        s = json.loads(open(P("data", "mcp_raw", "_summary.txt")).read())
        for a in s.get("assets", []):
            sym = {"HYP": "HYPE"}.get(a.get("symbol"), a.get("symbol"))
            if a.get("date"):
                out[sym] = a["date"][:10]
        out["_reference"] = s.get("referenceDate")
    except (OSError, ValueError):
        pass
    return out


def canary_flows(ticker):
    """{trade_date: US$m} from Canary's table (settlement dated -> previous session)."""
    path = P("data", "canary", f"{ticker}.csv")
    if not os.path.exists(path):
        return None, None
    rows = sorted(csv.DictReader(open(path)), key=lambda r: r["date"])
    out, last_rate = {}, None
    for prev, cur in zip(rows, rows[1:]):
        try:
            so0, so1, nav = float(prev["shares_outstanding"]), float(cur["shares_outstanding"]), float(cur["nav"])
        except (TypeError, ValueError):
            continue
        trade = prev_trading_day(cur["date"])
        out[trade] = out.get(trade, 0.0) + (so1 - so0) * nav / 1e6
        last_rate = cur["date"]
    return out, last_rate


def fold_calendar(rows, start, end, cols):
    """rows: {date: {col: v}} possibly incl. weekends -> {trading_day: {col: v}} (rules 3, 4).

    The source now spreads some Friday sessions over the weekend (XRP 2026-01-02,
    01-30, 02-27, 05-29: Friday + Saturday/Sunday rows add up exactly to the Friday
    figure in an earlier snapshot of the same source), so weekend rows go back to the
    previous session. A weekday-holiday row that repeats the previous session is a
    duplicate (XRP 2026-01-01; the 2025-12-31 total was US$5.58m per SoSoValue)."""
    days = trading_days(start, end)
    out = {d: {c: 0.0 for c in cols} for d in days}
    log = []
    for d in sorted(rows):
        if d > end:
            continue
        vals = rows[d]
        if is_trading_day(d):
            if d >= start:
                for c in cols:
                    out[d][c] += vals.get(c, 0.0)
            continue
        if all(abs(vals.get(c, 0.0)) < 1e-9 for c in cols):
            continue
        prev = prev_trading_day(d)
        weekend = dt.date.fromisoformat(d).weekday() >= 5
        if not weekend and prev in rows and all(abs(rows[prev].get(c, 0.0) - vals.get(c, 0.0)) < 1e-6 for c in cols):
            log.append(f"dropped holiday duplicate {d} (= {prev})")
            continue
        if prev in out:
            for c in cols:
                out[prev][c] += vals.get(c, 0.0)
            log.append(f"added {d} to {prev}")
    return out, log


def build_issuers(sym, cfg, pub):
    src = load_json(P("data", "flows", f"{sym}.json"))
    if not src:
        return None
    cols = [k for k in src.get("keys", []) if k != "total" and not any(k.endswith(p) for p in PRICE_KEYS)]
    rows = {}
    for r in src["rows"]:
        rows[r["date"][:10]] = {c: float(r.get(c) or 0.0) for c in cols}
    start = max(cfg["launch"], WINDOW_START)
    # last published session: MCP summary date if present, else the last trading day with any non-zero issuer
    nonzero = [d for d in rows if is_trading_day(d) and any(abs(v) > 1e-9 for v in rows[d].values())]
    end = pub.get(sym) or (max(nonzero) if nonzero else None)
    if not end:
        return None
    end = min(end, max(rows))
    log = []
    canary_note = None
    if cfg.get("canary") and "canary" in cols:
        cf, last_rate = canary_flows(cfg["canary"])
        if cf:
            # replace the source's settlement-dated Canary column by Canary's own table (already trade dated)
            for d in rows:
                rows[d]["canary"] = 0.0
            shifted = True
        else:
            shifted = False
            log.append("Canary table unavailable: source column shifted instead")
    days_rows, l2 = fold_calendar(rows, start, end, cols)
    log += l2
    if cfg.get("canary") and "canary" in cols:
        if shifted:
            for d in days_rows:
                days_rows[d]["canary"] = cf.get(d, 0.0)
            # sessions after Canary's last rate date - 1 are not known yet
            known_until = prev_trading_day(last_rate) if last_rate else None
            pend = [d for d in days_rows if known_until is None or d > known_until]
            for d in pend:
                days_rows[d]["canary"] = None
            if pend:
                canary_note = {"fund": cfg["canary"], "dates": pend}
            log.append(f"Canary {cfg['canary']} from its own table, moved to trade date")
        else:
            ds = sorted(days_rows)
            vals = [days_rows[d]["canary"] for d in ds]
            for i, d in enumerate(ds):
                days_rows[d]["canary"] = vals[i + 1] if i + 1 < len(ds) else None
            canary_note = {"fund": cfg["canary"], "dates": [ds[-1]]}
    ds = sorted(days_rows)
    flow = [r3(sum(v for v in days_rows[d].values() if v is not None)) for d in ds]
    funds = {}
    for c in cols:
        series = [None if days_rows[d][c] is None else r3(days_rows[d][c]) for d in ds]
        if any(v not in (None, 0.0) for v in series):
            funds[TICKERS.get(sym, {}).get(c) or ISSUER.get(c, c)] = series
    return {"dates": ds, "flow": flow, "funds": funds, "pending": canary_note, "log": log,
            "source": "cryptoetf.today 分發行商" + ("＋Canary 官網" if cfg.get("canary") else "")}


def load_mcp(sym):
    src = load_json(P("data", "flows_mcp", f"{sym}.json"))
    if not src:
        return {}
    return {r["date"][:10]: float(r["flow"]) for r in src.get("rows", [])}


def build_canary_only(sym, cfg, pub):
    cf, last_rate = canary_flows(cfg["canary"])
    if not cf:
        return build_totals(sym, cfg, pub, note="Canary table unavailable")
    start = cfg["launch"]
    known_until = prev_trading_day(last_rate)
    ds = trading_days(start, known_until)
    flow = [r3(cf.get(d, 0.0)) for d in ds]
    # check against the MCP totals (settlement dated): totals(t) should equal Canary(prev(t))
    m = load_mcp(sym)
    pairs = [(m[next_trading_day(d)], cf.get(d, 0.0)) for d in ds if next_trading_day(d) in m]
    diff = max([abs(a - b) for a, b in pairs], default=None)
    return {"dates": ds, "flow": flow, "funds": {}, "log": [f"Canary {cfg['canary']} table only; MCP check n={len(pairs)} max|diff|={diff if diff is None else round(diff, 2)}"],
            "pending": {"fund": cfg["canary"], "dates": [next_trading_day(known_until)]} if pub.get(sym, known_until) > known_until else None,
            "source": "Canary 官網（流通單位 × NAV）"}


def build_totals(sym, cfg, pub, note=None):
    m = load_mcp(sym)
    if not m:
        return None
    start = max(cfg["launch"], min(m))
    end = pub.get(sym) or max(m)
    rows = {d: {"t": v} for d, v in m.items()}
    days_rows, log = fold_calendar(rows, start, end, ["t"])
    ds = sorted(days_rows)
    flow = [days_rows[d]["t"] for d in ds]
    pending = None
    if cfg.get("canary"):
        cf, last_rate = canary_flows(cfg["canary"])
        if cf is not None:
            # totals carry the Canary fund on settlement date: remove it and add its trade-dated value
            known_until = prev_trading_day(last_rate)
            settle = {next_trading_day(d): v for d, v in cf.items()}
            for i, d in enumerate(ds):
                flow[i] = flow[i] - settle.get(d, 0.0) + (cf.get(d, 0.0) if d <= known_until else 0.0)
            pend = [d for d in ds if d > known_until]
            if pend:
                pending = {"fund": cfg["canary"], "dates": pend}
            log.append(f"{cfg['canary']} moved from settlement to trade date using Canary's table")
    if note:
        log.append(note)
    return {"dates": ds, "flow": [r3(v) for v in flow], "funds": {}, "log": log, "pending": pending,
            "source": "cryptoetf.today 資產合計（近 30 天起逐日累積）" + ("＋Canary 官網" if cfg.get("canary") else "")}


def main():
    pub = published_dates()
    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "window_start": WINDOW_START, "reference_date": pub.get("_reference"), "units": "US$ millions",
           "coins": {}}
    for sym, cfg in COINS.items():
        try:
            if cfg["plan"] == "issuers":
                res = build_issuers(sym, cfg, pub)
            elif cfg["plan"] == "canary_only":
                res = build_canary_only(sym, cfg, pub)
            else:
                res = build_totals(sym, cfg, pub)
        except Exception as e:  # one bad source must not stop the others
            res = None
            print(sym, "failed:", type(e).__name__, e)
        if not res:
            out["coins"][sym] = {"launch": cfg["launch"], "etf": cfg["etf"], "dates": [], "flow": [], "error": "no data"}
            continue
        keep = [i for i, d in enumerate(res["dates"]) if d >= max(WINDOW_START, cfg["launch"])]
        res["dates"] = [res["dates"][i] for i in keep]
        res["flow"] = [res["flow"][i] for i in keep]
        res["funds"] = {k: [v[i] for i in keep] for k, v in res.get("funds", {}).items()}
        out["coins"][sym] = {"launch": cfg["launch"], "etf": cfg["etf"], "ref": cfg.get("ref", False), **res}
        print(f"{sym:5s} {res['dates'][0] if res['dates'] else '-'} .. {res['dates'][-1] if res['dates'] else '-'} "
              f"n={len(res['dates'])} sum={sum(res['flow']):.1f} pending={res.get('pending')} | {'; '.join(res.get('log', [])[:3])}")
    json.dump(out, open(P("data", "daily.json"), "w"), separators=(",", ":"), ensure_ascii=False)


if __name__ == "__main__":
    main()
