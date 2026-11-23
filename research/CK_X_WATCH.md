# CoinKarma post watch (daily)

What Kevin asked on 2026-11-23: look at CoinKarma's new posts on X once a day. When they point at an indicator or a
rule we do not monitor yet, **do the research first, on your own**, then **ask him** whether to add it to the daily
watch. Nothing is added to the watch without his yes.

This file is the playbook for the scheduled task "CoinKarma 新貼文：每天輪詢、先研究、再問要不要加". Each run starts
with no memory of earlier runs: everything it needs is here, in `research/ck_x/seen.json`, and in the repo.

## 1. Find the new posts

Two routes, in this order. Use the first that works and say in the report which one was used.

1. **Kevin's own browser**, if one is available in the session and X is logged in there: the browser built into
   the Claude desktop app (tools named `...Claude_Browser__*`; it needs the desktop app open on his computer and
   the task set to require that computer) or Claude in Chrome (`mcp__claude-in-chrome__*`). Open a new tab at
   `https://x.com/coinkarma_`, list the posts (link, time, text, whether there is an image), scroll a few screens.
   For a post with a chart, open the image itself (`pbs.twimg.com/media/...`, `name=large`) and look at it: the
   panels' names, the levels drawn, the dates marked. Close every tab you opened.
   On 2026-11-23 Chrome was reachable but **not** logged in to X (a login wall, no timeline), and the built-in
   browser was not reachable (his computer was not connected); each keeps its own sign-ins, so X has to be
   signed in once in whichever is used, by him. Never type a password; if it is logged out or absent, say so in
   one line and use route 2.
2. **News wires that relay their posts.** ChainCatcher, BlockBeats, BingX flash news, Bloomingbit, BitPush and KuCoin
   news repeat CoinKarma's posts within minutes (2026-11-23: posted about 20:40 UTC+8, on ChainCatcher at 20:47,
   with a link to the post). WebSearch `CoinKarma` with the current month and year, and `CoinKarma 指標`,
   `CoinKarma analysis BTC`; open the hits with WebFetch. **Check each item's own date**: a search on 2026-11-23
   returned a BingX item dated 2025-11-23 that read like news of the day. A wire gives the claim, not the chart.

X itself, its syndication endpoints and Followin refuse automated fetching (robots). Do **not** go around that with
curl, a script, a GitHub runner, a cache or a mirror. If neither route yields anything, report that the check could
not be made today; do not guess.

Their Telegram trial channel (`t.me/s/coinkarma_market_pulse_trial`) carries only the Market Pulse chart and table,
three times a day; `scripts/ck_pulse.py` already reads it every hour into `data/ck/pulse/pulse.csv`.

## 2. What is new

`research/ck_x/seen.json` lists every post already handled (by X status id when known, else by date and topic).
A post not in it is new. Add **every** post you look at, also the ones you skip, with a one-line reason, so no run
does the same work twice. Statuses: `skipped`, `researched` (asked Kevin, no answer yet), `added`, `context`
(added as report text only), `declined`.

Research a post when it claims that some indicator, level or combination says where the price goes next. Skip
promotions, giveaways, reposts and recaps of something already covered **unless** they state a new rule or level.

Already covered (do not redo; extend if they add a rule):

| Their indicator | Ours | Result |
|---|---|---|
| Overall / BTC / ETH LIQ, Accumulated BTC LIQ, Overall Liquidity | `research/liq_watch.py`, `scripts/ck_liq.py`, `research/ck_pulse_backtest.py` | watched hourly; green arrow, levels inside ETF red |
| BTC ETF 30D Net Flow | `scripts/btc_etf30.py`, `research/etf_flow_backtest.py` | watched; zones, into-red cycles, flip |
| Bitfinex BTCUSD LONGS, Bitfinex Premium | `scripts/bfx_whale.py`, `research/bfx_whale_backtest.py` | watched; a bounce signal at best, longs rise early |
| The table's other rows (LSUR Z, ALT Resilient Index, USDC/USDT premium, Binance USDT rate, funding APY, Pulse Index) | `research/ck_pulse_backtest.py`, tests of 2026-11-20 in `research/etf_flow_backtest.py`'s commit | no edge alone; Pulse <= 5 and ARI >= 2 do not mark lows |
| Their other site indicators (funding, basis, CFTC, stablecoins, MSTR, DVOL, lending rates, CVD, ETH/BTC whale) | `scripts/ck_indicators.py`, `scripts/fetch_ck.py`, `data/ck/raw/*.json` | on the dashboard, not in the watch |

