#!/usr/bin/env python3
"""Daily open interest (OI) of every Binance USD-M perpetual, from Binance's public data files.

Binance's live API refuses US cloud IPs (HTTP 451, see research/probe_oi), but data.binance.vision
publishes one file per contract per UTC day (futures/um/daily/metrics: a row every 5 minutes; from
2021-12-01, BTC from 2020-09). Per USDT contract and day this keeps:
  oi_last   OI in US$ at the day's last row (about 23:55 UTC)
  oi_mean   mean OI in US$ over the day
  qty_last  OI in coins at the last row (oi_last / qty_last = the contract's price then)
  ls        mean long/short account ratio (count_long_short_ratio)
  taker     mean taker buy/sell volume ratio (sum_taker_long_short_vol_ratio)
  n         rows in the file (288 for a complete day)
Output: data/oi/binance/<YYYY-MM>.csv.gz and _meta.json (contracts, first/last day, how many hours
after midnight UTC Binance published the latest files). Incremental and time-boxed: only
(contract, day) pairs not stored yet are fetched, and no new download starts after --budget
minutes, so the workflow's commit step always runs. Stocks, gold and other non-crypto contracts are
stored too; the radar's build step decides what counts as an altcoin.
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import http.client
import io
import json
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "oi", "binance")
HOST, S3_HOST = "data.binance.vision", "s3-ap-northeast-1.amazonaws.com"
PREFIX = "data/futures/um/daily/metrics/"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
FIELDS = ["date", "symbol", "oi_last", "oi_mean", "qty_last", "ls", "taker", "n"]
_local = threading.local()


def get(host, path, tries=5):
    """GET over a per-thread keep-alive connection. Returns (status, body); 404 is not retried."""
    for k in range(tries):
        conns = _local.__dict__.setdefault("conns", {})
        c = conns.get(host) or http.client.HTTPSConnection(host, timeout=40)
        conns[host] = c
        try:
            c.request("GET", path, headers={"User-Agent": UA, "Accept": "*/*"})
            r = c.getresponse()
            body = r.read()
            if r.status in (200, 404):
                return r.status, body
            if r.status not in (403, 429, 500, 502, 503, 504):
                return r.status, body
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass
        conns.pop(host, None)
        time.sleep(1.5 * (k + 1))
    return None, b""


def s3_list(prefix, delimiter=None, marker=None):
    q = {"prefix": prefix}
    if delimiter:
        q["delimiter"] = delimiter
    if marker:
        q["marker"] = marker
    st, body = get(S3_HOST, "/data.binance.vision?" + urllib.parse.urlencode(q))
    if st != 200:
        raise RuntimeError(f"S3 list {prefix}: HTTP {st}")
    tag = lambda el: el.tag.split("}", 1)[-1]  # noqa: E731
    keys, prefixes, truncated, nxt = [], [], False, None
    for el in ET.fromstring(body):
        t = tag(el)
        if t == "Contents":
            d = {tag(c): c.text for c in el}
            keys.append((d.get("Key"), d.get("LastModified")))
        elif t == "CommonPrefixes":
            prefixes += [c.text for c in el if tag(c) == "Prefix"]
        elif t == "IsTruncated":
            truncated = el.text == "true"
        elif t == "NextMarker":
            nxt = el.text
    return keys, prefixes, truncated, nxt


def s3_list_all(prefix, delimiter=None):
    keys, prefixes, marker = [], [], None
    for _ in range(200):
        k, p, trunc, nxt = s3_list(prefix, delimiter, marker)
        keys += k
        prefixes += p
        if not trunc or not (k or p):
            break
        marker = nxt or (k[-1][0] if k else p[-1])
    return keys, prefixes


def files_of(sym):
    """[(day, last_modified)] of the contract's daily metrics files."""
    keys, _ = s3_list_all(f"{PREFIX}{sym}/")
    out = []
    for key, lm in keys:
        m = re.search(r"-(\d{4}-\d{2}-\d{2})\.zip$", key or "")
        if m:
            out.append((m.group(1), lm))
    return sorted(out)


def num(s):
    try:
        v = float(s)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def g6(v):
    return "" if v is None else f"{v:.6g}"


