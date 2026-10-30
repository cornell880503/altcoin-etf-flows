#!/usr/bin/env python3
"""Binance spot order books, kept live beside the hourly loop (scripts/live_loop.py starts and stops it).

Binance publishes no history of its spot order books, and its main hosts refuse US runners (451), but its
market-data hosts answer (research/probe_books_more): data-stream.binance.vision streams every change of
a book (diff depth, every 100 ms) and data-api.binance.vision gives the snapshot to start from. Binance's
procedure: buffer the stream, take a snapshot, drop the buffered changes it already has, apply the rest,
and start over when an update id is skipped.

From the local books, every SAMPLE_S seconds: the depth within 0.25 / 0.5 / 1 / 2 / 3 / 5 / 10 % of the
mid, bids and asks, as quantity (coins) and US$ (CoinKarma calls its LIQ "the ratio of quantities" in the
spot book), the reach (% from the mid both sides cover) and the number of levels. A level beyond the
snapshot (BTC: about 1.5 % from the price) is known only once it changes, so wide bands fill in during a
run (and start over with the next run, every three hours).

Files: data/ck/live/binspot/YYYY-MM-DDTHH.csv.gz, a row per coin with the hour's means (merged with
what an earlier run wrote for the same hour).
"""
import csv
import datetime as dt
import gzip
import io
import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "ck", "live", "binspot")
STREAM = "wss://data-stream.binance.vision/stream?streams="
REST = "https://data-api.binance.vision/api/v3/depth?symbol={s}&limit={n}"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC", "BCH", "SUI", "HYPE", "UNI",
         "AAVE", "NEAR", "GRAM", "ATOM", "APT", "ARB", "OP", "ZEC"]
SYMBOLS = {f"{c}USDT": c for c in COINS} | {"TONUSDT": "GRAM"}  # GRAM may still be TON on Binance spot
DEEP = {"BTC", "ETH"}  # snapshot of 5000 levels (weight 250); the rest 1000 (weight 50)
BANDS = (0.25, 0.5, 1, 2, 3, 5, 10)
VALS = ["reach", "levels_b", "levels_a"] + [f"{s}{k}" for k in BANDS for s in ("bidq", "askq", "bidn", "askn")]
FIELDS = ["hour", "coin", "n"] + VALS
SAMPLE_S = 10
RESYNC_GAP_S = 30  # at most one snapshot a symbol in this many seconds
BUF_MAX = 3000


def bands(bp, bq, ap, aq):
    """Depth within each band of the mid; bp/bq/ap/aq are numpy arrays of the book's prices and sizes."""
    if not len(bp) or not len(ap):
        return None
    bb, ba = bp.max(), ap.min()
    if not (bb > 0 and ba > bb):
        return None
    mid = (bb + ba) / 2
    out = {"reach": min((1 - bp.min() / mid) * 100, (ap.max() / mid - 1) * 100), "levels_b": len(bp), "levels_a": len(ap)}
    for k in BANDS:
        mb, ma = bp >= mid * (1 - k / 100), ap <= mid * (1 + k / 100)
        out[f"bidq{k}"] = float(bq[mb].sum())
        out[f"askq{k}"] = float(aq[ma].sum())
        out[f"bidn{k}"] = float((bp[mb] * bq[mb]).sum())
        out[f"askn{k}"] = float((ap[ma] * aq[ma]).sum())
    return out


