#!/usr/bin/env python3
"""Build the self-hosted dashboard: one static page with every trading day embedded.

Inputs (all in this repo):
  data/daily.json        cleaned daily net flows per coin and fund (scripts/build_daily.py)
  data/coin_px/*.json    each coin's own price at 16:00 New York (scripts/fetch_coin_px.py)
  site/template.html     the dashboard (statistics run in the browser, same code as make_docs)
  site/research.json     fixed research results (SOL/XRP/BTC study, BTC flow study, rule tests)

Usage: python3 scripts/build_site.py --out DIR
Writes DIR/index.html, DIR/health.json and DIR/.nojekyll (for GitHub Pages). Standard library only. Exits non-zero, without
writing, when the data look wrong, so a broken build never replaces a good page."""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_docs as MD  # noqa: E402
from calendar_us import trading_days  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)  # noqa: E731
ALTS = ["SOL", "XRP", "HYPE", "ZEC", "LINK", "DOGE", "AVAX", "HBAR", "LTC", "SUI", "DOT", "NEAR", "BNB", "TRX"]
REFS = ["BTC", "ETH"]
THIN = {"TDOT", "VAVX", "VBNB", "TRXS"}  # funds too small for the bp-of-market-cap view to mean much
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='7' fill='%230e1116'/%3E"
           "%3Crect x='7' y='17' width='4' height='8' rx='1' fill='%233987e5'/%3E"
           "%3Crect x='14' y='11' width='4' height='14' rx='1' fill='%233987e5'/%3E"
           "%3Crect x='21' y='6' width='4' height='19' rx='1' fill='%23e2b45c'/%3E%3C/svg%3E")


def snapshot(daily, built_at):
    px = MD.load_px(daily)
    allds = sorted({d for c in daily["coins"].values() for d in c["dates"]})
    cal = trading_days(daily["window_start"], allds[-1])
    docs = MD.build_docs(daily, {}, cal, px)  # no ETF closes: every statistic uses the coin price
    dates = sorted(docs)
    coins = {}
    for s in ALTS + REFS:
        f, x, p, mc, funds = [], [], [], [], {}
        for i, d in enumerate(dates):
            e = docs[d]["coins"].get(s, {})
            f.append(e.get("flow"))
            x.append(e.get("px"))
            p.append(e.get("pending"))
            mc.append(e.get("mcap"))
            for tk, v in (e.get("funds") or {}).items():
                funds.setdefault(tk, [None] * len(dates))[i] = v
        coins[s] = {"f": f, "x": x, "m": mc, "p": p if any(p) else None, "funds": funds or None}
    meta = {}
    for s in ALTS + REFS:
        dc = daily["coins"][s]
        first = dc["dates"][0] if dc["dates"] else None
        meta[s] = {"etf": dc["etf"], "launch": dc["launch"], "source": dc.get("source", ""),
                   "flowFrom": first, "full": bool(first and first <= max(dc["launch"], daily["window_start"])),
                   "thin": dc["etf"] in THIN, "gaps": dc.get("gaps") or []}
    snap = {"mode": "site", "asof": dates[-1], "built": built_at[:10], "built_at": built_at,
            "dates": dates, "coins": coins, "meta": meta}
    return snap, px


def check(snap, px):
    """Refuse to publish a page built from broken data."""
    dates = snap["dates"]
    problems = []
    if len(dates) < 100:
        problems.append(f"only {len(dates)} trading days")
    last = dates[-1] if dates else None
    if last and (dt.date.today() - dt.date.fromisoformat(last)).days > 10:
        problems.append(f"latest trading day {last} is more than 10 days old")
    for s in ("BTC", "ETH", "SOL", "XRP"):
        if not any(v is not None for v in snap["coins"][s]["f"][-5:]):
            problems.append(f"{s}: no flows in the last 5 trading days")
        if not any(v is not None for v in snap["coins"][s]["x"][-5:]):
            problems.append(f"{s}: no coin price in the last 5 trading days")
    missing = [s for s in ALTS + REFS if last and px.get(s, {}).get(last) is None]
    return problems, missing


def json_for_script(obj):
    # safe inside <script type="application/json">: no "</" sequence can close the tag early
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render(snap, research, built_at):
    tpl = open(P("site", "template.html"), encoding="utf-8").read()
    body = tpl.replace("/*SNAP*/", json_for_script(snap)).replace("/*DATA*/", json_for_script(research))
    if "/*SNAP*/" in body or "/*DATA*/" in body:
        raise SystemExit("template placeholders not filled")
    cut = body.index('<div class="wrap">')
    head, rest = body[:cut].strip(), body[cut:].strip()
    return "\n".join([
        "<!doctype html>",
        '<html lang="zh-Hant">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="description" content="美國現貨山寨幣 ETF 每日淨流量，和各幣幣價的相關、領先落後的顯著性，以及流量交易規則的研究。每 3 小時自動更新。">',
        '<meta name="theme-color" content="#0e1116">',
        '<meta name="robots" content="noindex">',
        f'<meta name="altetf-built" content="{built_at}">',
        f'<link rel="icon" href="{FAVICON}">',
        head,
        "</head>",
        "<body>",
        rest,
        "</body>",
        "</html>",
        "",
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    built_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    daily = json.load(open(P("data", "daily.json"), encoding="utf-8"))
    snap, px = snapshot(daily, built_at)
    problems, missing = check(snap, px)
    if problems:
        print("not building the site:", "; ".join(problems), file=sys.stderr)
        sys.exit(1)
    research = json.load(open(P("site", "research.json"), encoding="utf-8"))
    research.pop("_about", None)
    html = render(snap, research, built_at)
    os.makedirs(a.out, exist_ok=True)
    tmp = os.path.join(a.out, ".index.html.tmp")
    open(tmp, "w", encoding="utf-8").write(html)
    os.replace(tmp, os.path.join(a.out, "index.html"))
    health = {"built_at": built_at, "latest_date": snap["asof"], "trading_days": len(snap["dates"]),
              "missing_price_on_latest": missing, "bytes": len(html.encode())}
    json.dump(health, open(os.path.join(a.out, "health.json"), "w"), indent=1)
    open(os.path.join(a.out, ".nojekyll"), "w").close()  # GitHub Pages: serve the files as they are
    print(json.dumps(health, ensure_ascii=False))


if __name__ == "__main__":
    main()
