#!/usr/bin/env python3
"""Binance spot order books for the LIQ family, kept live by a chain of long jobs (.github/workflows/binspot.yml).

CoinKarma's LIQ is built on Binance spot books in a wide band (research/ck_backtest.py: within 10 % of the
price matches their BTC and Overall LIQ best). Binance publishes no history of them, and its main hosts
refuse US runners (451), but its market-data hosts answer: data-stream.binance.vision streams every change
of a book (diff depth, every 100 ms) and data-api.binance.vision gives snapshots of up to 5000 levels a side.

A snapshot reaches beyond 30 % of the price for most of the coins, but only about 1.2 % for BTC and 5 % for
ETH: their farther levels are known once they change, so their wide bands fill in over days of listening.
So a book is never thrown away:
- every REFRESH_S a new snapshot is merged into the live book (levels the stream has not touched since the
  snapshot are taken from it), so the near range stays exact as the price moves, and the range known
  exactly (exact_b / exact_a) only grows;
- a gap in the stream (a skipped update id, a reconnect) takes a new snapshot for its range and keeps the
  farther levels we had (counted in `gaps`: they may have changed in the meantime);
- each job runs about 5.7 hours and hands its books to the next without a gap: the next job is started
  first and listens to the stream; the old one then saves its books with their last update ids on the
  binspot-state branch and stops, and the new one applies what it heard after those ids (Binance's own
  procedure, with the handed-over book in place of a snapshot). A job that is handed nothing (the first
  one, or after a crash) starts from the last saved books, as the deep part behind fresh snapshots.

Every SAMPLE_S seconds, from each book: bids and asks within BANDS % of the mid, as quantity (coins:
CoinKarma calls LIQ "the ratio of quantities") and as US$; the number of levels; how far the exactly known
range reaches (exact_b / exact_a, % from the mid, 100 = the whole side); how long the book has been
building (age_h) and its gaps. Files: data/ck/live/binspot/YYYY-MM/YYYY-MM-DDTHH.r<run>.csv.gz, a row per
coin with the hour's means over the job's samples (a file per job and hour, so overlapping jobs never write
the same file); read_hours() merges them, weighted by the number of samples.

    python scripts/binance_spot_ws.py --run [--minutes 340]   the job (binspot.yml)
    python scripts/binance_spot_ws.py --listen 60             a short local test: no saved books, no git
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import gzip
import io
import json
import math
import os
import random
import subprocess
import tempfile
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
TICKER = "https://data-api.binance.vision/api/v3/ticker/price?symbol={s}"
UA = "Mozilla/5.0 (compatible; altcoin-etf-flows research; +https://github.com/cornell880503/altcoin-etf-flows)"
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC", "BCH", "SUI", "HYPE", "UNI",
         "AAVE", "NEAR", "GRAM", "ATOM", "APT", "ARB", "OP", "ZEC"]
SYMBOLS = {f"{c}USDT": c for c in COINS}
SNAP_LIMIT = 5000       # levels a side (the most Binance gives)
SNAP_WEIGHT = 250       # its request weight
WEIGHT_BUDGET = 3000    # our snapshots' weight in any minute (Binance allows 6000 a minute per IP)
REFRESH_S = 30 * 60     # a fresh snapshot merged into each live book this often
BANDS = (0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30)
KEEP_PCT = 50           # levels farther than this from the mid are dropped (memory, the saved books)
VALS = ["levels_b", "levels_a", "exact_b", "exact_a", "age_h", "gaps"] + [f"{s}{k}" for k in BANDS for s in ("bidq", "askq", "bidn", "askn")]
FIELDS = ["hour", "coin", "n"] + VALS
SAMPLE_S = 10
RESYNC_GAP_S = 30       # at most one gap snapshot a symbol in this many seconds
BUF_MAX = 20000         # buffered events a symbol (BTC sends at most 10 a second: half an hour)
STATE_BRANCH = "binspot-state"
WORKFLOW = "binspot.yml"
STEP = "Collect"        # the workflow step running this script: the old job waits until the new one's is running
LISTEN_S = 75           # ... this long after it started (by then it listens to the stream)
CKPT_S = 3 * 3600       # books saved this often (after a crash the next job starts from them)
SEED_MAX_S = 48 * 3600  # older saved books are not used
WAIT_S = 20 * 60        # how long a new job waits for the old one's books
POLL_S = 15             # ... looking this often


def hour_of(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H")


def bands(bp, bq, ap, aq):
    """Depth within each band of the mid; bp/bq/ap/aq are numpy arrays of the book's prices and sizes."""
    if not len(bp) or not len(ap):
        return None
    bb, ba = bp.max(), ap.min()
    if not (bb > 0 and ba > bb):
        return None
    mid = (bb + ba) / 2
    out = {"mid": mid, "levels_b": len(bp), "levels_a": len(ap)}
    for k in BANDS:
        mb, ma = bp >= mid * (1 - k / 100), ap <= mid * (1 + k / 100)
        out[f"bidq{k}"] = float(bq[mb].sum())
        out[f"askq{k}"] = float(aq[ma].sum())
        out[f"bidn{k}"] = float((bp[mb] * bq[mb]).sum())
        out[f"askn{k}"] = float((ap[ma] * aq[ma]).sum())
    return out