class Book:
    def __init__(self, sym, coin):
        self.sym, self.coin = sym, coin
        self.bids, self.asks = {}, {}
        self.last = None
        self.buf = []
        self.state = "wait"  # wait: buffering until a snapshot; live; dead: no such symbol
        self.next_snap = 0.0
        self.resyncs = 0

    def reset(self):
        self.bids, self.asks, self.last, self.buf, self.state = {}, {}, None, [], "wait"

    def apply(self, d):
        """One diff event; returns False on a gap (the book then waits for a new snapshot)."""
        if d["u"] <= self.last:
            return True
        if d["U"] > self.last + 1:
            self.reset()
            self.buf.append(d)
            self.resyncs += 1
            return False
        for side, book in (("b", self.bids), ("a", self.asks)):
            for p, q in d[side]:
                q = float(q)
                if q == 0:
                    book.pop(float(p), None)
                else:
                    book[float(p)] = q
        self.last = d["u"]
        return True

    def load(self, snap):
        """Set the book to a REST snapshot and apply the buffered events after it. False when the snapshot is
        older than the buffer (take another)."""
        lid = snap["lastUpdateId"]
        if self.buf and lid < self.buf[0]["U"] - 1:
            return False
        self.bids = {float(p): float(q) for p, q in snap["bids"]}
        self.asks = {float(p): float(q) for p, q in snap["asks"]}
        self.last = lid
        pending = [e for e in self.buf if e["u"] > lid]
        self.buf = []
        self.state = "live"
        for e in pending:
            if not self.apply(e):
                break
        return True


