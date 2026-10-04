"""Pre-specified flow-based trading rules, evaluated without look-ahead. Pure Python so the
dashboard's JavaScript can mirror it line by line (scripts/check_stats.py compares them).

Inputs per coin, aligned on the trading calendar (index i = trading day):
  K[i]  net flow of trade date i as known when first published (US$m; Canary parts that
        post a day late are excluded; None when unknown)
  P[i]  reference ETF close;  R[i] = log(P[i]/P[i-1]);  RB[i] = same for IBIT (BTC)
  lag   trading days from trade date to the decision close (1; 2 for Canary-only coins)

Decision at close d = t + lag uses only K[..t] and P[..d]; the position earns R[d+1].
Rules (long or flat):
  FF    K[t] > 0                                  follow yesterday's net inflow
  FZ    z(K[t]) > 1                               strong inflow (z vs the prior 60 known days, >= 20)
  FM5   K[t-4] + ... + K[t] > 0                   5-day flow momentum
  TR20  P[d] / P[d-20] > 1                        price trend only (no flows), the benchmark rule
  TF    TR20 and FM5                              does the flow filter add to the trend?
Costs: COST per side on every change of position.
"""
import math

COST = 0.0010
RULES = ["FF", "FZ", "FM5", "TR20", "TF"]


def zscore(K, t, win=60, minn=20):
    hist = [v for v in K[max(0, t - win):t] if v is not None]
    if K[t] is None or len(hist) < minn:
        return None
    m = sum(hist) / len(hist)
    sd = math.sqrt(sum((v - m) ** 2 for v in hist) / (len(hist) - 1))
    return (K[t] - m) / sd if sd > 0 else None


def sum5(K, t):
    if t < 4:
        return None
    w = K[t - 4:t + 1]
    return None if any(v is None for v in w) else sum(w)


def signals(K, P, lag):
    """dict rule -> list of 1/0/None indexed by decision day d."""
    n = len(P)
    out = {r: [None] * n for r in RULES}
    for d in range(n):
        t = d - lag
        if t >= 0 and K[t] is not None:
            out["FF"][d] = 1 if K[t] > 0 else 0
            z = zscore(K, t)
            out["FZ"][d] = None if z is None else (1 if z > 1 else 0)
            s5 = sum5(K, t)
            out["FM5"][d] = None if s5 is None else (1 if s5 > 0 else 0)
        if d >= 20 and P[d] is not None and P[d - 20] is not None:
            out["TR20"][d] = 1 if P[d] / P[d - 20] > 1 else 0
        if out["TR20"][d] is not None and out["FM5"][d] is not None:
            out["TF"][d] = out["TR20"][d] * out["FM5"][d]
    return out


def backtest(sig, R, cost=COST):
    """Daily net log returns of a long/flat rule (None where not tradable) and the matching
    buy-and-hold series over the same days."""
    n = len(R)
    net, bh, prev, prev_bh = [None] * n, [None] * n, None, None
    for d in range(n - 1):
        s, r = sig[d], R[d + 1]
        if s is None or r is None:
            prev = None if s is None else prev
            continue
        turn = abs(s - (prev if prev is not None else 0))
        net[d + 1] = s * r - turn * cost
        bh[d + 1] = r - (cost if prev_bh is None else 0)
        prev, prev_bh = s, 1
    return net, bh


def perf(rets, ann=252):
    v = [x for x in rets if x is not None]
    if len(v) < 20:
        return None
    m = sum(v) / len(v)
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
    eq, peak, dd, cum = 1.0, 1.0, 0.0, 0.0
    for x in v:
        cum += x
        eq = math.exp(cum)
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    return {"n": len(v), "ann_ret": math.expm1(m * ann), "sharpe": m / sd * math.sqrt(ann) if sd > 0 else None,
            "max_dd": dd, "total": math.expm1(cum)}


