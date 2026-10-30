#!/usr/bin/env python3
"""The hourly layer of the site, as one long job that hands over to the next (GitHub's cron fires too
seldom and too late for hourly work; .github/workflows/liq-live.yml dispatches the next run when this
one ends, and live-watchdog.yml restarts the chain if it ever breaks).

Every `--every` minutes: a snapshot of the live order books (fetch_book_live.py). Each newly completed
UTC hour: the other live values (fetch_live_extras.py), then the site is rebuilt and published, so the
page is at most about an hour behind. Data commits go to main, the site to the "site" branch. The loop
ends after `--minutes`, or early when the live scripts, the template or the workflow change on main (so a
new version takes over within one round). It leaves the minutes run and the reason in
$RUNNER_TEMP/live_minutes and live_reason for the workflow's hand-over step.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_book_live as FB  # noqa: E402
import fetch_live_extras as FX  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(ROOT, "data", "ck", "live", "_meta.json")
WATCH = ["scripts", "site", ".github/workflows/liq-live.yml"]
TMP = os.environ.get("RUNNER_TEMP", "/tmp")


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
            if meta().get("published_hour") != done:
                try:
                    FX.main()
                except Exception:
                    traceback.print_exc()
                push(f"live: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}")
                if publish():
                    m = meta()
                    m["published_hour"] = done
                    with open(META, "w") as f:
                        json.dump(m, f, indent=1, sort_keys=True)
                    push(f"live: published {done}")
                    print(f"published {done} ({time.time() - t0:.0f}s)", flush=True)
            elif rounds % 2 == 0:
                push(f"live book: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}")
            sh("git pull -q --rebase -X theirs origin main")
            if tree() != start_tree:
                reason = "update"
                break
            nxt = t0 + a.every * 60
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
