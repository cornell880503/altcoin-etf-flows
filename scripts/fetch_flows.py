#!/usr/bin/env python3
"""Fetch daily per-fund net flows (US$ millions) for every US spot crypto ETF complex
tracked by cryptoetf.today and store them as JSON under data/flows/.

Two page layouts are understood:
- until 2026-10-10 each flow page embedded its full daily history as a JSON array named
  "historical" (one object per day: date, total and one field per issuer);
- since 2026-10-11 the page carries "cols" (the fund tickers) and "daily" (the last 30
  sessions, one cell per fund, null = not reported yet, a "partial" flag per session). That
  window is merged into the stored history: inside the window the page wins, older days are
  kept. Tickers are stored under the issuer keys the history already uses (build_daily.TICKERS).
robots.txt allows these pages (it disallows /api/ and /admin/ only). Files are rewritten only
when the data changed, so the repo history doubles as a revision log.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows; daily research fetch)"
BASE = "https://cryptoetf.today/en/{slug}-etf-flows"
# symbol -> page slug. Only these five pages embed their history in the HTML; the smaller assets'
# history was backfilled once (research/import_overview.py) and continues via the API.
ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp", "HYPE": "hype"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "flows")


PROBE = os.path.join(ROOT, "data", "probe", "flows")


def fetch(slug):
    """(page, info): the HTML and what the server said about it; page is None on an HTTP error."""
    req = urllib.request.Request(BASE.format(slug=slug), headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            info = {"status": r.status, "url": r.geturl()}
            info.update({h: r.headers.get(h) for h in ("Content-Type", "Server", "X-Vercel-Cache", "X-Nextjs-Cache", "Age",
                                                        "Last-Modified", "CF-Cache-Status") if r.headers.get(h)})
            return r.read().decode("utf-8", "replace"), info
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return None, {"status": e.code, "reason": str(e.reason), "body_head": body[:300]}


def page_markers(page):
    """What kind of page came back, for data/diagnostics.json when no flow table is found."""
    t = page.replace('\\"', '"')
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    dates = [m.start() for m in re.finditer(r'"date"\s*:\s*"20\d\d-\d\d-\d\d', t)]
    keys = sorted(set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\[\s*\{', t)))[:60]
    return {"title": title.group(1).strip()[:120] if title else None, "next_f_pushes": page.count("self.__next_f.push"),
            "next_data": "__NEXT_DATA__" in page, "date_fields": len(dates),
            "date_contexts": [t[max(0, i - 80):i + 60] for i in dates[:3]], "array_keys": keys,
            "script_srcs": re.findall(r'<script[^>]+src="([^"]+)"', page)[:8]}


def arrays_named(page, name):
    """Every JSON array that follows "<name>": in the page (RSC payloads escape quotes)."""
    t = page.replace('\\"', '"')
    pat = re.compile(r'"' + re.escape(name) + r'"\s*:\s*\[')
    out, pos = [], 0
    while True:
        m = pat.search(t, pos)
        if not m:
            return out
        start = m.end() - 1
        depth = 0
        for end in range(start, len(t)):
            c = t[end]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        out.append(json.loads(t[start:end + 1]))
                    except ValueError:
                        pass
                    break
        pos = start + 1


def ticker_keys(sym):
    """Fund ticker -> the issuer key the stored history uses."""
    import build_daily  # noqa: E402  (same folder; constants only at import)
    return {t: k for k, t in build_daily.TICKERS.get(sym, {}).items()}


def window_rows(page, sym):
    """The 2026-10-11 layout: rows for the last 30 sessions, keyed like the stored history, and
    notes (window, partial sessions, tickers not known yet)."""
    cols = next((a for a in arrays_named(page, "cols") if a and all(isinstance(c, dict) and c.get("ticker") for c in a)), None)
    daily = next((a for a in arrays_named(page, "daily")
                  if a and all(isinstance(d, dict) and "cells" in d and d.get("key") for d in a)), None)
    if not cols or not daily:
        return None, None
    keymap = ticker_keys(sym)
    tick = [c["ticker"] for c in cols]
    keys = [keymap.get(x, x) for x in tick]
    rows, partial = [], []
    for d in daily:
        cells = d.get("cells") or []
        if len(cells) != len(keys):
            continue
        row = {"date": str(d["key"])[:10]}
        for k, v in zip(keys, cells):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                row[k] = round(float(v), 4)
        if isinstance(d.get("total"), (int, float)) and not isinstance(d.get("total"), bool):
            row["total"] = round(float(d["total"]), 4)
        rows.append(row)
        if d.get("partial"):
            partial.append(row["date"])
    rows.sort(key=lambda r: r["date"])
    if not rows:
        return None, None
    return rows, {"window": [rows[0]["date"], rows[-1]["date"]], "sessions": len(rows), "partial": sorted(partial),
                  "unknown_tickers": [x for x in tick if x not in keymap]}


def merge_window(old, new):
    """The stored history with the page's window laid over it: every stored row inside the
    window's dates (weekend rows too) is replaced, older rows are kept."""
    lo, hi = new[0]["date"], new[-1]["date"]
    return sorted([r for r in (old or []) if not lo <= r["date"] <= hi] + new, key=lambda r: r["date"])


