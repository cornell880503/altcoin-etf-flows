#!/usr/bin/env python3
"""Does measuring altcoin leverage on Binance alone distort the radar? Compares Binance with the other
venues that have free history (data/oi/<venue>/), on the inputs and on the ALR itself.

1. Inputs: correlation of 14-day growth (log) between venues, for altcoin OI, major (BTC+ETH) OI and the
   two relative measures the ALR ranks (alt OI vs major OI, alt OI vs alt market cap).
2. ALR: Binance only vs all venues (chain-linked; each venue enters on its first day): correlation,
   alert days, and the 30-day backtest of each.
Writes research/alt_radar/venues.json. Usage: python3 research/venues_compare.py [venue ...]
"""
import datetime as dt
import json
import math
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import alt_radar as R  # noqa: E402


def corr(xs, ys):
    n = len(xs)
    if n < 30:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 3) if sx and sy else None


def growth(idx, L=14):
    return {d: math.log(v / idx[p]) for d, v in idx.items()
            for p in [R.d2s(R.s2d(d) - dt.timedelta(days=L))] if p in idx and idx[p] > 0 and v > 0}


def venue_series(oi, ccls, venue, dates):
    alt = {s: {d: v[0] for d, v in oi[s].items()} for s, c in ccls.items() if c == "alt" and R.venue_of(s) == venue}
    maj = {s: {d: v[0] for d, v in oi[s].items()} for s, c in ccls.items() if c == "major" and R.venue_of(s) == venue}
    first = max(min(min(v) for v in maj.values()), min(min(v) for v in alt.values()))
    ds = [d for d in dates if d >= first]
    a_idx, a_sum = R.chain(alt, ds, 5.0)
    m_idx, m_sum = R.chain(maj, ds, 5.0)
    return a_idx, m_idx, a_sum, m_sum


