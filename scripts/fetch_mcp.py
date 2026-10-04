#!/usr/bin/env python3
"""Daily net flows for the smaller ETF complexes from cryptoetf.today's open MCP
endpoint (no key; 60 requests/minute). The endpoint returns the last 30 days per
asset, so every run merges into data/flows_mcp/<SYMBOL>.json and history grows
day by day. Raw replies are kept in data/mcp_raw/ for auditing."""
import datetime as dt
import json
import os
import re
import time
import urllib.request

URL = "https://mcp.cryptoetf.today/api/mcp"
UA = "altcoin-etf-flows/1.0 (+https://github.com/cornell880503/altcoin-etf-flows)"
ASSETS = ["LINK", "HBAR", "AVAX", "DOGE", "LTC", "SUI", "DOT", "NEAR", "BNB", "TRX", "ZEC",
          "HYPE", "SOL", "XRP", "BTC", "ETH"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "flows_mcp")
RAW = os.path.join(ROOT, "data", "mcp_raw")


class MCP:
    def __init__(self):
        self.sid, self.n = None, 0

    def _post(self, payload):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": UA}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=40) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("Content-Type", "")
        if "event-stream" in ctype or body.lstrip().startswith(("event:", "data:")):
            msgs = [json.loads(l[5:].strip()) for l in body.splitlines() if l.startswith("data:") and l[5:].strip()]
            return msgs[-1] if msgs else None
        return json.loads(body) if body.strip() else None

    def call(self, method, params=None):
        self.n += 1
        return self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})

    def notify(self, method):
        try:
            self._post({"jsonrpc": "2.0", "method": method})
        except Exception:
            pass


def text_of(result):
    parts = (result or {}).get("result", {}).get("content", [])
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def rows_from(obj):
    """Find a list of {date, flow/total/net...} records anywhere in a parsed reply."""
    found = []
    def walk(x):
        if isinstance(x, list) and x and all(isinstance(i, dict) for i in x):
            if any("date" in i for i in x):
                found.append(x)
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    best = max(found, key=len) if found else []
    rows = []
    for i in best:
        d = str(i.get("date", ""))[:10]
        val = None
        for k in ("netFlow", "net_flow", "flow", "total", "net", "value", "netFlowUsd", "netFlowMillions"):
            if isinstance(i.get(k), (int, float)):
                val, key = float(i[k]), k
                break
        if re.match(r"\d{4}-\d{2}-\d{2}$", d) and val is not None:
            rows.append({"date": d, "flow": val, "field": key})
    return rows


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)
    m = MCP()
    init = m.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "altcoin-etf-flows", "version": "1.0"}})
    m.notify("notifications/initialized")
    tools = m.call("tools/list")
    json.dump({"initialize": init, "tools": tools}, open(os.path.join(RAW, "_server.json"), "w"), indent=1)
    schema = {}
    for t in (tools or {}).get("result", {}).get("tools", []):
        schema[t["name"]] = t.get("inputSchema", {})
    props = list((schema.get("get_asset_flows") or {}).get("properties", {}).keys())
    arg = props[0] if props else "asset"
    report = {"arg": arg, "assets": {}}
    summary = m.call("tools/call", {"name": "get_flows_summary", "arguments": {}})
    open(os.path.join(RAW, "_summary.txt"), "w").write(text_of(summary))
    for sym in ASSETS:
        time.sleep(1.5)
        try:
            res = m.call("tools/call", {"name": "get_asset_flows", "arguments": {arg: sym.lower()}})
            txt = text_of(res)
            open(os.path.join(RAW, f"{sym}.txt"), "w").write(txt)
            try:
                parsed = json.loads(txt)
            except ValueError:
                parsed = None
            new = rows_from(parsed) if parsed is not None else []
            path = os.path.join(OUT, f"{sym}.json")
            try:
                store = json.load(open(path))
            except (OSError, ValueError):
                store = {"asset": sym, "unit": "USD millions (as published by the MCP endpoint)", "rows": []}
            by_date = {r["date"]: r for r in store["rows"]}
            for r in new:
                by_date[r["date"]] = {"date": r["date"], "flow": r["flow"]}
            rows = [by_date[k] for k in sorted(by_date)]
            if rows != store["rows"]:
                store["rows"] = rows
                store["field"] = new[0]["field"] if new else store.get("field")
                json.dump(store, open(path, "w"), separators=(",", ":"))
            report["assets"][sym] = {"new_rows": len(new), "stored": len(rows),
                                     "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None}
        except Exception as e:
            report["assets"][sym] = {"error": str(e)[:200]}
    report["run_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json.dump(report, open(os.path.join(ROOT, "data", "mcp_report.json"), "w"), indent=1, sort_keys=True)
    print(json.dumps(report)[:2000])


if __name__ == "__main__":
    main()
