#!/usr/bin/env python3
"""Daily net flows from the official cryptoetf.today REST API (needs the repository
secret CRYPTOETF_API_KEY; skipped quietly without it).

1. Per fund, every run, for the coins whose history is kept fund by fund (BTC ETH SOL XRP HYPE):
   GET /flows/{asset}/funds -> {days:[{date, totalUsdM, funds:[{ticker, issuer, netFlowUsdM}]}]}
   (the last 30 days; a fund that has not reported yet is null, not 0). The window is laid over
   data/flows/<SYM>.json (inside it the API wins, older days are kept), each ticker under the
   issuer key the history already uses. This is the main source since the site's flow pages
   stopped carrying the data (2026-10-18); scripts/fetch_flows.py only fills in for a coin the
   API could not deliver. Report: data/flows_api/_funds_report.json (including how the API's
   figures compare with the stored ones on the days both have).
2. Asset totals, once a day: GET /flows/{asset} -> {days:[{date, netFlowUsdM}]}, merged into
   data/flows_api/<SYM>.json.
The key never leaves the environment variable: nothing here writes it to disk or prints it."""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

BASE = "https://api.cryptoetf.today/api/v1"
CODES = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp", "HYPE": "hyp", "DOGE": "doge", "LINK": "link",
         "AVAX": "avax", "HBAR": "hbar", "LTC": "ltc", "BNB": "bnb", "DOT": "dot", "SUI": "sui", "NEAR": "near",
         "TRX": "trx", "ZEC": "zec"}
MIN_HOURS_BETWEEN = 20  # the source updates once a day, before the US open
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "flows_api")
UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows)"


def get(path, key):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                                                       "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        limits = {h: r.headers.get(h) for h in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")}
        return json.loads(r.read().decode("utf-8")), limits


FUNDS = ("BTC", "ETH", "SOL", "XRP", "HYPE")  # coins kept fund by fund in data/flows/<SYM>.json


def fund_rows(data, keymap):
    """API days -> rows keyed like data/flows/<SYM>.json (issuer keys; a null fund is left out)."""
    rows, partial, unknown = [], [], set()
    for d in data.get("days") or []:
        if not isinstance(d, dict) or not d.get("date"):
            continue
        row = {"date": str(d["date"])[:10]}
        nulls = 0
        for f in d.get("funds") or []:
            tk, v = f.get("ticker"), f.get("netFlowUsdM")
            if not tk:
                continue
            k = keymap.get(tk)
            if k is None:
                unknown.add(tk)
                k = tk
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                row[k] = round(float(v), 4)
            else:
                nulls += 1
        tot = d.get("totalUsdM")
        if isinstance(tot, (int, float)) and not isinstance(tot, bool):
            row["total"] = round(float(tot), 4)
        if nulls:
            partial.append(row["date"])
        rows.append(row)
    rows.sort(key=lambda r: r["date"])
    return rows, partial, sorted(unknown)


def compare(old, new):
    """How the API's figures compare with the stored ones on the dates both have."""
    stored = {r["date"]: r for r in old or []}
    n, bad = 0, []
    for r in new:
        o = stored.get(r["date"])
        if not o:
            continue
        for k in (set(r) | set(o)) - {"date"}:
            if k.endswith("Price") or k == "total":
                continue
            n += 1
            a, b = r.get(k) or 0.0, o.get(k) or 0.0
            if abs(a - b) > 0.05:
                bad.append([r["date"], k, round(b, 3), round(a, 3)])
    return {"compared": n, "differ": len(bad), "examples": bad[:8]}