def clean_rows(rows):
    keep = []
    for r in rows:
        if not isinstance(r, dict) or "date" not in r:
            continue
        row = {"date": str(r["date"])[:10]}
        for k, v in r.items():
            if k == "date":
                continue
            if isinstance(v, (int, float)) and v is not True and v is not False:
                row[k] = round(float(v), 4)
        keep.append(row)
    keep.sort(key=lambda r: r["date"])
    return keep


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest_path = os.path.join(ROOT, "data", "manifest.json")
    try:
        manifest = json.load(open(manifest_path))
    except (OSError, ValueError):
        manifest = {"assets": {}}
    changed, failures, diag = [], [], {}
    for sym, slug in ASSETS.items():
        try:
            page, info = fetch(slug)
        except Exception as e:  # network error: keep the previous file
            failures.append(f"{sym}: {e}")
            diag[sym] = {"error": str(e)[:200]}
            time.sleep(2)
            continue
        if page is None:  # HTTP error: keep the previous file
            failures.append(f"{sym}: HTTP {info.get('status')}")
            diag[sym] = info
            time.sleep(2)
            continue
        t = page.replace('\\"', '"')
        names = sorted(set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\[\s*\{\s*"date"', t)))
        diag[sym] = {"bytes": len(page), "historical_mentions": t.count('"historical"'), "dated_arrays": names, **info}
        cands = []
        for nm in ["historical"]:  # the full per-fund history (layout until 2026-10-10); other dated arrays hold totals only
            for a in arrays_named(page, nm):
                c = clean_rows(a)
                if c and any(k != "date" for k in c[0]) and any(("total" in r) for r in c):
                    cands.append(c)
            if cands:
                diag[sym]["used"] = nm
                break
        if not cands:  # the 2026-10-11 layout: the last 30 sessions per fund
            new, note = window_rows(page, sym)
            if new:
                path = os.path.join(OUT, f"{sym}.json")
                try:
                    old = json.load(open(path)).get("rows")
                except (OSError, ValueError):
                    old = None
                diag[sym].update({"used": "cols+daily", **note})
                if old and old[-1]["date"] < new[0]["date"]:
                    # sessions between the stored history and the window are lost: say so loudly
                    failures.append(f"{sym}: gap between stored history ({old[-1]['date']}) and the page window ({new[0]['date']})")
                totals = {d.get("date"): d.get("flow") for a in arrays_named(page, "days") for d in a
                          if isinstance(d, dict) and isinstance(d.get("flow"), (int, float))}
                diag[sym]["total_mismatch"] = [r["date"] for r in new if r["date"] in totals and "total" in r
                                               and abs(totals[r["date"]] - r["total"]) > 0.05]
                cands = [merge_window(old, new)]
        if not cands:
            failures.append(f"{sym}: no dated flow array")
            diag[sym].update(page_markers(page))
            if sym in ("BTC", "SOL"):  # keep a copy to see what changed (overwritten each failing run)
                os.makedirs(PROBE, exist_ok=True)
                open(os.path.join(PROBE, f"{sym}.html"), "w", encoding="utf-8").write(page)
            time.sleep(2)
            continue
        rows = max(cands, key=len)  # the daily flow table is the longest dated array
        path = os.path.join(OUT, f"{sym}.json")
        try:
            old = json.load(open(path)).get("rows")
        except (OSError, ValueError):
            old = None
        if rows != old:
            keys = sorted({k for r in rows for k in r if k != "date"})
            json.dump({"asset": sym, "slug": slug, "source": BASE.format(slug=slug), "unit": "USD millions",
                       "keys": keys, "rows": rows}, open(path, "w"), separators=(",", ":"))
            manifest["assets"][sym] = {"rows": len(rows), "first": rows[0]["date"], "last": rows[-1]["date"],
                                       "keys": keys}
            changed.append(sym)
        time.sleep(2)
    if changed:
        manifest["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest["changed"] = changed
        json.dump(manifest, open(manifest_path, "w"), indent=1, sort_keys=True)
    json.dump({"failures": failures, "assets": diag}, open(os.path.join(ROOT, "data", "diagnostics.json"), "w"), indent=1, sort_keys=True)
    print("changed:", changed)
    print("failures:", failures)
    # fail the run (so it shows red in Actions) only if none of the core assets could be read
    failed = {f.split(":")[0] for f in failures}
    if {"BTC", "SOL", "XRP"} <= failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
