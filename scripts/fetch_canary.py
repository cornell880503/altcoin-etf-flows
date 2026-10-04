#!/usr/bin/env python3
"""Canary Capital primary data: daily shares outstanding and NAV for each Canary
ETF, read from the fund pages on canaryetfs.com. Flow(t) = (SO(t) - SO(t-1)) x NAV(t).
Canary dates these rows on settlement (T+1); build_daily.py moves them to trade date.

History is merged into data/canary/<TICKER>.csv on every run, so it keeps growing
even if the site only shows a recent window."""
import csv
import datetime as dt
import html
import json
import os
import re
import time
import urllib.request
from html.parser import HTMLParser

FUNDS = ["XRPC", "LTCC", "HBR", "SOLC", "SUIS", "TRXS"]
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 altcoin-etf-flows/1.0")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "canary")


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append({"rows": [], "row": None, "cell": None})
        elif self._stack:
            t = self._stack[-1]
            if tag == "tr":
                t["row"] = []
            elif tag in ("td", "th") and t["row"] is not None:
                t["cell"] = []

    def handle_endtag(self, tag):
        if not self._stack:
            return
        t = self._stack[-1]
        if tag in ("td", "th") and t["cell"] is not None and t["row"] is not None:
            t["row"].append(re.sub(r"\s+", " ", "".join(t["cell"])).strip())
            t["cell"] = None
        elif tag == "tr" and t["row"] is not None:
            if t["row"]:
                t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table":
            self.tables.append(self._stack.pop()["rows"])

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"].append(data)


def num(s):
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").replace("%", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def to_date(s):
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s[:20].strip(), fmt).date().isoformat()
        except ValueError:
            pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def pick(header, *needles, avoid=()):
    for i, h in enumerate(header):
        hl = h.lower()
        if all(n in hl for n in needles) and not any(a in hl for a in avoid):
            return i
    return None


def rows_from_tables(tables, fund):
    out = []
    for rows in tables:
        for hi, header in enumerate(rows[:3]):
            so = pick(header, "shares", "outstanding")
            nav = pick(header, "nav", avoid=("change", "%", "premium"))
            date = pick(header, "date")
            if so is None or nav is None or date is None:
                continue
            tick = pick(header, "ticker")
            na = pick(header, "net assets")
            for r in rows[hi + 1:]:
                if len(r) <= max(so, nav, date):
                    continue
                if tick is not None and tick < len(r) and r[tick].strip().upper() != fund:
                    continue
                d, s, n = to_date(r[date]), num(r[so]), num(r[nav])
                if d and s is not None and n:
                    out.append({"date": d, "shares_outstanding": s, "nav": n,
                                "net_assets": num(r[na]) if na is not None and na < len(r) else None})
            break
    return out


def rows_from_json(page, fund):
    """Fallback: JSON objects embedded in scripts with shares/NAV/date fields."""
    out = []
    txt = html.unescape(page).replace('\\"', '"')
    for m in re.finditer(r"\{[^{}]{20,800}\}", txt):
        blob = m.group(0)
        if "hares" not in blob or "NAV" not in blob.upper():
            continue
        try:
            o = json.loads(blob)
        except ValueError:
            continue
        keys = {k.lower().replace("_", " "): k for k in o}
        ks = next((keys[k] for k in keys if "shares" in k and "outstanding" in k), None)
        kn = next((keys[k] for k in keys if k in ("nav", "nav per share", "net asset value")), None)
        kd = next((keys[k] for k in keys if "date" in k), None)
        kt = next((keys[k] for k in keys if "ticker" in k), None)
        if not (ks and kn and kd):
            continue
        if kt and str(o[kt]).upper() != fund:
            continue
        d, s, n = to_date(o[kd]), num(o[ks]), num(o[kn])
        if d and s is not None and n:
            out.append({"date": d, "shares_outstanding": s, "nav": n, "net_assets": None})
    return out


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "funds": {}}
    for fund in FUNDS:
        info = {}
        try:
            page = fetch(f"https://canaryetfs.com/{fund.lower()}/")
            p = Tables()
            p.feed(page)
            info["bytes"] = len(page)
            info["tables"] = [{"n_rows": len(t), "head": t[:2]} for t in p.tables][:8]
            new = rows_from_tables(p.tables, fund)
            info["from"] = "table"
            if not new:
                new = rows_from_json(page, fund)
                info["from"] = "json"
            if not new:
                info["from"] = None
                hits = [m.start() for m in re.finditer(r"(?i)shares\s*outstanding", page)][:3]
                info["snippets"] = [re.sub(r"\s+", " ", page[max(0, h - 300):h + 500]) for h in hits]
                os.makedirs(os.path.join(OUT, "_raw"), exist_ok=True)
                open(os.path.join(OUT, "_raw", f"{fund}.html"), "w").write(page[:600000])
            # merge with stored history (newest value for a date wins)
            path = os.path.join(OUT, f"{fund}.csv")
            store = {}
            if os.path.exists(path):
                for r in csv.DictReader(open(path)):
                    store[r["date"]] = r
            before = dict(store)
            for r in new:
                store[r["date"]] = {k: ("" if v is None else v) for k, v in r.items()}
            if new and store != before:
                with open(path, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=["date", "shares_outstanding", "nav", "net_assets"])
                    w.writeheader()
                    for k in sorted(store):
                        w.writerow({c: store[k].get(c, "") for c in w.fieldnames})
            info.update(parsed=len(new), stored=len(store),
                        first=min(store) if store else None, last=max(store) if store else None)
        except Exception as e:  # keep going for the other funds
            info["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        report["funds"][fund] = info
        time.sleep(2)
    json.dump(report, open(os.path.join(OUT, "_report.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps({k: {kk: v.get(kk) for kk in ("parsed", "stored", "first", "last", "from", "error")}
                      for k, v in report["funds"].items()}, indent=1))


if __name__ == "__main__":
    main()