class Collector:
    def __init__(self, coins=None, out=OUT):
        want = set(coins or COINS)
        self.books = {s: Book(s, c) for s, c in SYMBOLS.items() if c in want}
        self.out = out
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.acc = {}  # (hour, coin) -> {"n": samples, value sums}
        self.cur_hour = None
        self.msgs = 0
        self.errors = []
        self.ws = None
        self.threads = []

    # ------------------------------------------------------------------ stream
    def _on_message(self, _ws, msg):
        try:
            d = json.loads(msg).get("data") or {}
            b = self.books.get(d.get("s"))
            if not b:
                return
            with self.lock:
                self.msgs += 1
                if b.state == "wait":
                    b.buf.append(d)
                    if len(b.buf) > BUF_MAX:
                        del b.buf[: len(b.buf) - BUF_MAX]
                elif b.state == "live":
                    b.apply(d)
        except Exception as e:  # never let one message stop the stream
            self._err(f"message: {type(e).__name__}: {e}")

    def _check_symbols(self):
        """Mark symbols Binance spot does not list (a 400 on the price ticker), before subscribing."""
        for b in self.books.values():
            req = urllib.request.Request(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={b.sym}", headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    b.state = "dead"
            except Exception:
                pass
            time.sleep(0.1)

    def _stream(self):
        import websocket  # websocket-client
        self._check_symbols()
        url = STREAM + "/".join(f"{s.lower()}@depth@100ms" for s, b in self.books.items() if b.state != "dead")
        while not self.stop_ev.is_set():
            with self.lock:
                for b in self.books.values():
                    if b.state != "dead":
                        b.reset()
            try:
                self.ws = websocket.WebSocketApp(url, header={"User-Agent": UA}, on_message=self._on_message,
                                                 on_error=lambda _w, e: self._err(f"stream: {e}"))
                self.ws.run_forever(ping_interval=60, ping_timeout=20)
            except Exception as e:
                self._err(f"stream: {type(e).__name__}: {e}")
            if not self.stop_ev.is_set():
                time.sleep(5)  # reconnect

    # ------------------------------------------------------------------ snapshots
    def _snapshot(self, b):
        n = 5000 if b.coin in DEEP else 1000
        req = urllib.request.Request(REST.format(s=b.sym, n=n), headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 400:  # no such symbol
                return "dead"
            self._err(f"snapshot {b.sym}: HTTP {e.code}")
        except Exception as e:
            self._err(f"snapshot {b.sym}: {type(e).__name__}")
        return None

    def _snapshots(self):
        while not self.stop_ev.is_set():
            now = time.time()
            due = [b for b in self.books.values() if b.state == "wait" and b.buf and now >= b.next_snap]
            for b in due:
                if self.stop_ev.is_set():
                    break
                snap = self._snapshot(b)
                with self.lock:
                    b.next_snap = time.time() + RESYNC_GAP_S
                    if snap == "dead":
                        b.state, b.buf = "dead", []
                    elif snap and b.state == "wait":
                        if not b.load(snap):
                            b.next_snap = time.time() + 2
                time.sleep(1.0)  # spread the snapshots' weight
            self.stop_ev.wait(0.5)

    # ------------------------------------------------------------------ sampling and files
    def _sample(self):
        while not self.stop_ev.is_set():
            t = time.time()
            h = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H")
            if self.cur_hour and h != self.cur_hour:
                self.flush(self.cur_hour)
            self.cur_hour = h
            arrays = []
            with self.lock:
                for b in self.books.values():
                    if b.state == "live" and b.bids and b.asks:
                        arrays.append((b.coin, np.fromiter(b.bids.keys(), float, len(b.bids)), np.fromiter(b.bids.values(), float, len(b.bids)),
                                       np.fromiter(b.asks.keys(), float, len(b.asks)), np.fromiter(b.asks.values(), float, len(b.asks))))
            for coin, bp, bq, ap, aq in arrays:
                s = bands(bp, bq, ap, aq)
                if s:
                    a = self.acc.setdefault((h, coin), {"n": 0})
                    a["n"] += 1
                    for k, v in s.items():
                        a[k] = a.get(k, 0.0) + v
            self.stop_ev.wait(max(0.5, SAMPLE_S - (time.time() - t) % SAMPLE_S))

    def flush(self, hour):
        """Write the hour's means, merged with the file an earlier run wrote for it."""
        rows = {k[1]: v for k, v in list(self.acc.items()) if k[0] == hour}
        for coin in rows:
            self.acc.pop((hour, coin), None)
        if not rows:
            return
        os.makedirs(self.out, exist_ok=True)
        p = os.path.join(self.out, f"{hour}.csv.gz")
        have = {}
        if os.path.exists(p):
            with gzip.open(p, "rt", newline="") as f:
                for r in csv.DictReader(f):
                    have[r["coin"]] = r
        out = {}
        for coin in set(have) | set(rows):
            old, new = have.get(coin), rows.get(coin)
            n_old = int(old["n"]) if old else 0
            n_new = new["n"] if new else 0
            row = {"hour": hour, "coin": coin, "n": n_old + n_new}
            for k in VALS:
                tot = (float(old[k]) * n_old if old and old.get(k) not in (None, "") else 0.0) + (new.get(k, 0.0) if new else 0.0)
                row[k] = f"{tot / max(1, n_old + n_new):.6g}"
            out[coin] = row
        buf = io.StringIO()
        w = csv.DictWriter(buf, FIELDS, lineterminator="\n")
        w.writeheader()
        for coin in sorted(out):
            w.writerow(out[coin])
        with open(p + ".tmp", "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as g:
                g.write(buf.getvalue().encode())
        os.replace(p + ".tmp", p)

    # ------------------------------------------------------------------ control
    def _err(self, text):
        self.errors = (self.errors + [f"{dt.datetime.now(dt.timezone.utc):%H:%M:%S} {text[:160]}"])[-20:]

    def start(self):
        for fn in (self._stream, self._snapshots, self._sample):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self.threads.append(th)
        return self

    def status(self):
        with self.lock:
            return {"live": sorted(b.coin for b in self.books.values() if b.state == "live"),
                    "waiting": sorted(b.sym for b in self.books.values() if b.state == "wait"),
                    "dead": sorted(b.sym for b in self.books.values() if b.state == "dead"),
                    "msgs": self.msgs, "resyncs": sum(b.resyncs for b in self.books.values()),
                    "levels_btc": [len(b.bids) + len(b.asks) for b in self.books.values() if b.coin == "BTC"][:1],
                    "errors": self.errors[-5:]}

    def stop(self):
        self.stop_ev.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            traceback.print_exc()
        for th in self.threads:
            th.join(timeout=15)
        if self.cur_hour:
            self.flush(self.cur_hour)


if __name__ == "__main__":  # a short local run: python scripts/binance_spot_ws.py [seconds]
    import sys
    c = Collector().start()
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    t_end = time.time() + secs
    while time.time() < t_end:
        time.sleep(10)
        print(json.dumps(c.status()), flush=True)
    c.stop()
    print("stopped", json.dumps(c.status()))