def parse(sym, day, raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    txt = z.read(z.namelist()[0]).decode("utf-8", "replace")
    rows = []
    for r in csv.reader(io.StringIO(txt)):
        if len(r) < 4 or not r[0][:1].isdigit():
            continue  # header
        qty, val = num(r[2]), num(r[3])
        if qty is None or val is None:
            continue
        rows.append((r[0], qty, val, num(r[6]) if len(r) > 6 else None, num(r[7]) if len(r) > 7 else None))
    if not rows:
        return None
    rows.sort()
    _, qty_last, oi_last, _, _ = rows[-1]
    mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    ls = mean([x[3] for x in rows if x[3] is not None])
    tk = mean([x[4] for x in rows if x[4] is not None])
    return [day, sym, g6(oi_last), g6(mean([x[2] for x in rows])), g6(qty_last),
            "" if ls is None else f"{ls:.4f}", "" if tk is None else f"{tk:.4f}", str(len(rows))]


def fetch(sym, day):
    st, body = get(HOST, f"/{PREFIX}{sym}/{sym}-metrics-{day}.zip")
    if st != 200:
        return sym, day, None, st
    try:
        return sym, day, parse(sym, day, body), 200
    except Exception as e:  # a corrupt file is skipped and retried next run
        return sym, day, None, f"parse {type(e).__name__}"


def month_path(ym):
    return os.path.join(OUT, f"{ym}.csv.gz")


def read_month(ym):
    p = month_path(ym)
    if not os.path.exists(p):
        return []
    with gzip.open(p, "rt", newline="") as f:
        r = csv.reader(f)
        next(r, None)
        return [row for row in r if row]


def write_month(ym, rows):
    rows = sorted({(r[0], r[1]): r for r in rows}.values())
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FIELDS)
    w.writerows(rows)
    with open(month_path(ym), "wb") as f:  # mtime=0: identical content gives an identical file
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as g:
            g.write(buf.getvalue().encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=280, help="minutes after which no new download starts")
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--since", default="2020-09-01")
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)

    stored = set()
    for fn in os.listdir(OUT):
        if fn.endswith(".csv.gz"):
            stored |= {(r[1], r[0]) for r in read_month(fn[:7])}
    print(f"stored pairs: {len(stored)}", flush=True)

    _, prefixes = s3_list_all(PREFIX, delimiter="/")
    syms = sorted(p[len(PREFIX):].strip("/") for p in prefixes)
    syms = [s for s in syms if s.endswith("USDT")]
    with cf.ThreadPoolExecutor(16) as ex:
        listing = dict(zip(syms, ex.map(files_of, syms)))
    print(f"contracts: {len(syms)}, files listed: {sum(len(v) for v in listing.values())} "
          f"({time.time() - t0:.0f}s)", flush=True)

    todo = [(s, d) for s, files in listing.items() for d, _ in files if d >= a.since and (s, d) not in stored]
    todo.sort(key=lambda x: (x[1], x[0]))  # oldest first: an interrupted run leaves complete early months
    print(f"to fetch: {len(todo)}", flush=True)

    new, fails, done = {}, {}, 0
    deadline = t0 + a.budget * 60
    with cf.ThreadPoolExecutor(a.workers) as ex:
        it = iter(todo)
        pending = set()
        while True:
            while len(pending) < a.workers * 4 and time.time() < deadline:
                nxt = next(it, None)
                if nxt is None:
                    break
                pending.add(ex.submit(fetch, *nxt))
            if not pending:
                break
            finished, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fu in finished:
                sym, day, row, st = fu.result()
                done += 1
                if row:
                    new.setdefault(day[:7], []).append(row)
                else:
                    fails[str(st)] = fails.get(str(st), 0) + 1
                if done % 20000 == 0:
                    print(f"  {done}/{len(todo)} ({time.time() - t0:.0f}s) fails={fails}", flush=True)
    print(f"fetched {done} of {len(todo)} in {time.time() - t0:.0f}s; fails={fails}", flush=True)

    for ym, rows in sorted(new.items()):
        write_month(ym, read_month(ym) + rows)

    meta = {"run_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "contracts": {s: [f[0][0], f[-1][0], len(f)] for s, f in listing.items() if f},
            "fetched": done, "remaining": len(todo) - done, "fails": fails}
    lags = []  # hours from the end of the file's UTC day to its publication
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"):
        for d, lm in listing.get(s, [])[-14:]:
            try:
                pub = dt.datetime.strptime(lm[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
                end = dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
                lags.append(round((pub - end).total_seconds() / 3600, 2))
            except Exception:
                pass
    lags.sort()
    meta["publish_lag_hours"] = {"n": len(lags), "min": lags[0] if lags else None,
                                 "median": lags[len(lags) // 2] if lags else None, "max": lags[-1] if lags else None}
    with open(os.path.join(OUT, "_meta.json"), "w") as f:
        json.dump(meta, f, separators=(",", ":"), sort_keys=True)
    print("meta:", {k: v for k, v in meta.items() if k != "contracts"}, flush=True)


if __name__ == "__main__":
    main()
