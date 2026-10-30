#!/usr/bin/env python3
"""Is there spot order-book history a GitHub runner can download? CoinKarma's LIQ is computed on spot
order books, while our history uses Binance's USD-M perpetual depth files (Binance publishes no spot
depth history). Read off CoinKarma's own chart (research/ck_chart), our perp-based BTC LIQ correlates
only about 0.3 with theirs, so a spot source would be worth a lot. Candidates: OKX's historical market
data (order books for spot as well as swaps?), Gate's public data downloads, Bybit's order-book archive.
Output: research/probe_spot/report.json (status, size and the start of each answer)."""
import datetime as dt
import gzip
import io
import json
import os
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "probe_spot")
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
REPORT = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "probes": {}}


def get(url, rng=None, timeout=40):
    h = {"User-Agent": UA}
    if rng:
        h["Range"] = f"bytes=0-{rng}"
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            body = r.read()
            return {"status": r.status, "len": int(r.headers.get("Content-Length") or len(body)), "range": r.headers.get("Content-Range"),
                    "type": r.headers.get("Content-Type"), "secs": round(time.time() - t0, 2)}, body
    except urllib.error.HTTPError as e:
        return {"status": e.code, "secs": round(time.time() - t0, 2)}, e.read()[:400]
    except Exception as e:
        return {"status": None, "error": str(e)[:200], "secs": round(time.time() - t0, 2)}, b""


def ms(d):
    return int(dt.datetime.combine(d, dt.time(), dt.timezone.utc).timestamp() * 1000)


def okx():
    base = "https://www.okx.com/api/v5/public/market-data-history"
    out = {}
    for label, d0, d1 in (("sep", dt.date(2026, 9, 10), dt.date(2026, 9, 12)), ("oct", dt.date(2026, 10, 26), dt.date(2026, 10, 28)),
                          ("year", dt.date(2025, 10, 1), dt.date(2025, 10, 2))):
        for module in (4, 5):
            for q in ("instType=SPOT&instIdList=BTC-USDT", "instType=SPOT&instFamilyList=BTC-USDT"):
                url = f"{base}?module={module}&{q}&dateAggrType=daily&begin={ms(d0)}&end={ms(d1)}"
                meta, body = get(url)
                try:
                    j = json.loads(body)
                    det = ((j.get("data") or [{}])[0].get("details") or [])
                    files = [(g.get("filename"), g.get("sizeMB")) for dd in det for g in dd.get("groupDetails") or []]
                    meta.update({"code": j.get("code"), "msg": j.get("msg"), "files": files[:6]})
                    if files and label == "oct" and module == 5 and "first_url" not in out:
                        out["first_url"] = det[0]["groupDetails"][0]["url"]
                except ValueError:
                    meta["head"] = body[:200].decode("utf-8", "replace")
                out[f"{label} m{module} {q.split('&')[1].split('=')[0]}"] = meta
                time.sleep(0.3)
    if out.get("first_url"):  # the start of one file
        meta, body = get(out["first_url"], rng=200000)
        meta["head"] = "binary"
        try:
            import tarfile
            tf = tarfile.open(fileobj=io.BytesIO(body), mode="r|gz")
            for m in tf:
                f = tf.extractfile(m)
                if f:
                    meta["member"] = m.name
                    meta["head"] = f.read(600).decode("utf-8", "replace")
                break
        except Exception as e:
            meta["tar_error"] = str(e)[:200]
        out["file_head"] = meta
    return out


def gate():
    out = {}
    urls = ["https://download.gatedata.org/spot/orderbooks/202610/BTC_USDT-2026102700.csv.gz",
            "https://download.gatedata.org/spot/orderbooks/202610/BTC_USDT-20261027.csv.gz",
            "https://download.gatedata.org/spot/orderbooks/202609/BTC_USDT-2026091000.csv.gz",
            "https://download.gatedata.org/spot/deals/202610/BTC_USDT-202610.csv.gz"]
    for u in urls:
        meta, body = get(u, rng=4000)
        if meta.get("status") in (200, 206):
            try:
                meta["head"] = gzip.GzipFile(fileobj=io.BytesIO(body)).read(800).decode("utf-8", "replace")
            except Exception:
                meta["head"] = body[:300].decode("utf-8", "replace")
        else:
            meta["head"] = body[:200].decode("utf-8", "replace")
        out[u] = meta
    return out


def bybit():
    out = {}
    for u in ("https://quote-saver.bycsi.com/orderbook/spot/BTCUSDT/2026-10-27_BTCUSDT_ob200.data.zip",
              "https://quote-saver.bycsi.com/orderbook/spot/BTCUSDT/2026-09-10_BTCUSDT_ob200.data.zip",
              "https://quote-saver.bycsi.com/orderbook/linear/BTCUSDT/2026-10-27_BTCUSDT_ob500.data.zip"):
        meta, body = get(u, rng=3000)
        meta["head"] = body[:120].hex() if meta.get("status") in (200, 206) else body[:200].decode("utf-8", "replace")
        out[u] = meta
    return out


def main():
    for name, fn in (("okx", okx), ("gate", gate), ("bybit", bybit)):
        try:
            REPORT["probes"][name] = fn()
        except Exception as e:
            REPORT["probes"][name] = {"error": f"{type(e).__name__}: {e}"}
        print(name, json.dumps(REPORT["probes"][name])[:1500], flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(REPORT, f, indent=1)


if __name__ == "__main__":
    main()