def snap_side(snap, limit):
    """A snapshot's levels and the range they cover exactly: bids >= lo, asks <= hi (a side shorter than the limit
    is the whole side)."""
    bids = {float(p): float(q) for p, q in snap["bids"]}
    asks = {float(p): float(q) for p, q in snap["asks"]}
    lo = min(bids) if len(snap["bids"]) >= limit else 0.0
    hi = max(asks) if len(snap["asks"]) >= limit else math.inf
    return bids, asks, lo, hi


class Book:
    def __init__(self, sym, coin):
        self.sym, self.coin = sym, coin
        self.bids, self.asks = {}, {}
        self.last = None        # the last update id applied
        self.buf = []           # events heard while waiting for a snapshot or the old job's book
        self.state = "wait"     # wait | live | dead (no such symbol)
        self.lo, self.hi = 0.0, math.inf  # the range known exactly: bids >= lo, asks <= hi
        self.born = None        # when this book (or the one it continues) started
        self.gaps = 0           # times farther levels were carried over a gap (they may be stale)
        self.resyncs = 0
        self.next_snap = 0.0
        self.next_refresh = 0.0
        self.track = None       # during a refresh: {(side, price): update id of its last change}

    def gap(self):
        """Missed updates (or a new connection): wait for a snapshot; the levels stay as the deep book."""
        if self.state == "live":
            self.resyncs += 1
        if self.state != "dead":
            self.state = "wait"
        self.buf, self.track = [], None

    def apply(self, d):
        """One diff event; False on a gap."""
        if d["u"] <= self.last:
            return True
        if d["U"] > self.last + 1:
            self.gap()
            self.buf.append(d)
            return False
        tr = self.track
        for side, book in (("b", self.bids), ("a", self.asks)):
            for p, q in d[side]:
                p, q = float(p), float(q)
                if q == 0:
                    book.pop(p, None)
                else:
                    book[p] = q
                if tr is not None:
                    tr[(side, p)] = d["u"]
        self.last = d["u"]
        return True

    def _go_live(self, last):
        self.last = last
        pending = [e for e in self.buf if e["u"] > last]
        self.buf = []
        self.state = "live"
        for e in pending:
            if not self.apply(e):
                break

    def load_snapshot(self, snap, limit=SNAP_LIMIT):
        """Start (again) from a snapshot: exact within its range; the farther levels we had stay. False when the
        snapshot is older than what was buffered (take another)."""
        lid = snap["lastUpdateId"]
        if self.buf and lid < self.buf[0]["U"] - 1:
            return False
        bids, asks, lo, hi = snap_side(snap, limit)
        carried = 0
        for p, q in self.bids.items():
            if p < lo:
                bids[p] = q
                carried += 1
        for p, q in self.asks.items():
            if p > hi:
                asks[p] = q
                carried += 1
        if carried:
            self.gaps += 1
        self.bids, self.asks, self.lo, self.hi = bids, asks, lo, hi
        if self.born is None:
            self.born = time.time()
        self._go_live(lid)
        return True

    def refresh(self, snap, limit=SNAP_LIMIT):
        """Merge a snapshot into the live book, which has applied every event up to its id: in the snapshot's range,
        a level the stream has not changed since is set (or removed) as the snapshot has it."""
        lid = snap["lastUpdateId"]
        tr, self.track = self.track or {}, None
        bids, asks, lo, hi = snap_side(snap, limit)
        for side, book, new, inside in (("b", self.bids, bids, lambda p: p >= lo), ("a", self.asks, asks, lambda p: p <= hi)):
            for p in [p for p in book if inside(p) and p not in new and tr.get((side, p), -1) <= lid]:
                del book[p]
            for p, q in new.items():
                if tr.get((side, p), -1) <= lid:
                    book[p] = q
        self.lo, self.hi = min(self.lo, lo), max(self.hi, hi)

    def load_state(self, s):
        """Continue from the old job's book: exact when this job's buffer reaches back to its last update id."""
        if s.get("last") is None or (self.buf and self.buf[0]["U"] > s["last"] + 1):
            return False
        self._set(s)
        self._go_live(s["last"])
        return True

    def seed(self, s):
        """A saved book as the deep part behind the next snapshot (the book keeps waiting)."""
        self._set(s)
        self.lo, self.hi = 0.0, math.inf  # replaced by the snapshot's range

    def _set(self, s):
        self.bids = {float(p): float(q) for p, q in s["b"]}
        self.asks = {float(p): float(q) for p, q in s["a"]}
        self.lo = float(s.get("lo") or 0.0)
        self.hi = math.inf if s.get("hi") is None else float(s["hi"])
        self.born, self.gaps = s.get("born"), int(s.get("gaps") or 0)

    def saved(self):
        return {"last": self.last if self.state == "live" else None, "lo": self.lo, "hi": None if math.isinf(self.hi) else self.hi,
                "born": self.born, "gaps": self.gaps, "b": list(self.bids.items()), "a": list(self.asks.items())}

    def prune(self):
        if self.state != "live" or not self.bids or not self.asks:
            return
        mid = (max(self.bids) + min(self.asks)) / 2
        lo, hi = mid * (1 - KEEP_PCT / 100), mid * (1 + KEEP_PCT / 100)
        self.bids = {p: q for p, q in self.bids.items() if p >= lo}
        self.asks = {p: q for p, q in self.asks.items() if p <= hi}
        self.lo, self.hi = max(self.lo, lo), min(self.hi, hi)


