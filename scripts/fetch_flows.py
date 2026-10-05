#!/usr/bin/env python3
"""Fetch daily per-fund net flows (US$ millions) for every US spot crypto ETF complex
tracked by cryptoetf.today and store them as JSON under data/flows/.

Each flow page embeds its full daily history as a JSON array named "historical"
(one object per day: date, total and one field per issuer). robots.txt allows these
pages (it disallows /api/ and /admin/ only). Files are rewritten only when the data
changed, so the repo history doubles as a revision log.
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows; daily research fetch)"
BASE = "https://cryptoetf.today/en/{slug}-etf-flows"
# symbol -> page slug. Only these five pages embed their history in the HTML; the smaller assets'
# history was backfilled once (research/import_overview.py) and continues via the API.
ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp", "HYPE": "hype"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "flows")


def fetch(slug):
    req = urllib.request.Request(BASE.format(slug=slug), headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


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
            page = fetch(slug)
        except Exception as e:  # missing page or network error: keep the previous file
            failures.append(f"{sym}: {e}")
            diag[sym] = {"error": str(e)[:200]}
            time.sleep(2)
            continue
        t = page.replace('\\"', '"')
        names = sorted(set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\[\s*\{\s*"date"', t)))
        diag[sym] = {"bytes": len(page), "historical_mentions": t.count('"historical"'), "dated_arrays": names}
        cands = []
        for nm in (["historical"] + [n for n in names if n != "historical"]):
            for a in arrays_named(page, nm):
                c = clean_rows(a)
                if c and any(k != "date" for k in c[0]) and any(("total" in r) for r in c):
                    cands.append(c)
            if cands:
                diag[sym]["used"] = nm
                break
        if not cands:
            failures.append(f"{sym}: no dated flow array")
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
