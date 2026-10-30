#!/usr/bin/env python3
"""The hourly layer of the site, as one long job that hands over to the next (GitHub's cron fires too
seldom and too late for hourly work; .github/workflows/liq-live.yml dispatches the next run when this
one ends, and live-watchdog.yml restarts the chain if it ever breaks).

Every `--every` minutes: a snapshot of the live order books (fetch_book_live.py). Each newly completed
UTC hour: the other live values (fetch_live_extras.py), then the site is rebuilt and published, so the
page is at most about an hour behind; then the data workflows in KICK are started (GitHub's own schedule
fires on this repo about one run in five, often hours late: book-data's two-hourly cron ran once on
2026-10-29). The site is also rebuilt in any round in which new Binance files from book-data arrived on
main (the other workflows publish the page themselves). Data commits go to main, the site to the "site"
branch. The loop ends after
`--minutes`, or early when the live scripts, the template or the workflow change on main (so a new
version takes over within one round). It leaves the minutes run and the reason in
$RUNNER_TEMP/live_minutes and live_reason for the workflow's hand-over step.
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_book_live as FB  # noqa: E402
import fetch_live_extras as FX  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(ROOT, "data", "ck", "live", "_meta.json")
WATCH = ["scripts", "site", ".github/workflows/liq-live.yml"]
TMP = os.environ.get("RUNNER_TEMP", "/tmp")
# workflows the chain starts: {file: minimum minutes since its last run}. Each does little when nothing is new.
KICK = {
    "fetch.yml": 30,         # ETF flows (issuers report after the US close and through the next morning), prices, the page
    "indicators.yml": 30,    # the daily sources of the indicator library (stablecoins, CFTC, rates, App Store, ...)
    "book-data.yml": 30,     # Binance's daily order-book and candle files (out about 21-23 hours after the day)
    "radar.yml": 30,         # the leverage radar (Binance's new day, Hyperliquid's midnight snapshot)
    "okx-history.yml": 180,  # OKX's replayed books (out a day later): calibration, and hours the live loop missed
    "binspot.yml": 10,       # the Binance spot book collector: its jobs hand over to each other, this restarts it if none runs
}
BINSPOT = os.path.join(ROOT, "data", "ck", "live", "binspot")


def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True)
    if r.returncode and check:
        raise RuntimeError(f"{cmd}: {r.stderr[-300:]}")
    return r


def tree():
    return {p: sh(f"git rev-parse HEAD:{p}").stdout.strip() for p in WATCH}


def push(msg, paths="data/ck/live"):
    sh(f"git add -A {paths}")
    if sh("git diff --cached --quiet").returncode == 0:
        return True
    sh(f'git commit -q -m "{msg}"')
    for _ in range(5):
        if sh("git pull -q --rebase -X theirs origin main").returncode == 0 and sh("git push -q origin HEAD:main").returncode == 0:
            return True
        sh("git rebase --abort")
        time.sleep(7)
    print("::warning::could not push", msg, flush=True)
    return False


def publish():
    out = os.path.join(TMP, "site")
    sh(f"rm -rf {out}")
    r = sh(f"{sys.executable} scripts/build_site.py --out {out}")
    if r.returncode:
        print("build failed:", r.stderr[-800:], flush=True)
        return False
    token, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("no token: built only", flush=True)
        return False
    cmds = ["git init -q -b site", 'git config user.name "etf-flows-bot"',
            'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"', "git add -A",
            f'git commit -q -m "site: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}"']
    for c in cmds:
        subprocess.run(c, shell=True, cwd=out, capture_output=True)
    for _ in range(3):
        if subprocess.run(f"git push -q -f https://x-access-token:{token}@github.com/{repo}.git HEAD:site", shell=True, cwd=out,
                          capture_output=True).returncode == 0:
            return True
        time.sleep(5)
    return False


def gh(path, body=None):
    token, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def kick():
    """Start each workflow in KICK unless a run of it is queued or running or one started recently."""
    now, out = dt.datetime.now(dt.timezone.utc), {}
    for wf, gap in KICK.items():
        try:
            runs = (gh(f"actions/workflows/{wf}/runs?per_page=3") or {}).get("workflow_runs")
            if runs is None:
                out[wf] = "no token"
                continue
            if any(r["status"] != "completed" for r in runs):
                out[wf] = "busy"
                continue
            last = max((dt.datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) for r in runs), default=None)
            if last and (now - last).total_seconds() < gap * 60:
                out[wf] = "recent"
                continue
            gh(f"actions/workflows/{wf}/dispatches", {"ref": "main"})
            out[wf] = "started"
        except Exception as e:
            out[wf] = f"{type(e).__name__}: {str(e)[:80]}"
    print("kick:", out, flush=True)
    return out


def binspot_status():
    """The Binance spot collector (binspot.yml): its newest hour on main, and its last saved books (every three hours
    and at each hand-over) with its status then."""
    out = {}
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(BINSPOT, "**", "*.csv.gz"), recursive=True))
    if files:
        out["newest_hour"] = max(f[:13] for f in files)
    try:
        c = gh("commits/binspot-state") or {}
        meta = json.loads(c["commit"]["message"])
        out.update({k: meta.get(k) for k in ("at", "run", "final", "to")}, **(meta.get("status") or {}))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


def data_rev():
    """A fingerprint of the Binance files book-data adds (the tree hashes under data/ck/book and data/ck/px1h,
    without their run reports; it works in the runner's shallow clone). That workflow does not publish the
    page itself, while fetch, indicators and radar do: rebuilding after them too would only add Pages builds."""
    rows = sh("git ls-tree HEAD data/ck/book/ data/ck/px1h/").stdout.splitlines()
    return hashlib.sha1("\n".join(ln for ln in rows if not ln.endswith("_meta.json")).encode()).hexdigest()[:16]


def meta():
    try:
        return json.load(open(META))
    except (OSError, ValueError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=170)
    ap.add_argument("--every", type=float, default=10)
    a = ap.parse_args(argv)
    t_start = time.time()
    t_end = t_start + a.minutes * 60
    start_tree = tree()
    reason, rounds = "time", 0
    sh('git config user.name "etf-flows-bot"')
    sh('git config user.email "41898282+github-actions[bot]@users.noreply.github.com"')
    try:
        while True:
            t0 = time.time()
            rounds += 1
            try:
                FB.main(["--rounds", "1", "--gap", "0"])
            except Exception:
                traceback.print_exc()
            done = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H")
            new_hour = meta().get("published_hour") != done
            if new_hour:
                try:
                    FX.main()
                except Exception:
                    traceback.print_exc()
            if new_hour or data_rev() != meta().get("published_rev"):
                push(f"live: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}")
                rev = data_rev()
                if publish():
                    m = meta()
                    m["published_hour"], m["published_rev"] = done, rev
                    m["published_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    with open(META, "w") as f:
                        json.dump(m, f, indent=1, sort_keys=True)
                    push(f"live: published {done}")
                    print(f"published {done}{'' if new_hour else ' (new data)'} ({time.time() - t0:.0f}s)", flush=True)
                if new_hour:
                    m = meta()
                    m["kick"] = {"at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **kick()}
                    m["binspot"] = binspot_status()
                    with open(META, "w") as f:  # committed with the next push
                        json.dump(m, f, indent=1, sort_keys=True)
            elif rounds % 2 == 0:
                push(f"live book: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}")
            # --autostash: in a round that commits nothing, the new snapshot is still uncommitted, and a plain
            # pull would refuse to run (so updates were only seen every other round)
            sh("git pull -q --rebase --autostash -X theirs origin main")
            if tree() != start_tree:
                reason = "update"
                break
            # rounds on the clock (:00:20, :10:20, ...): an hour's six snapshots are evenly spread, and the
            # hour's publish comes right after it closes
            step = a.every * 60
            nxt = (time.time() // step + 1) * step + 20
            if nxt + 60 > t_end:
                break
            time.sleep(max(0.0, nxt - time.time()))
        push(f"live book: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}")
    except Exception:
        reason = "error"
        traceback.print_exc()
    finally:
        mins = (time.time() - t_start) / 60
        for name, val in (("live_minutes", f"{mins:.0f}"), ("live_reason", reason)):
            with open(os.path.join(TMP, name), "w") as f:
                f.write(val)
        print(f"loop ended: {reason} after {mins:.0f} minutes, {rounds} rounds", flush=True)


if __name__ == "__main__":
    main()