def main():
    others = sys.argv[1:] or [v for v in ("hyperliquid", "okx", "gate") if os.path.isdir(os.path.join(R.DATA, v))]
    venues = ["binance"] + others
    oi, cm = R.load_oi(venues), R.load_cm()
    S_all = R.build(oi, cm)
    ccls = S_all["contracts"]
    rep = {"venues": venues, "inputs": {}, "share": {}}
    dates = S_all["oi_dates"]
    ser = {v: venue_series(oi, ccls, v, dates) for v in venues}
    alt_g = {v: growth(ser[v][0]) for v in venues}
    maj_g = {v: growth(ser[v][1]) for v in venues}
    mc_g = growth(S_all["alt_idx"])
    for v in others:
        common = sorted(set(alt_g["binance"]) & set(alt_g[v]))
        f1b = [alt_g["binance"][d] - maj_g["binance"][d] for d in common if d in maj_g["binance"] and d in maj_g[v]]
        f1o = [alt_g[v][d] - maj_g[v][d] for d in common if d in maj_g["binance"] and d in maj_g[v]]
        cm_common = [d for d in common if d in mc_g]
        rep["inputs"][v] = {
            "from": common[0] if common else None, "days": len(common),
            "alt_oi_14d": corr([alt_g["binance"][d] for d in common], [alt_g[v][d] for d in common]),
            "major_oi_14d": corr([maj_g["binance"][d] for d in common if d in maj_g[v]],
                                 [maj_g[v][d] for d in common if d in maj_g[v]]),
            "alt_vs_major_14d": corr(f1b, f1o),
            "alt_oi_vs_mcap_14d": corr([alt_g["binance"][d] - mc_g[d] for d in cm_common],
                                       [alt_g[v][d] - mc_g[d] for d in cm_common]),
            "median_abs_gap_alt_14d_pct": round(100 * statistics.median(abs(alt_g["binance"][d] - alt_g[v][d]) for d in common), 2)
            if common else None}
        last = max(d for d in ser[v][2] if d in ser["binance"][2])
        rep["share"][v] = {"day": last, "alt_oi_bn": round(ser[v][2][last] / 1e9, 2),
                           "binance_alt_oi_bn": round(ser["binance"][2][last] / 1e9, 2),
                           "major_oi_bn": round(ser[v][3][last] / 1e9, 2),
                           "binance_major_oi_bn": round(ser["binance"][3][last] / 1e9, 2),
                           "alt_contracts": sum(1 for s, c in ccls.items() if c == "alt" and R.venue_of(s) == v)}

    S_bn = R.build(R.load_oi(["binance"]), cm)
    out = {}
    for name, S in (("binance", S_bn), ("all", S_all)):
        score, comp, al, ev = R.run(S, R.PARAMS_V2)
        e = ev["all"]
        out[name] = {"score": score, "alerts": al,
                     "eval": {k: e.get(k) for k in ("alerts", "per_year", "kinds", "median_dd", "median_up", "random_dd",
                                                    "control_dd", "p_rot_dd")},
                     "eval_no_rule": {k: R.run(S, dict(R.PARAMS_V2, rally_rule=False))[3]["all"].get(k)
                                      for k in ("alerts", "median_dd", "random_dd", "p_rot_dd")},
                     "eval_delay1": {k: R.run(S, dict(R.PARAMS_V2, delay=1))[3]["all"].get(k)
                                     for k in ("alerts", "median_dd", "random_dd", "p_rot_dd")},
                     "latest": {"d": max(score), "alr": score[max(score)], **comp[max(score)]}}
    first_multi = min(rep["inputs"][v]["from"] for v in others if rep["inputs"][v]["from"]) if others else None
    common = [d for d in sorted(out["binance"]["score"]) if d in out["all"]["score"] and first_multi and d >= first_multi]
    a = [out["binance"]["score"][d] for d in common]
    b = [out["all"]["score"][d] for d in common]
    hot_b = {d for d in common if out["binance"]["score"][d] >= 95}
    hot_a = {d for d in common if out["all"]["score"][d] >= 95}
    al_b = [d for d in out["binance"]["alerts"] if not first_multi or d >= first_multi]
    al_a = [d for d in out["all"]["alerts"] if not first_multi or d >= first_multi]
    near = lambda d, xs: min((abs((R.s2d(d) - R.s2d(x)).days) for x in xs), default=None)  # noqa: E731
    rep["alr"] = {"from": first_multi, "days": len(common), "corr": corr(a, b),
                  "median_abs_diff": round(statistics.median(abs(x - y) for x, y in zip(a, b)), 2) if common else None,
                  "days_ge95_binance": len(hot_b), "days_ge95_all": len(hot_a), "days_ge95_both": len(hot_b & hot_a),
                  "alerts_binance": al_b, "alerts_all": al_a,
                  "binance_alerts_matched_within_7d": sum(1 for d in al_b if near(d, al_a) is not None and near(d, al_a) <= 7),
                  "all_alerts_matched_within_7d": sum(1 for d in al_a if near(d, al_b) is not None and near(d, al_b) <= 7),
                  "binance_only": [d for d in al_b if near(d, al_a) is None or near(d, al_a) > 7],
                  "all_only": [d for d in al_a if near(d, al_b) is None or near(d, al_b) > 7]}
    for name in ("binance", "all"):
        o = out[name]
        rep[name] = {k: o[k] for k in ("alerts", "eval", "eval_no_rule", "eval_delay1", "latest")}
    path = os.path.join(ROOT, "research", "alt_radar", "venues.json")
    with open(path, "w") as f:
        json.dump(rep, f, indent=1, sort_keys=True)
    print(json.dumps({k: rep[k] for k in ("venues", "inputs", "share", "alr")}, indent=1))
    for name in ("binance", "all"):
        print(name, "alerts:", rep[name]["alerts"])
        print("  eval", rep[name]["eval"])
        print("  no rule", rep[name]["eval_no_rule"], "delay1", rep[name]["eval_delay1"])
        print("  latest", rep[name]["latest"])


if __name__ == "__main__":
    main()
