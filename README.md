# altcoin-etf-flows

Daily net flows (US$ millions, per fund) for US spot crypto ETFs: BTC, ETH, SOL, XRP, HYPE, LINK, HBAR, AVAX, DOGE, LTC, SUI, DOT, BNB (and NEAR, TRX, ZEC when available).

- Source: the public flow pages of [cryptoetf.today](https://cryptoetf.today/en) (robots.txt allows them; `/api/` is not touched).
- Schedule: a GitHub Action checks every 3 hours and commits only when the numbers change, so the commit history is also a revision log.
- Files: `data/flows/<SYMBOL>.json` with `rows` = one object per day (`date`, `total`, one field per issuer), and `data/manifest.json` with coverage per asset.
- Raw URL pattern: `https://raw.githubusercontent.com/cornell880503/altcoin-etf-flows/main/data/flows/SOL.json`

Notes: dates are as published by the source. Canary funds are reported on settlement date (T+1); downstream analysis shifts them back one trading day. Seed capital on launch day can appear as a flow and should be excluded for analysis.
