"""Storage for data/oi/<source>/ (Binance open interest, Coin Metrics market caps).

One gzip CSV per finished month (<YYYY-MM>.csv.gz) and, for the running month, one file per day
(daily/<YYYY-MM-DD>.csv.gz): a daily update adds a small file instead of rewriting the month, which
keeps the git history small. When a month is over its days are folded into its month file. Rows
are keyed by (date, id) = their first two columns; a later write of the same key replaces it, and
a day file overrides the month file when both hold a key.
"""
import csv
import datetime as dt
import glob
import gzip
import io
import os


def _read(path):
    with gzip.open(path, "rt", newline="") as f:
        r = csv.reader(f)
        next(r, None)
        return [row for row in r if row]


def _write(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted({(r[0], r[1]): r for r in rows}.values())
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:  # mtime=0: the same rows give the same bytes (no spurious git diffs)
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9) as g:
            g.write(buf.getvalue().encode())
    os.replace(tmp, path)


def _month_files(folder):
    return sorted(glob.glob(os.path.join(folder, "????-??.csv.gz")))


def _day_files(folder):
    return sorted(glob.glob(os.path.join(folder, "daily", "????-??-??.csv.gz")))


def read_all(folder):
    """All rows, one per key (day files win over month files)."""
    rows = {}
    for p in _month_files(folder) + _day_files(folder):
        for r in _read(p):
            rows[(r[0], r[1])] = r
    return list(rows.values())


def keys(folder):
    return {(r[0], r[1]) for r in read_all(folder)}


def this_month(today=None):
    return (today or dt.datetime.now(dt.timezone.utc).date()).isoformat()[:7]


def fold(folder, header, today=None):
    """Fold the day files of finished months into their month files."""
    cur = this_month(today)
    by_month = {}
    for p in _day_files(folder):
        ym = os.path.basename(p)[:7]
        if ym < cur:
            by_month.setdefault(ym, []).append(p)
    for ym, paths in by_month.items():
        mp = os.path.join(folder, f"{ym}.csv.gz")
        rows = _read(mp) if os.path.exists(mp) else []
        for p in paths:
            rows += _read(p)
        _write(mp, header, rows)
        for p in paths:
            os.remove(p)
    return sorted(by_month)


def write_rows(folder, header, rows, today=None):
    """Merge rows into the right files: finished months into month files, the running month into
    day files. Returns the number of files written."""
    cur = this_month(today)
    months, days = {}, {}
    for r in rows:
        (months if r[0][:7] < cur else days).setdefault(r[0][:7] if r[0][:7] < cur else r[0], []).append(r)
    n = 0
    for ym, rr in sorted(months.items()):
        mp = os.path.join(folder, f"{ym}.csv.gz")
        _write(mp, header, (_read(mp) if os.path.exists(mp) else []) + rr)
        n += 1
    for d, rr in sorted(days.items()):
        dp = os.path.join(folder, "daily", f"{d}.csv.gz")
        _write(dp, header, (_read(dp) if os.path.exists(dp) else []) + rr)
        n += 1
    return n