## 3. The research (before asking him)

The standard this project holds itself to; the three scripts above are worked examples.

1. **Their definition.** Their guide (`docs.coinkarma.co/indicators-guide/...`), the chart's axes, labels and
   levels. Never commit their text; describe it in our own words.
2. **Our own series.** Rebuild it from data in the repo (`data/ck/raw`, `data/flows`, `data/coin_px`,
   `data/ck/pulse`, `data/ck/px1h`, `data/ck/live`). If something is missing, a free source that a GitHub runner
   can reach, fetched by a workflow (this workspace's own network is closed to most hosts). No paid API; a key
   only as a GitHub secret that Kevin adds himself, never printed, committed or pasted.
3. **Check it against theirs**: their table in `pulse.csv`, or the chart panel digitized (as for the 30D flow:
   r 0.98, slope 0.995, the latest label within 0.1K). State the fit and one exact value.
4. **Backtest** on the longest history there is, entering only when the value was knowable (publication lags:
   the ETF totals are final only with BlackRock's figure). Compare with every day and with comparable days
   (dips, the same regime). Count episodes, not overlapping rows; with few cases list every one. Try the
   neighbouring thresholds and say if the result depends on one cell. Report what fails.
5. **Verdict**, one of: an event worth a notification; context only (a report line or a sentence in the verdict);
   not worth adding. Say what it would have told him in the market of the last weeks.

Write `research/ck_x/<YYYY-MM-DD>-<slug>.md` (short: the claim, the definition, the fit, the numbers, the
verdict), the scripts and their JSON, add the post to `seen.json`, commit and push to `main` (retry on GitHub 5xx;
`git pull --rebase` first). Until he says yes, do **not** change `research/liq_watch.py`,
`scripts/binance_spot_ws.py`, the workflows that feed the notices, or any scheduled task.

## 4. Tell him and ask

One message in Traditional Chinese (SendUserMessage; he is not watching the run):

- what they posted, in one or two lines of our own words, with the link;
- what our test found: the numbers, how many cases, where it failed;
- the recommendation (notification / context only / do not add) and why;
- the question: 要不要加進監看？
- 歷史統計，不是投資建議。

Nothing new today: one line saying so (and which route was used). Do not send a research report for a post that
was skipped.

## 5. After a yes

As the earlier ones were done: a module in `scripts/`, a backtest JSON that `fetch.yml` refreshes daily, a line in
the report and, if warranted, one event in `research/liq_watch.py` with a once-per-episode rule; exercise
`decide()` with crafted inputs; commit; confirm the next hourly `research/liq_watch_latest.txt` on `main` shows it.
Mark the post `added` (or `context`, `declined`) in `seen.json`.

## Rules that always apply

- Financial topics: facts and historical statistics, no recommendations to buy or sell; say it is not investment advice.
- Reply to Kevin in Traditional Chinese.
- API keys (cryptoetf.today, Coinalyze, Etherscan, Telegram, CoinGlass): never printed, committed, pasted or stored.
- The IBKR connector: read-only market data; never create, edit or delete orders, alerts or watchlists.
- Git: the repo's identity (`Claude <noreply@anthropic.com>`), never Kevin's e-mail; no force-push; end commit
  messages with the attribution lines the session gives. `[skip actions]` only on commits that must not start a workflow.
- Pushing `scripts/binance_spot_ws.py` restarts the collector (a hand-over); pushing `scripts/**` starts `fetch.yml`.
- Close the browser tabs you open. Do not post, like, follow or message anything on X.
