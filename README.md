# altcoin-etf-flows

Daily net flows (US$ millions, per fund) for US spot crypto ETFs: BTC, ETH, SOL, XRP, HYPE, LINK, HBAR, AVAX, DOGE, LTC, SUI, DOT, BNB, NEAR, TRX and ZEC, each coin's own price, and a dashboard that tests whether the flows lead the price.

- Sources: [cryptoetf.today](https://cryptoetf.today/en) (public flow pages, plus its API with a key kept as an Actions secret), Canary Capital's own fund data, each coin's price from Coinbase (Binance or Hyperliquid when a pair is missing), market caps from CoinGecko.
- Schedule: a GitHub Action runs every 3 hours, commits only when the numbers change (so the commit history is also a revision log), and rebuilds the website.
- Files: `data/daily.json` (cleaned daily flows per coin and fund, by US trade date), `data/coin_px/<SYMBOL>.json` (the coin's price at 10:00 and 16:00 New York and its daily close at 00:00 UTC), `data/mcap/`, `data/flows/<SYMBOL>.json` (as published).
- Raw URL pattern: `https://raw.githubusercontent.com/cornell880503/altcoin-etf-flows/main/data/daily.json`

Notes: Canary funds are reported on settlement date (T+1); the cleaning shifts them back one trading day. Seed capital before an ETF's first trading day is dropped. Every return and statistic uses the coin's own daily close at 00:00 UTC (08:00 Singapore; that UTC day holds the whole US session), never an ETF's share price.

## Website

Live at **https://cornell880503.github.io/altcoin-etf-flows/** (GitHub Pages, published from the `site` branch; it refreshes by itself after every run of the workflow).

`scripts/build_site.py` turns the data into one static page: `site/template.html` (the dashboard; its statistics run in the browser) plus the fixed research results in `site/research.json`. The workflow builds it after every data refresh and force-pushes it to the `site` branch, which always holds a single commit:

- `https://raw.githubusercontent.com/cornell880503/altcoin-etf-flows/site/index.html`
- `https://raw.githubusercontent.com/cornell880503/altcoin-etf-flows/site/health.json` (build time, latest trading day)

A build stops without publishing when the data look wrong (too few days, no recent flows or prices), so the last good page stays up.

### Serve it from your own server

On a Debian or Ubuntu server (for example a Linode Nanode), run as root:

```sh
curl -fsSL https://raw.githubusercontent.com/cornell880503/altcoin-etf-flows/main/deploy/install.sh | sudo bash
```

It installs Caddy and a systemd timer that fetches the page at :07 and :37 every hour, checks it and swaps it in. The server needs no keys or tokens. Running it again is safe; it rewrites the web server config from the settings given on that run. Optional settings go before `bash`:

| Setting | Effect |
|---|---|
| `DOMAIN=etf.example.com` | also serve `https://etf.example.com` (certificate from Let's Encrypt; the DNS A record must point to the server) |
| `SITE_PASSWORD=...` | ask for a password (user name `etf`) |
| `AUTO_UPDATES=0` | leave the system's automatic security updates alone (by default they are switched on) |

Example: `curl -fsSL .../deploy/install.sh | sudo DOMAIN=etf.example.com bash`

Day to day:

```sh
systemctl list-timers altetf-update.timer   # next fetch
cat /var/www/altetf/health.json             # what the page was built from
sudo systemctl start altetf-update          # fetch now
journalctl -u altetf-update -n 20           # recent fetches
```

If the page does not open, allow inbound TCP 80 (and 443 for a domain) in the Linode Cloud Firewall, if one is attached. To remove it: `sudo systemctl disable --now altetf-update.timer caddy && sudo rm -f /etc/systemd/system/altetf-update.* /usr/local/bin/altetf-update`.
