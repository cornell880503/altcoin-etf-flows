# CoinKarma's Karma Index: "a buy signal again" (their post of 2026-11-26)

**The claim.** The Karma Index, their "cyclical indicator" for BTC's medium/long-term cycle, has triggered a buy
signal again; such signals "have appeared several times near BTC's medium/long-term relative bottoms". The chart:
BTC/USDT daily from 2022 with the index below, levels 20 and 80, last value 19.00, seven yellow boxes on the
stretches below 20. The same post carries their Black Friday offer.

**Their definition** ([guide](https://docs.coinkarma.co/english/indicators-guide/karma-index)). A composite of five
groups of data (spot liquidity, on-chain, off-exchange sentiment, exchange flows, derivatives); below 20 a possible
relative bottom, above 80 a possible relative top. No formula, weights or lookback. Their own comparison (buy 10 %
of capital on each buy signal, close half on each sell signal) reports a 62.5 % maximum drawdown for it. The value
is not in the Telegram table we read; it is a different thing from that table's Pulse Index.

**Our series.** There is none: it cannot be computed. The only history is their chart, so it was digitized
(`research/karma_index_digitize.py` -> `karma_index_2026-11-26.json`; the image itself is not in the repo). The time
axis is fitted to our own BTC closes (residual 1.8 px = 1.8 % in price, every section of the chart within a quarter
pixel), one pixel column is 2.47 days: dates are good to about +-3 days, levels to +-1.5.

**What the chart says** (`research/karma_index_backtest.py`, `karma_index_backtest.json`). Ranges are over the five
days in which the first reading below 20 fell.

| first below 20 | BTC then | days below 20 | lowest close afterwards | 180 days later | a year later |
|---|---|---|---|---|---|
| 2022-06-17..21 | 19.0-20.7K | 2 | 15.6K, 153 days on (-18..-24 %) | -19..-8 % | +30..+45 % |
| 2022-09-19..23 | 18.5-19.5K | 14 | 15.6K, 59 days on (-15..-20 %) | +38..+50 % | +37..+44 % |
| 2022-11-05..09 | 15.9-21.3K | 38 | 15.6K, 12 days on (-2..-27 %) | +35..+74 % | +65..+130 % |
| 2023-09-09..13 | 25.2-26.2K | 2 | none (-1..+4 %) | +158..+174 % | +112..+129 % |
| 2026-02-03..07 | 63.0-75.9K | 88 | 56.1K, 287 days on (-11..-26 %) | -16..+2 % | not yet (today -20..-4 %) |
| 2026-05-27..31 | 73.5-74.5K | 53 | 56.1K, 174 days on (-24..-25 %) | not yet | today -19..-17 % |
| 2026-11-11..15 | 64.2-74.0K | 14, running | 56.1K, 6 days on (-13..-24 %) | | today -18..-5 % |

- It is a state, not an event. 2026-02..08 had about 141 days below 20. The present run began about 2026-11-13,
  eight days before the low so far; the post of 11-26 is not a fresh trigger. The level has crept up inside the zone
  (18 -> 19) and sits just under the line.
- After the first reading the price went lower by 11 % or more in 5 of 7 episodes even from the lowest close of the
  entry window; the low came 6 days to 9.5 months later.
- A year on, the four 2022-23 entries were 30-130 % higher. The 2026 ones are not a year old and are all under water.
- The lowest readings on the whole chart are 2026-03/04 (a column mean of 12.5; 2022's lows were 17-19), with BTC
  at 65-70K; nine months later the price made a new low.
- It does say more than "the price has fallen a lot". Of the days with BTC 30 % or more below its one-year high,
  those below 20 were higher 90 days later 63 % of the time (median +17 %), the others 38 % (-10 %); 180 days:
  85 % against 38 %. The difference is mostly the first half of 2022: the price was already 30-45 % down and the
  index still at 40-55. (Overlapping days from three market phases: a description, not a sample of 200.)
- The sell side: above 80 only in 2024-03 and 2024-11/12; a year later BTC was +28..+52 % and -8..+4 %. The
  2025-10 high came with the index near 50.
- A caution that cannot be removed: this is today's version of a closed formula drawn back over the past. We cannot
  check what it showed in real time.

**Can we rebuild it?** No.
- Its level follows price position and futures basis (BTC's rank within two years r 0.81, 90-day RSI 0.79, basis
  0.77); a three-series fit reaches r 0.88 judged leave-one-year-out, but its lowest readings overlap the buy zone
  only about half the time, and the best single rule (90-day funding) 63 %.
- Their own Pulse Index (the table we read three times a day) averaged over 30-45 days tracks it at r 0.84-0.87
  since 2025-02. Its low stretches cover about 80 % of the zone, with entries days to weeks off (2026-10-19 against
  2026-11-13) and one false week (2025-11). So the Karma Index is roughly "the Pulse Index held low for weeks",
  which the table already shows: Pulse 14 on 2026-11-26, its 30-day mean 16, the lowest since July.

**Verdict.** Context, not a notification. It cannot be computed, it changes state a few times a year, and it does
not time a low (median wait to the low: months). What it adds to the hourly watch is one sentence of background
while it is on. Offered to Kevin on 2026-11-26: a report line with the 30-day mean of the Pulse Index as a stand-in,
labelled as an approximation, and no notification. Waiting for his answer; nothing in the watch was changed.

Historical statistics, not investment advice.
