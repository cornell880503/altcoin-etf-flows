#!/usr/bin/env python3
"""One-off probe (runs in GitHub Actions, which can reach the sites): how do the flow pages of
the smaller assets on cryptoetf.today expose their daily history, and how does strategy.com
publish its bitcoin purchase table? Saves the raw HTML, the rendered text and tables, and the
JSON / RSC responses the page itself loads while rendering (no extra requests are made beyond
one normal page view per URL). Output: data/probe/<name>/..."""
import json
import os
import re
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "probe")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
PAGES = {
    "chainlink": "https://cryptoetf.today/en/chainlink-etf-flows",
    "dogecoin": "https://cryptoetf.today/en/dogecoin-etf-flows",
    "hedera": "https://cryptoetf.today/en/hedera-etf-flows",
    "strategy": "https://www.strategy.com/purchases",
}
ROBOTS = ["https://cryptoetf.today/robots.txt", "https://www.strategy.com/robots.txt"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {"robots": {}, "pages": {}}
    for u in ROBOTS:
        try:
            st, body = get(u)
            report["robots"][u] = body[:4000]
        except Exception as e:
            report["robots"][u] = f"ERR {e}"
    for name, url in PAGES.items():
        d = os.path.join(OUT, name)
        os.makedirs(d, exist_ok=True)
        try:
            st, html = get(url)
            open(os.path.join(d, "raw.html"), "w").write(html)
            t = html.replace('\\"', '"')
            report["pages"][name] = {"status": st, "bytes": len(html),
                                     "date_like": len(re.findall(r'20\d\d-\d\d-\d\d', t)),
                                     "keys_before_arrays": sorted(set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\[\s*[\{\[]', t)))[:200]}
        except Exception as e:
            report["pages"][name] = {"error": str(e)[:300]}
        time.sleep(2)
    # render each page once in a headless browser and keep what the page itself fetched
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome")
            except Exception:
                browser = p.chromium.launch()
            for name, url in PAGES.items():
                d = os.path.join(OUT, name)
                net = []
                ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
                page = ctx.new_page()

                def on_response(resp, net=net):
                    try:
                        req = resp.request
                        item = {"url": resp.url, "status": resp.status, "method": req.method, "type": req.resource_type,
                                "ct": resp.headers.get("content-type", "")}
                        if req.resource_type in ("fetch", "xhr") or "json" in item["ct"] or "x-component" in item["ct"]:
                            body = resp.text()
                            item["len"] = len(body)
                            item["body"] = body[:3_000_000]
                        net.append(item)
                    except Exception as e:  # body unavailable (redirect, etc.)
                        net.append({"url": resp.url, "error": str(e)[:200]})
                page.on("response", on_response)
                try:
                    page.goto(url, wait_until="networkidle", timeout=90000)
                    page.wait_for_timeout(4000)
                    for _ in range(12):  # scroll so lazy sections load
                        page.mouse.wheel(0, 2500)
                        page.wait_for_timeout(400)
                    page.wait_for_timeout(2000)
                    buttons = page.eval_on_selector_all("button, a", "els => els.map(e => (e.innerText || '').trim()).filter(t => t && t.length < 40)")
                    text = page.inner_text("body")
                    tables = page.eval_on_selector_all("table", "els => els.map(t => t.innerText)")
                    json.dump({"url": url, "buttons": buttons[:300], "tables": tables, "text": text[:400000]},
                              open(os.path.join(d, "rendered.json"), "w"), ensure_ascii=False)
                    report["pages"].setdefault(name, {})["rendered"] = {"tables": len(tables), "rows_in_tables": [t.count("\n") for t in tables],
                                                                        "text_len": len(text)}
                except Exception as e:
                    report["pages"].setdefault(name, {})["render_error"] = str(e)[:300]
                json.dump(net, open(os.path.join(d, "network.json"), "w"), ensure_ascii=False)
                report["pages"].setdefault(name, {})["network"] = [{k: v for k, v in n.items() if k != "body"} for n in net
                                                                  if n.get("type") in ("fetch", "xhr") or "json" in n.get("ct", "")][:80]
                ctx.close()
                time.sleep(2)
            browser.close()
    except Exception as e:
        report["playwright_error"] = str(e)[:500]
    json.dump(report, open(os.path.join(OUT, "report.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps({k: (v if k != "pages" else {n: {kk: vv for kk, vv in x.items() if kk != "network"} for n, x in v.items()})
                      for k, v in report.items() if k != "robots"}, ensure_ascii=False)[:3000])


if __name__ == "__main__":
    main()
