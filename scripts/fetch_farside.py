#!/usr/bin/env python3
"""Backup source for the per-fund flows: Farside Investors' public tables (US$ millions).

Used only for a coin that cryptoetf.today did not deliver in this run (its API per-fund window,
scripts/fetch_api.py, or its flow page, scripts/fetch_flows.py), as happened in the
cryptoetf.today outage of 2026-10-18 ("Flow data is temporarily unavailable", API 502).
Farside covers BTC, ETH, SOL and HYPE (no XRP page). It only appends sessions after the last
stored one (it never rewrites stored days; when cryptoetf.today is back its window overwrites
them), and only if Farside agrees with the stored figures on the recent sessions both have.
Each ticker goes under the issuer key the history already uses (build_daily.TICKERS).
robots.txt there has no rule for other agents. Report: data/flows_farside/_report.json."""
import datetime as dt
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_daily  # noqa: E402
from fetch_flows import api_covered  # noqa: E402

UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows; daily research fetch)"
PAGES = {"BTC": "https://farside.co.uk/btc/", "ETH": "https://farside.co.uk/eth/",
         "SOL": "https://farside.co.uk/sol/", "HYPE": "https://farside.co.uk/hyp/"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "flows_farside")


class Rows(html.parser.HTMLParser):
    """Every table row as a list of cell texts."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def number(s):
    """'(71.8)' -> -71.8, '1,234.5' -> 1234.5, '-' or '' -> None (not reported)."""
    t = s.replace(",", "").replace("$", "").strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse(page, sym):
    """Rows keyed like data/flows/<SYM>.json, plus notes (tickers found, unknown tickers)."""
    p = Rows()
    p.feed(page)
    keymap = {t: k for k, t in build_daily.TICKERS.get(sym, {}).items()}
    header = next((r for r in p.rows if sum(1 for c in r if c in keymap) >= 2), None)
    if not header:
        return [], {"error": "no header row with known tickers", "table_rows": len(p.rows)}
    cols = {i: c for i, c in enumerate(header) if re.fullmatch(r"[A-Z]{2,5}", c or "")}
    out = []
    for r in p.rows:
        if not r:
            continue
        try:
            day = dt.datetime.strptime(r[0], "%d %b %Y").date().isoformat()
        except ValueError:
            continue
        row = {"date": day}
        for i, tk in cols.items():
            if i < len(r):
                v = number(r[i])
                if v is not None:
                    row[keymap.get(tk, tk)] = round(v, 4)
        if len(row) > 1:
            row["total"] = round(sum(v for k, v in row.items() if k != "date"), 4)
            out.append(row)
    out.sort(key=lambda x: x["date"])
    return out, {"tickers": [cols[i] for i in sorted(cols)], "unknown_tickers": [t for t in cols.values() if t not in keymap]}


def agree(old, new, last_n=10):
    """Compare the recent sessions both have. The two sources differ now and then on a small fund
    (BRRR +4.0M on 2026-10-14 at cryptoetf.today, 0 at Farside), so the test that matters is the
    funds that moved the most: a table read into the wrong columns fails it. Returns
    (main funds compared, of them differing, all figures compared, of them differing)."""
    stored = {r["date"]: r for r in old}
    both = [r for r in new if r["date"] in stored][-last_n:]
    keys = {k for r in both for k in stored[r["date"]] if k not in ("date", "total") and not k.endswith("Price")}
    size = {k: sum(abs(stored[r["date"]].get(k) or 0.0) for r in both) for k in keys}
    main = [k for k in sorted(size, key=size.get, reverse=True)[:3] if size[k] > 0]
    nm = bm = na = ba = 0
    for r in both:
        o = stored[r["date"]]
        for k in (set(r) | set(o)) - {"date", "total"}:
            if k.endswith("Price"):
                continue
            a, b = r.get(k) or 0.0, o.get(k) or 0.0
            off = abs(a - b) > max(0.15, 0.02 * abs(b))  # Farside rounds to 0.1
            na += 1
            ba += off
            if k in main:
                nm += 1
                bm += off
    return nm, bm, na, ba


def page_used():
    """Coins whose cryptoetf.today page delivered in this run (data/diagnostics.json)."""
    try:
        d = json.load(open(os.path.join(ROOT, "data", "diagnostics.json")))
    except (OSError, ValueError):
        return set()
    return {s for s, v in (d.get("assets") or {}).items() if v.get("used")}


def main():
    os.makedirs(OUT, exist_ok=True)
    done = api_covered() | page_used()
    report = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "assets": {}}
    for sym, url in PAGES.items():
        if sym in done:
            report["assets"][sym] = {"skipped": "cryptoetf.today delivered this run"}
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=40) as r:
                status, page = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            report["assets"][sym] = {"error": f"HTTP {e.code}"}
            continue
        except Exception as e:
            report["assets"][sym] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
            continue
        rows, note = parse(page, sym)
        rep = {"status": status, "bytes": len(page), "rows": len(rows), **note}
        if rows:
            rep["window"] = [rows[0]["date"], rows[-1]["date"]]
            path = os.path.join(ROOT, "data", "flows", f"{sym}.json")
            try:
                store = json.load(open(path))
            except (OSError, ValueError):
                store = None
            old = (store or {}).get("rows") or []
            nm, bm, na, ba = agree(old, rows)
            rep["vs_stored"] = {"main_funds": nm, "main_differ": bm, "all_figures": na, "all_differ": ba}
            if not old or not nm:
                rep["error"] = "no stored sessions to check Farside against; nothing appended"
            elif bm > 0.2 * nm:
                rep["error"] = f"{bm} of {nm} recent figures of the main funds differ from the stored ones; nothing appended"
            else:
                last = old[-1]["date"]
                add = [r for r in rows if r["date"] > last]
                if add:
                    store["rows"] = old + add
                    store["keys"] = sorted({k for r in store["rows"] for k in r if k != "date"})
                    store["appended_from_farside"] = sorted(set(store.get("appended_from_farside", [])) | {r["date"] for r in add})
                    json.dump(store, open(path, "w"), separators=(",", ":"))
                rep["appended"] = [r["date"] for r in add]
        report["assets"][sym] = rep
        time.sleep(2)
    json.dump(report, open(os.path.join(OUT, "_report.json"), "w"), indent=1, sort_keys=True)
    print(json.dumps({k: {kk: v.get(kk) for kk in ("skipped", "error", "window", "appended", "vs_stored")}
                      for k, v in report["assets"].items()}))


if __name__ == "__main__":
    main()