def fetch_funds(key, now):
    """Per-fund windows for FUNDS, every run."""
    import build_daily  # noqa: E402  (same folder; constants only at import)
    from fetch_flows import merge_window  # noqa: E402
    report = {"run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "assets": {}}
    for sym in FUNDS:
        try:
            data, limits = get(f"/flows/{CODES[sym]}/funds", key)
            keymap = {t: k for k, t in build_daily.TICKERS.get(sym, {}).items()}
            rows, partial, unknown = fund_rows(data, keymap)
            if not rows:
                report["assets"][sym] = {"error": "no days in the reply"}
                continue
            path = os.path.join(ROOT, "data", "flows", f"{sym}.json")
            try:
                store = json.load(open(path))
            except (OSError, ValueError):
                store = {"asset": sym, "unit": "USD millions", "rows": []}
            old = store.get("rows") or []
            merged = merge_window(old, rows)
            changed = merged != old
            if changed:
                store.update(rows=merged, keys=sorted({k for r in merged for k in r if k != "date"}),
                             source_window="api.cryptoetf.today /v1/flows/{asset}/funds")
                json.dump(store, open(path, "w"), separators=(",", ":"))
            report["assets"][sym] = {"ok": True, "window": [rows[0]["date"], rows[-1]["date"]], "days": len(rows),
                                     "partial": partial, "unknown_tickers": unknown, "changed": changed,
                                     "gap": bool(old and old[-1]["date"] < rows[0]["date"]),
                                     "vs_stored": compare(old, rows), "window_days": data.get("windowDays"),
                                     "rate_limit": limits}
        except urllib.error.HTTPError as e:
            report["assets"][sym] = {"error": f"HTTP {e.code}"}
            if e.code in (401, 403):
                break  # bad key: stop instead of burning requests
            if e.code == 429:
                time.sleep(20)
        except Exception as e:  # keep going for the other coins
            report["assets"][sym] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
        time.sleep(1.2)
    json.dump(report, open(os.path.join(OUT, "_funds_report.json"), "w"), indent=1, sort_keys=True)
    print("per-fund:", json.dumps({k: {kk: v.get(kk) for kk in ("window", "changed", "partial", "error")}
                                   for k, v in report["assets"].items()}))


def main():
    key = os.environ.get("CRYPTOETF_API_KEY", "").strip()
    os.makedirs(OUT, exist_ok=True)
    rep_path = os.path.join(OUT, "_report.json")
    try:
        prev = json.load(open(rep_path))
    except (OSError, ValueError):
        prev = {}
    now = dt.datetime.now(dt.timezone.utc)
    if not key:
        print("CRYPTOETF_API_KEY not set; skipping the REST API")
        return
    fetch_funds(key, now)
    last = prev.get("last_success")
    if last and (now - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)).total_seconds() < MIN_HOURS_BETWEEN * 3600:
        print("fetched less than", MIN_HOURS_BETWEEN, "hours ago; skipping")
        return
    report = {"run_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "assets": {}, "last_success": prev.get("last_success")}
    ok = 0
    for sym, code in CODES.items():
        try:
            data, limits = get(f"/flows/{code}", key)
            rows = [{"date": str(d["date"])[:10], "flow": float(d["netFlowUsdM"])}
                    for d in data.get("days", []) if d.get("date") and isinstance(d.get("netFlowUsdM"), (int, float))]
            path = os.path.join(OUT, f"{sym}.json")
            try:
                store = json.load(open(path))
            except (OSError, ValueError):
                store = {"asset": sym, "source": BASE + "/flows/" + code, "unit": "USD millions", "rows": []}
            by_date = {r["date"]: r for r in store["rows"]}
            for r in rows:
                by_date[r["date"]] = r
            merged = [by_date[k] for k in sorted(by_date)]
            if merged != store["rows"] or store.get("windowDays") != data.get("windowDays"):
                store.update(rows=merged, windowDays=data.get("windowDays"), updatedAt=data.get("updatedAt"))
                json.dump(store, open(path, "w"), separators=(",", ":"))
            report["assets"][sym] = {"window_days": data.get("windowDays"), "returned": len(rows), "stored": len(merged),
                                     "first": merged[0]["date"] if merged else None, "last": merged[-1]["date"] if merged else None,
                                     "rate_limit": limits}
            ok += 1
        except urllib.error.HTTPError as e:
            report["assets"][sym] = {"error": f"HTTP {e.code}"}
            if e.code in (401, 403):
                break  # bad key: stop instead of burning requests
            if e.code == 429:
                time.sleep(20)
        except Exception as e:  # keep going for the other assets
            report["assets"][sym] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        time.sleep(1.2)
    if ok:
        report["last_success"] = report["run_at"]
    json.dump(report, open(rep_path, "w"), indent=1, sort_keys=True)
    print(json.dumps({k: {kk: v.get(kk) for kk in ("window_days", "stored", "last", "error")} for k, v in report["assets"].items()}))


if __name__ == "__main__":
    main()