def hac_slope(s, y, lags=5):
    """OLS of y on [1, s]: slope, se = max(Newey-West, classical), normal p."""
    n = len(s)
    ms, my = sum(s) / n, sum(y) / n
    sxx = sum((a - ms) ** 2 for a in s)
    if sxx <= 0 or n < 10:
        return None
    b = sum((a - ms) * (c - my) for a, c in zip(s, y)) / sxx
    a0 = my - b * ms
    e = [c - a0 - b * a for a, c in zip(s, y)]
    u = [(a - ms) * ei for a, ei in zip(s, e)]
    S = sum(v * v for v in u)
    for l in range(1, lags + 1):
        w = 1 - l / (lags + 1)
        S += 2 * w * sum(u[i] * u[i - l] for i in range(l, n))
    var_hac = S / (sxx * sxx) * n / (n - 2)
    var_ols = sum(v * v for v in e) / (n - 2) / sxx
    se = math.sqrt(max(var_hac, var_ols, 1e-300))
    z = b / se
    return {"diff": b, "se": se, "t": z, "p": math.erfc(abs(z) / math.sqrt(2)), "n": n}


def timing(sig, Y, min_n=40, min_side=8):
    """Does the signal at d separate the next day's return Y[d+1]? (mean on - mean off)"""
    s, y = [], []
    for d in range(len(sig) - 1):
        if sig[d] is not None and Y[d + 1] is not None:
            s.append(sig[d])
            y.append(Y[d + 1])
    on = sum(s)
    if len(s) < min_n or on < min_side or len(s) - on < min_side:
        return None
    res = hac_slope(s, y)
    if res:
        res.update(n_on=on, mean_on=sum(v for a, v in zip(s, y) if a) / on,
                   mean_off=sum(v for a, v in zip(s, y) if not a) / (len(s) - on))
    return res


def hedged(R, RB, minn=20):
    """R[i] - beta_i * RB[i], beta from days before i only (expanding, >= minn pairs)."""
    out = [None] * len(R)
    sx = sy = sxy = sxx = 0.0
    k = 0
    for i in range(len(R)):
        if R[i] is not None and RB[i] is not None and k >= minn:
            mb, mr = sx / k, sy / k
            var = sxx / k - mb * mb
            beta = (sxy / k - mb * mr) / var if var > 0 else 0.0
            out[i] = R[i] - beta * RB[i]
        if R[i] is not None and RB[i] is not None:
            sx += RB[i]; sy += R[i]; sxy += RB[i] * R[i]; sxx += RB[i] * RB[i]; k += 1
    return out


def basket(Rs):
    """Equal-weight, daily rebalanced log return across the coins with a return that day."""
    n = len(Rs[0])
    out = [None] * n
    for i in range(n):
        v = [R[i] for R in Rs if R[i] is not None]
        if v:
            out[i] = math.log(sum(math.exp(x) for x in v) / len(v))
    return out


def mulberry32(seed):
    """Small seeded RNG with the same bits in Python and JavaScript (dashboard)."""
    a = seed & 0xFFFFFFFF

    def rnd():
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = ((a ^ (a >> 15)) * (1 | a)) & 0xFFFFFFFF
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & 0xFFFFFFFF)) & 0xFFFFFFFF) ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296
    return rnd


def rotation_check(tests, iters=500, seed=20261005):
    """Data-snooping check for the best of many timing tests. Each draw rotates every return
    series against its signals by a random offset (same offset for tests on the same series):
    both series keep their own autocorrelation but lose their alignment. The adjusted p is
    the share of draws whose largest |t| over all tests reaches the observed largest |t|.
    tests: list of dicts with keys group, sig, Y (aligned lists) and t (observed)."""
    rnd = mulberry32(seed)
    ts = [abs(t["t"]) for t in tests if t.get("t") is not None]
    if not ts:
        return None
    obs = max(ts)
    exceed = 0
    for _ in range(iters):
        shifts, best = {}, 0.0
        for t in tests:
            n = len(t["Y"])
            if t["group"] not in shifts:
                shifts[t["group"]] = 20 + int(rnd() * (n - 40))
            k = shifts[t["group"]]
            Y = t["Y"][k:] + t["Y"][:k]
            r = timing(t["sig"], Y)
            if r is not None and abs(r["t"]) > best:
                best = abs(r["t"])
        if best >= obs:
            exceed += 1
    return {"max_abs_t": obs, "p_adjusted": (exceed + 1) / (iters + 1), "iters": iters, "n_tests": len(ts)}
