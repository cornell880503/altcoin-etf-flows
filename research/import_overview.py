#!/usr/bin/env python3
"""One-off backfill of the smaller assets' flow history (since each ETF's launch).

cryptoetf.today's public flow pages (/en/<slug>-etf-flows) draw their charts from one JSON
document that the page loads for every visitor (per-fund daily flows, ~400 days, 11 smaller
assets). research/probe_site.py rendered the pages once in a browser on 2026-10-04 and kept that
response (data/probe/<page>/network.json). This script extracts it into data/flows_hist/<SYM>.json.
Nothing here calls the site; later days come from the official API (scripts/fetch_api.py)."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = [os.path.join(ROOT, "data", "probe", p, "network.json") for p in ("chainlink", "dogecoin", "hedera")]
OUT = os.path.join(ROOT, "data", "flows_hist")


def main():
    doc = None
    for f in SRC:
        if not os.path.exists(f):
            continue
        for n in json.load(open(f)):
            if "/api/etf-issuers/overview" in n.get("url", "") and n.get("body"):
                d = json.loads(n["body"])
                if doc is None or d.get("generatedAt", "") > doc.get("generatedAt", ""):
                    doc = d
    if doc is None:
        raise SystemExit("no captured overview document found")
    os.makedirs(OUT, exist_ok=True)
    meta = {"asOf": doc.get("asOf"), "generatedAt": doc.get("generatedAt"), "captured": "2026-10-04",
            "how": "response loaded by https://cryptoetf.today/en/<slug>-etf-flows during one browser render",
            "assets": {}}
    for a in doc["assets"]:
        sym = a["asset"]
        per = {}
        for tk, rows in (a.get("fundFlows") or {}).items():
            for r in rows:
                if isinstance(r.get("flow"), (int, float)):
                    per.setdefault(r["date"][:10], {})[tk] = float(r["flow"])
        rows = []
        for r in a.get("flows", []):
            d = r["date"][:10]
            rows.append({"date": d, "total": float(r.get("total") or 0.0), "source": r.get("source"),
                         "fundsCovered": r.get("fundsCovered"), "price": r.get("price"), "funds": per.get(d, {})})
        rows.sort(key=lambda r: r["date"])
        funds = [{k: f.get(k) for k in ("ticker", "name", "issuer", "exchange", "listedAt", "hasHistory")} for f in a.get("funds", [])]
        json.dump({"asset": sym, "unit": "USD millions", "asOf": doc.get("asOf"), "funds": funds, "rows": rows},
                  open(os.path.join(OUT, f"{sym}.json"), "w"), separators=(",", ":"))
        meta["assets"][sym] = {"first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
                               "rows": len(rows), "fullHistory": a.get("fullHistory"), "cumulative": a.get("cumulative"),
                               "sources": sorted({str(r["source"]) for r in rows})}
    json.dump(meta, open(os.path.join(OUT, "_meta.json"), "w"), indent=1)
    print(json.dumps(meta["assets"], indent=0)[:3000])


if __name__ == "__main__":
    main()
