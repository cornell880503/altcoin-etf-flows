"""Flow-vs-price statistics shared by the daily summary (Python) and the dashboard (JS).
Keep both implementations identical; scripts/check_stats.py compares them.

Definitions
- r_t      log return of the coin's reference ETF close, previous trading day to t
- rb_t     same for IBIT (BTC)
- x_t      net flow / previous close  (proportional to flow as a share of market cap)
- y_t      'abn': r_t - beta * rb_t, beta = OLS slope of r on rb over the same sample
           (for BTC itself y = r);  'raw': r_t
- corr     Pearson over days where x, r, rb are all present (days whose Canary part is
           still pending are excluded)
- rolling  window of w trading days ending at t; needs >= 2/3 of the days with data and at
           least w/6 days with a non-zero flow; beta re-estimated inside each window
- full     r from 10 days with 5 non-zero flow days; the test (standard error = the larger
           of Newey-West with 4 Bartlett lags and the classical OLS one; normal p),
           interval and BH q need 30 days with 10 non-zero flow days ("small" otherwise)
- leadlag  needs 10 days with 5 non-zero flow days
"""
import math

def _mean(a):
    return sum(a) / len(a)


def pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx, my = _mean(x), _mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def beta(r, rb):
    if len(r) < 3:
        return 0.0
    mr, mb = _mean(r), _mean(rb)
    sbb = sum((b - mb) ** 2 for b in rb)
    if sbb <= 0:
        return 0.0
    return sum((a - mr) * (b - mb) for a, b in zip(r, rb)) / sbb


def triples(x, r, rb, lo, hi):
    """indices in [lo, hi] where x, r, rb are all present"""
    return [i for i in range(max(lo, 0), hi + 1) if x[i] is not None and r[i] is not None and rb[i] is not None]


def ys(r, rb, idx, mode, is_btc):
    if mode == "raw" or is_btc:
        return [r[i] for i in idx], 0.0
    b = beta([r[i] for i in idx], [rb[i] for i in idx])
    return [r[i] - b * rb[i] for i in idx], b


def nonzero(xs):
    return sum(1 for v in xs if abs(v) > 1e-12)


def corr_sample(x, r, rb, lo, hi, mode="abn", is_btc=False):
    """(r, n, n_nonzero_flow)"""
    idx = triples(x, r, rb, lo, hi)
    if len(idx) < 3:
        return None, len(idx), 0
    y, _ = ys(r, rb, idx, mode, is_btc)
    xs = [x[i] for i in idx]
    return pearson(xs, y), len(idx), nonzero(xs)


def rolling(x, r, rb, w, mode="abn", is_btc=False):
    out = []
    need, need_nz = math.ceil(w * 2 / 3), math.ceil(w / 6)
    for t in range(len(x)):
        if t + 1 < w:
            out.append(None)
            continue
        c, n, nz = corr_sample(x, r, rb, t - w + 1, t, mode, is_btc)
        out.append(c if (n >= need and nz >= need_nz) else None)
    return out


def hac_test(xv, yv, lags=4):
    """corr, se, p, lo, hi for y ~ x standardized; Newey-West with small-sample factor n/(n-2)."""
    n = len(xv)
    if n < 10:
        return None
    mx, my = _mean(xv), _mean(yv)
    sx = math.sqrt(sum((a - mx) ** 2 for a in xv) / n)
    sy = math.sqrt(sum((b - my) ** 2 for b in yv) / n)
    if sx <= 0 or sy <= 0:
        return None
    zx = [(a - mx) / sx for a in xv]
    zy = [(b - my) / sy for b in yv]
    b = sum(a * c for a, c in zip(zx, zy)) / n
    u = [zx[i] * (zy[i] - b * zx[i]) for i in range(n)]
    s = sum(v * v for v in u)
    for l in range(1, lags + 1):
        w = 1 - l / (lags + 1)
        s += 2 * w * sum(u[i] * u[i - l] for i in range(l, n))
    var = s / (n * n) * n / (n - 2)
    # never below the classical standard error: with a few extreme flow days the
    # Newey-West estimate can collapse and make a tiny correlation look significant
    se = max(math.sqrt(max(var, 1e-300)), math.sqrt(max(1 - b * b, 0) / (n - 2)))
    z = b / se
    p = math.erfc(abs(z) / math.sqrt(2))
    return {"r": b, "se": se, "p": p, "lo": b - 1.96 * se, "hi": b + 1.96 * se, "n": n}


def full_test(x, r, rb, lo, hi, mode="abn", is_btc=False):
    idx = triples(x, r, rb, lo, hi)
    if len(idx) < 10:
        return None
    xs = [x[i] for i in idx]
    nz = nonzero(xs)
    if nz < 5:
        return None
    y, b = ys(r, rb, idx, mode, is_btc)
    res = hac_test(xs, y)
    if res:
        res.update(beta=b, nz=nz, small=len(idx) < 30 or nz < 10)
    return res


def lead_lag(x, r, rb, lo, hi, kmax=5, mode="abn", is_btc=False):
    """corr(x_t, y_{t+k}) for k=-kmax..kmax over [lo, hi]; beta from the same-day sample."""
    idx = triples(x, r, rb, lo, hi)
    if len(idx) < 10 or nonzero([x[i] for i in idx]) < 5:
        return []
    if mode == "raw" or is_btc:
        b = 0.0
    else:
        b = beta([r[i] for i in idx], [rb[i] for i in idx])
    y = [None if (r[i] is None or rb[i] is None) else r[i] - b * rb[i] for i in range(len(r))]
    out = []
    for k in range(-kmax, kmax + 1):
        xs, yv = [], []
        for t in range(max(lo, 0), hi + 1):
            s = t + k
            if 0 <= s < len(y) and lo <= s <= hi and x[t] is not None and y[s] is not None:
                xs.append(x[t])
                yv.append(y[s])
        c = pearson(xs, yv) if len(xs) >= 10 else None
        out.append({"k": k, "r": c, "n": len(xs)})
    return out


def bh(pvals):
    """Benjamini-Hochberg q-values; None stays None."""
    items = sorted([(p, i) for i, p in enumerate(pvals) if p is not None])
    m = len(items)
    q = [None] * len(pvals)
    prev = 1.0
    for rank in range(m, 0, -1):
        p, i = items[rank - 1]
        prev = min(prev, p * m / rank)
        q[i] = prev
    return q


def quantile(vals, qq):
    v = sorted(vals)
    if not v:
        return None
    pos = (len(v) - 1) * qq
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)