class Collector:
    def __init__(self, coins=None, out=OUT, run="local"):
        want = set(coins or COINS)
        self.books = {s: Book(s, c) for s, c in SYMBOLS.items() if c in want}
        self.out, self.run = out, str(run)
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.hold = False       # no snapshots while waiting for the old job's books
        self.acc = {}           # (hour, coin) -> {"n": samples, value sums}
        self.cur_hour = None
        self.msgs = self.snaps = self.refreshes = 0
        self.flushed = 0        # files written (the job pushes when this changes)
        self.errors = []
        self.ws = None
        self.connected = None   # when the current connection opened
        self.used = collections.deque()  # (time, weight) of our recent snapshots
        self.threads = []
        self.takeover = None    # how the job got its books (run())

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
            req = urllib.request.Request(TICKER.format(s=b.sym), headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    b.state = "dead"
            except Exception:
                pass

    def _opened(self, _ws):
        self.connected = time.time()

    def _stream(self):
        import websocket  # websocket-client
        self._check_symbols()
        url = STREAM + "/".join(f"{s.lower()}@depth@100ms" for s, b in self.books.items() if b.state != "dead")
        while not self.stop_ev.is_set():
            with self.lock:
                for b in self.books.values():
                    b.gap()
            try:
                self.ws = websocket.WebSocketApp(url, header={"User-Agent": UA}, on_open=self._opened, on_message=self._on_message,
                                                 on_error=lambda _w, e: self._err(f"stream: {e}"))
                self.ws.run_forever(ping_interval=60, ping_timeout=20)
            except Exception as e:
                self._err(f"stream: {type(e).__name__}: {e}")
            self.connected = None
            if not self.stop_ev.is_set():
                time.sleep(5)  # reconnect

    # ------------------------------------------------------------------ snapshots
    def _weight(self, w):
        """Wait until w more weight fits our budget for the last minute."""
        while not self.stop_ev.is_set():
            now = time.time()
            while self.used and self.used[0][0] < now - 60:
                self.used.popleft()
            if sum(x for _, x in self.used) + w <= WEIGHT_BUDGET:
                self.used.append((now, w))
                return True
            self.stop_ev.wait(1.0)
        return False

    def _snapshot(self, b):
        req = urllib.request.Request(REST.format(s=b.sym, n=SNAP_LIMIT), headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                used = int(r.headers.get("x-mbx-used-weight-1m") or 0)
                body = json.loads(r.read())
            self.snaps += 1
            if used > 4800:  # others on this IP are busy too: let the minute pass
                self.stop_ev.wait(61 - time.time() % 60)
            return body
        except urllib.error.HTTPError as e:
            if e.code == 400:  # no such symbol
                return "dead"
            self._err(f"snapshot {b.sym}: HTTP {e.code}")
            if e.code in (418, 429):  # over the limit: wait as told
                self.stop_ev.wait(min(600, int(e.headers.get("Retry-After") or 60)))
        except Exception as e:
            self._err(f"snapshot {b.sym}: {type(e).__name__}")
        return None

    def _snapshots(self):
        while not self.stop_ev.is_set():
            if not self.hold:
                now = time.time()
                for b in list(self.books.values()):
                    if self.stop_ev.is_set() or self.hold:
                        break
                    if b.state == "wait" and b.buf and now >= b.next_snap:
                        self._gap_snapshot(b)
                    elif b.state == "live" and now >= b.next_refresh:
                        self._refresh(b)
            self.stop_ev.wait(0.5)

    def _gap_snapshot(self, b):
        if not self._weight(SNAP_WEIGHT):
            return
        snap = self._snapshot(b)
        with self.lock:
            b.next_snap = time.time() + RESYNC_GAP_S
            if snap == "dead":
                b.state, b.buf = "dead", []
            elif snap and b.state == "wait":
                if b.load_snapshot(snap):
                    b.next_refresh = time.time() + REFRESH_S * random.uniform(0.5, 1)  # spread the refreshes
                else:
                    b.next_snap = time.time() + 2

    def _refresh(self, b):
        if not self._weight(SNAP_WEIGHT):
            return
        with self.lock:
            b.track = {}  # from here on, the stream's changes win over the snapshot's
        snap = self._snapshot(b)
        b.next_refresh = time.time() + REFRESH_S
        if not isinstance(snap, dict):
            with self.lock:
                b.track = None
            return
        lid = snap["lastUpdateId"]
        t_end = time.time() + 10
        while time.time() < t_end:  # the stream is usually a fraction of a second behind the snapshot
            with self.lock:
                if b.state != "live" or b.track is None:  # a gap meanwhile: that path takes its own snapshot
                    return
                if b.last >= lid:
                    b.refresh(snap)
                    self.refreshes += 1
                    return
            time.sleep(0.1)
        with self.lock:
            b.track = None

    # ------------------------------------------------------------------ sampling and files
    def _sample(self):
        while not self.stop_ev.is_set():
            t = time.time()
            h = hour_of(t)
            if self.cur_hour and h != self.cur_hour:
                self.flush(self.cur_hour)
            self.cur_hour = h
            arrays = []
            with self.lock:
                for b in self.books.values():
                    if b.state == "live" and b.bids and b.asks:
                        arrays.append((b.coin, b.lo, b.hi, b.born, b.gaps,
                                       np.fromiter(b.bids.keys(), float, len(b.bids)), np.fromiter(b.bids.values(), float, len(b.bids)),
                                       np.fromiter(b.asks.keys(), float, len(b.asks)), np.fromiter(b.asks.values(), float, len(b.asks))))
            for coin, lo, hi, born, gaps, bp, bq, ap, aq in arrays:
                s = bands(bp, bq, ap, aq)
                if not s:
                    continue
                mid = s.pop("mid")
                s["exact_b"] = max(-100.0, min(100.0, (1 - lo / mid) * 100)) if lo > 0 else 100.0
                s["exact_a"] = max(-100.0, min(100.0, (hi / mid - 1) * 100)) if math.isfinite(hi) else 100.0
                s["age_h"] = (t - born) / 3600 if born else 0.0
                s["gaps"] = gaps
                a = self.acc.setdefault((h, coin), {"n": 0})
                a["n"] += 1
                for k, v in s.items():
                    a[k] = a.get(k, 0.0) + v
            self.stop_ev.wait(max(0.5, SAMPLE_S - (time.time() - t) % SAMPLE_S))

    def path(self, hour):
        return os.path.join(self.out, hour[:7], f"{hour}.r{self.run}.csv.gz")

    def flush(self, hour):
        """Write this job's means for the hour (merged with what it wrote for the hour before, if anything)."""
        rows = {k[1]: v for k, v in list(self.acc.items()) if k[0] == hour}
        for coin in rows:
            self.acc.pop((hour, coin), None)
        if not rows:
            return
        p = self.path(hour)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        have = {}
        if os.path.exists(p):
            with gzip.open(p, "rt", newline="") as f:
                have = {r["coin"]: r for r in csv.DictReader(f)}
        for coin, new in rows.items():
            old = have.get(coin)
            n_old = int(old["n"]) if old else 0
            n = n_old + new["n"]
            row = {"hour": hour, "coin": coin, "n": n}
            for k in VALS:
                tot = (float(old[k]) * n_old if old and old.get(k) not in (None, "") else 0.0) + new.get(k, 0.0)
                row[k] = f"{tot / max(1, n):.6g}"
            have[coin] = row
        buf = io.StringIO()
        w = csv.DictWriter(buf, FIELDS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for coin in sorted(have):
            w.writerow(have[coin])
        with open(p + ".tmp", "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as g:
                g.write(buf.getvalue().encode())
        os.replace(p + ".tmp", p)
        self.flushed += 1

    # ------------------------------------------------------------------ control
    def _err(self, text):
        self.errors = (self.errors + [f"{dt.datetime.now(dt.timezone.utc):%H:%M:%S} {text[:160]}"])[-20:]

    def start(self):
        for fn in (self._stream, self._snapshots, self._sample):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self.threads.append(th)
        return self

    def saved(self):
        """Every book as it stands (copied under the lock), for the next job."""
        with self.lock:
            for b in self.books.values():
                b.prune()
            return {s: b.saved() for s, b in self.books.items() if b.state != "dead" and (b.bids or b.asks)}

    def prune(self):
        with self.lock:
            for b in self.books.values():
                b.prune()

    def status(self):
        with self.lock:
            live = [b for b in self.books.values() if b.state == "live" and b.bids and b.asks]
            exact = {}
            for b in live:
                mid = (max(b.bids) + min(b.asks)) / 2
                exact[b.coin] = [round((1 - b.lo / mid) * 100, 1) if b.lo > 0 else 100, round((b.hi / mid - 1) * 100, 1) if math.isfinite(b.hi) else 100]
            return {"live": len(live), "waiting": sorted(b.sym for b in self.books.values() if b.state == "wait"),
                    "dead": sorted(b.sym for b in self.books.values() if b.state == "dead"),
                    "msgs": self.msgs, "snaps": self.snaps, "refreshes": self.refreshes,
                    "resyncs": sum(b.resyncs for b in self.books.values()), "gaps": {b.coin: b.gaps for b in live if b.gaps},
                    "levels": {b.coin: len(b.bids) + len(b.asks) for b in live if b.coin in ("BTC", "ETH")},
                    "exact": {c: exact[c] for c in ("BTC", "ETH") if c in exact},
                    "age_h": round(max([(time.time() - b.born) / 3600 for b in live if b.born] or [0]), 1),
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


# ---------------------------------------------------------------------- reading the files
def read_hours(path=OUT):
    """{hour: {coin: {column: mean, "n": samples}}} over every file (the jobs' hourly ones and the first day's), the
    means weighted by samples."""
    acc = {}
    for p in sorted(glob.glob(os.path.join(path, "**", "*.csv.gz"), recursive=True)):
        try:
            with gzip.open(p, "rt", newline="") as f:
                rows = list(csv.DictReader(f))
        except (OSError, EOFError, csv.Error):
            continue
        for r in rows:
            n = int(float(r.get("n") or 0))
            if n <= 0:
                continue
            cell = acc.setdefault(r["hour"], {}).setdefault(r["coin"], [0, {}])
            cell[0] += n
            for k, v in r.items():
                if k in ("hour", "coin", "n") or v in (None, ""):
                    continue
                try:
                    x = float(v)
                except ValueError:
                    continue
                s = cell[1].setdefault(k, [0.0, 0])
                s[0] += x * n
                s[1] += n
    return {h: {c: {k: s[0] / s[1] for k, s in cell[1].items()} | {"n": cell[0]} for c, cell in coins.items()}
            for h, coins in acc.items()}


# ---------------------------------------------------------------------- the job: GitHub, the saved books
def gh(path, body=None, raw=False):
    token, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/{path}", headers=hdr,
                                 data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if raw:
        return data
    return json.loads(data) if data else {}


def ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp() if s else None


def running():
    """Ids of the collector's runs in progress."""
    r = gh(f"actions/workflows/{WORKFLOW}/runs?status=in_progress&per_page=20") or {}
    return [x["id"] for x in r.get("workflow_runs", [])]


def run_done(run_id):
    return (gh(f"actions/runs/{run_id}") or {}).get("status") == "completed"


def step_started(run_id):
    """When the run's Collect step started, while it runs (None otherwise)."""
    for j in (gh(f"actions/runs/{run_id}/jobs") or {}).get("jobs", []):
        for s in j.get("steps") or []:
            if s.get("name") == STEP and s.get("status") == "in_progress":
                return ts(s.get("started_at"))
    return None


def state_head():
    """(commit sha, meta) of the saved books, or (None, None)."""
    try:
        c = gh(f"commits/{STATE_BRANCH}")
    except urllib.error.HTTPError as e:
        if e.code in (404, 409, 422):
            return None, None
        raise
    if not c:
        return None, None
    try:
        meta = json.loads(c["commit"]["message"])
    except (ValueError, KeyError):
        meta = {}
    return c["sha"], meta


def state_fetch(sha):
    return json.loads(gzip.decompress(gh(f"contents/state.json.gz?ref={sha}", raw=True)))


def state_push(books, meta):
    token, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return False
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "state.json.gz"), "wb") as f:
        f.write(gzip.compress(json.dumps({"v": 1, "books": books}, separators=(",", ":")).encode(), 6))
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    for c in (["git", "init", "-q", "-b", STATE_BRANCH], ["git", "config", "user.name", "etf-flows-bot"],
              ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], ["git", "add", "-A"],
              ["git", "commit", "-q", "-m", json.dumps(meta, separators=(",", ":"))]):
        subprocess.run(c, cwd=d, capture_output=True)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q", "-f", url, f"HEAD:refs/heads/{STATE_BRANCH}"], cwd=d, capture_output=True).returncode == 0:
            subprocess.run(["rm", "-rf", d])
            return True
        time.sleep(5)
    subprocess.run(["rm", "-rf", d])
    return False


def save(col, final, to=None):
    t = time.time()
    books = col.saved()
    meta = {"run": col.run, "saved_at": round(t, 1), "at": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "final": final, "to": to, "books": len(books), "takeover": col.takeover, "status": col.status()}
    ok = state_push(books, meta)
    print(f"books saved ({'final' if final else 'checkpoint'}, {len(books)} books, {time.time() - t:.0f}s): {ok}", flush=True)
    return ok


def push_data(msg):
    def sh(cmd):
        return subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)
    sh("git add -A data/ck/live/binspot")
    if sh("git diff --cached --quiet").returncode == 0:
        return True
    sh(f'git commit -q -m "{msg}"')
    for _ in range(5):
        if sh("git pull -q --rebase --autostash origin main").returncode == 0 and sh("git push -q origin HEAD:main").returncode == 0:
            return True
        sh("git rebase --abort")
        time.sleep(7)
    print("::warning::could not push", msg, flush=True)
    return False


def take_over(col, me, t_listen):
    """Load the books: the old job's, continued without a gap if it hands them over after this job started
    listening; otherwise the last saved ones as the deep part behind snapshots."""
    rep, st = {"mode": "fresh"}, None
    older = [r for r in running() if r < me] if me else []
    if older:
        rep["from"] = older
        t_end, next_check = time.time() + WAIT_S, 0.0
        while time.time() < t_end:
            sha, meta = state_head()
            if meta and meta.get("final") and str(meta.get("run")) in {str(r) for r in older} and meta.get("saved_at", 0) > t_listen:
                st, rep["mode"] = state_fetch(sha), "continue"
                break
            if time.time() >= next_check:
                next_check = time.time() + 60
                older = [r for r in older if not run_done(r)]
                if not older:  # it ended without handing over
                    break
            time.sleep(POLL_S)
    if st is None:
        sha, meta = state_head()
        if meta and time.time() - meta.get("saved_at", 0) < SEED_MAX_S:
            st, rep["mode"] = state_fetch(sha), "seed"
            rep["saved_min_ago"] = round((time.time() - meta["saved_at"]) / 60)
    if st:
        n_cont = n_seed = 0
        with col.lock:
            for b in col.books.values():
                s = st["books"].get(b.sym)
                if not s or b.state == "dead":
                    continue
                if rep["mode"] == "continue" and b.load_state(s):
                    b.next_refresh = time.time() + random.uniform(60, REFRESH_S)
                    n_cont += 1
                else:
                    b.seed(s)
                    n_seed += 1
        rep.update(continued=n_cont, seeded=n_seed)
    return rep


def run(minutes=340, hard=352):
    """The job: take the books over, sample and push every hour, save the books every CKPT_S, start the next job
    after `minutes` and hand the books to it once it listens (or save them and stop at `hard`)."""
    t0 = time.time()
    me = int(os.environ.get("GITHUB_RUN_ID") or 0)
    os.chdir(ROOT)
    subprocess.run('git config user.name "etf-flows-bot" && git config user.email "41898282+github-actions[bot]@users.noreply.github.com"',
                   shell=True, cwd=ROOT)
    col = Collector(run=me or "local")
    col.hold = True
    col.start()
    while col.connected is None and time.time() < t0 + 90:
        time.sleep(0.5)
    t_listen = col.connected or time.time()
    try:
        rep = take_over(col, me, t_listen)
    except Exception as e:
        traceback.print_exc()
        rep = {"mode": "fresh", "error": f"{type(e).__name__}: {e}"}
    col.hold = False
    col.takeover = rep | {"at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    print("take-over:", json.dumps(rep), flush=True)
    # the first save soon after the take-over (the books are complete again by then), then every CKPT_S
    pushed, next_ckpt, next_prune, next_check, dispatched = col.flushed, time.time() + 600, time.time() + 3600, time.time() + 120, 0.0
    successor = None
    try:
        while True:
            time.sleep(15)
            now = time.time()
            if col.flushed != pushed:
                pushed = col.flushed
                push_data(f"binspot: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}")
                print("status:", json.dumps(col.status()), flush=True)
            if now >= next_prune:
                col.prune()
                next_prune = now + 3600
            if now >= next_ckpt:
                save(col, final=False)
                next_ckpt = now + CKPT_S
            if me and now >= next_check:
                next_check = now + (30 if dispatched else 120)
                try:
                    for r in sorted((r for r in running() if r > me), reverse=True):
                        t_step = step_started(r)
                        if t_step and now >= t_step + LISTEN_S:
                            successor = r
                            break
                except Exception:
                    traceback.print_exc()
                if successor:
                    break
            if me and now >= t0 + minutes * 60 and now - dispatched > 600:
                try:
                    gh(f"actions/workflows/{WORKFLOW}/dispatches", {"ref": "main"})
                    print("dispatched the next job", flush=True)
                except Exception:
                    traceback.print_exc()
                dispatched = now
            if now >= t0 + hard * 60:
                break
    except Exception:
        traceback.print_exc()
    save(col, final=True, to=successor)
    col.stop()
    push_data(f"binspot: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ} (end)")
    print(f"ended after {(time.time() - t0) / 60:.0f} minutes; handed to {successor}; status {json.dumps(col.status())}", flush=True)


def listen(secs):
    c = Collector(out=os.path.join(tempfile.mkdtemp(), "binspot")).start()
    t_end = time.time() + secs
    while time.time() < t_end:
        time.sleep(10)
        print(json.dumps(c.status()), flush=True)
    c.stop()
    print("stopped", json.dumps(c.status()), "files in", c.out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="the job (binspot.yml)")
    ap.add_argument("--minutes", type=float, default=340, help="start the next job after this long")
    ap.add_argument("--hard", type=float, default=352, help="stop after this long, handed over or not")
    ap.add_argument("--listen", type=float, default=60, help="without --run: listen this many seconds, locally")
    a = ap.parse_args()
    if a.run:
        run(a.minutes, a.hard)
    else:
        listen(a.listen)
