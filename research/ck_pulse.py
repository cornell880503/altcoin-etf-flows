#!/usr/bin/env python3
"""CoinKarma's own indicator values from their public Telegram trial channel (t.me/coinkarma_market_pulse_trial):
three times a day (00, 08, 16 UTC) it posts a Market Pulse chart and a table image with each indicator's current
value, CoinKarma's thresholds and status (Buy / Hold / Sell), BTC's and ETH's price and the update time. The values
are only in the images, so this reads the channel's web preview (t.me/s/...), downloads the images and reads the
tables with tesseract. Run on a GitHub runner (needs tesseract-ocr; the workspace cannot reach Telegram).

Output (research/ck_pulse/):
  pulse.csv     a row per table: post, posted_at, update_at, btc, eth, and per indicator its value and status
  ocr.jsonl.gz  the OCR lines per post (to audit the reading)
  report.json   counts, the posts skipped and why
Incremental: posts already in pulse.csv (or skipped before) are not read again.

    python research/ck_pulse.py [--limit N] [--workers 4]
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "research", "ck_pulse")
CHANNEL = "coinkarma_market_pulse_trial"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
# row label -> key; matched in this order on the lower-cased label (the table gained rows over time)
KEYS = [("accumulated", "acc_btc_liq"), ("overall liquidity", "overall_liquidity_btc"), ("overall liq", "overall_liq"),
        ("btc liq", "btc_liq"), ("eth liq", "eth_liq"), ("lsur", "lsur_z"), ("resilient", "ari"), ("bitfinex", "bitfinex_premium"),
        ("usdc", "usdc_usdt_premium"), ("interest", "binance_usdt_rate"), ("funding", "oi_funding_apy"), ("pulse", "pulse_index")]
FIELDS = ["post", "posted_at", "update_at", "btc", "eth"] + [f"{k}{s}" for _, k in KEYS for s in ("", "_st")] + ["other"]
NUM = re.compile(r"[-−–]?\d[\d,]*(?:\.\d+)?%?")


def get(url, tries=5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(int(e.headers.get("Retry-After") or 0) or 5 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise


def page(before=None):
    """The posts of one page of the web preview: [(post id, posted at ISO, [image urls])]."""
    url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
    html = get(url).decode("utf-8", "replace")
    out = []
    for blk in html.split('class="tgme_widget_message_wrap')[1:]:
        m = re.search(rf'data-post="{CHANNEL}/(\d+)"', blk)
        if not m:
            continue
        t = re.search(r'<time datetime="([^"]+)"', blk)
        imgs = re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", blk)
        out.append((int(m.group(1)), t.group(1) if t else None, imgs))
    return out


def posts(stop_at=0, max_pages=400):
    """Every post newer than stop_at, newest page first."""
    seen, before = {}, None
    for _ in range(max_pages):
        ps = page(before)
        new = [p for p in ps if p[0] not in seen]
        if not new:
            break
        for p in new:
            seen[p[0]] = p
        lo = min(p[0] for p in ps)
        if lo <= max(1, stop_at + 1):
            break
        before = lo
        time.sleep(1.0)
    return [seen[k] for k in sorted(seen) if k > stop_at]


def mask(im, mode):
    """Black text on white for tesseract: 'white' keeps near-white/grey text (labels, values, status words; drops
    the coloured arrows and dots), 'bright' keeps any bright text (the coloured prices too)."""
    a = np.asarray(im.convert("RGB")).astype(np.int16)
    mn, mx = a.min(axis=2), a.max(axis=2)
    keep = (mn > 130) & (mx - mn < 60) if mode == "white" else (mx > 120)
    out = Image.fromarray(np.where(keep, 0, 255).astype(np.uint8))
    scale = max(1, round(1800 / max(1, im.width)))
    return out.resize((im.width * scale, im.height * scale), Image.LANCZOS) if scale > 1 else out


def tsv(img, psm=6):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        img.save(f.name)
        p = f.name
    r = subprocess.run(["tesseract", p, "stdout", "--psm", str(psm), "-l", "eng", "tsv"], capture_output=True, text=True)
    os.unlink(p)
    lines = {}
    for row in r.stdout.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        key = (int(c[2]), int(c[3]), int(c[4]))
        lines.setdefault(key, []).append((int(c[6]), int(c[7]), int(c[8]), c[11].strip()))
    return [sorted(ws) for _, ws in sorted(lines.items(), key=lambda kv: min(w[1] for w in kv[1]))]


def num(tok):
    t = tok.replace("−", "-").replace("–", "-").replace(",", "")
    t = re.sub(r"(?<=\d)[Oo]|[Oo](?=[\d.])", "0", t)
    pct = t.endswith("%")
    t = t.rstrip("%")
    try:
        return float(t), pct
    except ValueError:
        return None, pct


def read_table(path):
    """{key: (value, status)}, update time, BTC and ETH price, and the OCR lines."""
    im = Image.open(path)
    if im.height < im.width * 1.1:  # the chart image (landscape), not the table
        return None
    lines = tsv(mask(im, "white"))
    text_lines = [" ".join(w[3] for w in ln) for ln in lines]
    vals, other = {}, {}
    # columns from the header row (Indicator / Current Value / Bottom Ref / Top Ref / Status)
    cols = None
    for ln in lines:
        words = {w[3].lower(): w for w in ln}
        if "indicator" in words and "status" in words:
            xs = sorted([words["indicator"][0], words.get("current", words.get("value", (0,)))[0],
                         words.get("bottom", (0,))[0], words.get("top", (0,))[0], words["status"][0]])
            cols = xs
            break
    for ln in lines:
        txt = " ".join(w[3] for w in ln)
        low = txt.lower()
        if low.startswith(("indicator", "last update", "coinkarma")) or "pulse index" == low.strip():
            continue
        # the label: the words before the first number
        label, rest = [], []
        for w in ln:
            if not rest and not NUM.fullmatch(w[3].replace("O", "0")) and not (cols and w[0] >= cols[1] - 10):
                label.append(w[3])
            else:
                rest.append(w)
        lab = " ".join(label).lower()
        key = next((k for kw, k in KEYS if kw in lab), None)
        if not key or not rest:
            if lab and rest:
                other[lab] = " ".join(w[3] for w in rest)
            continue
        value = None
        for w in rest:
            if cols and w[0] > cols[2] - 10:  # past the value column
                break
            v, _ = num(NUM.search(w[3]).group(0)) if NUM.search(w[3]) else (None, False)
            if v is not None:
                value = v
                break
        status = next((s for s in ("Buy", "Hold", "Sell") if s.lower() in low.split()[-2:] or low.endswith(s.lower())), None)
        if key not in vals:
            vals[key] = (value, status)
    # the header: update time and prices (coloured), from the top part with any bright text
    top = im.crop((0, 0, im.width, int(im.height * 0.45)))
    head = " ".join(" ".join(w[3] for w in ln) for ln in tsv(mask(top, "bright"), psm=11))
    upd = re.search(r"(20\d\d-\d\d-\d\d)\s+(\d\d:\d\d)", " ".join(text_lines) + " " + head)
    btc = re.search(r"BTC[:;]?\s*[$S§]?\s*([\d,]+(?:\.\d+)?)", head)
    eth = re.search(r"ETH[:;]?\s*[$S§]?\s*([\d,]+(?:\.\d+)?)", head)
    return {"vals": vals, "other": other, "update_at": f"{upd.group(1)}T{upd.group(2)}" if upd else None,
            "btc": float(btc.group(1).replace(",", "")) if btc else None, "eth": float(eth.group(1).replace(",", "")) if eth else None,
            "lines": text_lines, "head": head}


def work(item):
    pid, posted, url = item
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "i.jpg")
        try:
            with open(p, "wb") as f:
                f.write(get(url))
            r = read_table(p)
        except Exception as e:
            return pid, posted, {"error": f"{type(e).__name__}: {e}"}
    return pid, posted, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the newest N posts (a test)")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    csv_p, ocr_p, rep_p = (os.path.join(OUT, n) for n in ("pulse.csv", "ocr.jsonl.gz", "report.json"))
    have = {}
    if os.path.exists(csv_p):
        have = {int(r["post"]): r for r in csv.DictReader(open(csv_p))}
    rep = json.load(open(rep_p)) if os.path.exists(rep_p) else {}
    skipped = {int(k): v for k, v in rep.get("skipped", {}).items()}
    done = set(have) | set(skipped)
    t0 = time.time()
    ps = posts(stop_at=0 if a.limit else (max(done) if done else 0), max_pages=(a.limit // 20 + 2) if a.limit else 400)
    items = [(pid, posted, imgs[0]) for pid, posted, imgs in ps if imgs and pid not in done]
    if a.limit:
        items = items[-a.limit:]
    print(f"{len(ps)} posts listed, {len(items)} to read ({time.time() - t0:.0f}s)", flush=True)
    audit = []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for pid, posted, r in ex.map(work, items):
            if r is None:
                skipped[pid] = "chart"
                continue
            if "error" in r or not r.get("vals"):
                skipped[pid] = r.get("error", "no table rows") if r else "?"
                audit.append({"post": pid, "error": skipped[pid], "lines": (r or {}).get("lines")})
                continue
            row = {"post": pid, "posted_at": posted, "update_at": r["update_at"], "btc": r["btc"], "eth": r["eth"],
                   "other": json.dumps(r["other"], ensure_ascii=False) if r["other"] else ""}
            for _, k in KEYS:
                v, s = r["vals"].get(k, (None, None))
                row[k], row[k + "_st"] = ("" if v is None else v), (s or "")
            have[pid] = row
            audit.append({"post": pid, "lines": r["lines"], "head": r["head"]})
    with open(csv_p, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        for pid in sorted(have):
            w.writerow(have[pid])
    old = []
    if os.path.exists(ocr_p):
        with gzip.open(ocr_p, "rt") as f:
            old = [json.loads(x) for x in f if x.strip()]
    keep = {x["post"]: x for x in old} | {x["post"]: x for x in audit}
    with gzip.open(ocr_p, "wt") as f:
        for k in sorted(keep):
            f.write(json.dumps(keep[k], ensure_ascii=False) + "\n")
    reasons = {}
    for v in skipped.values():
        reasons[v.split(":")[0]] = reasons.get(v.split(":")[0], 0) + 1
    rep = {"at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tables": len(have), "skipped_by_reason": reasons,
           "first": min(have) if have else None, "last": max(have) if have else None, "secs": round(time.time() - t0),
           "skipped": {str(k): v for k, v in sorted(skipped.items())}}
    with open(rep_p, "w") as f:
        json.dump(rep, f, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "skipped"}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
