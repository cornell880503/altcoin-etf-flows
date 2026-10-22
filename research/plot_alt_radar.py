#!/usr/bin/env python3
"""Chart of the ALR v2 backtest: altcoin market cap (ex BTC, ETH, stablecoins) with every alert,
and the ALR line below it. Reads research/alt_radar/{series,backtest}.json; writes
research/alt_radar/alt_radar_backtest.png."""
import datetime as dt
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "research", "alt_radar")

for f in font_manager.findSystemFonts():
    if "NotoSansCJK" in f:
        font_manager.fontManager.addfont(f)
plt.rcParams["font.family"] = ["Noto Sans CJK TC", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

s = json.load(open(os.path.join(D, "series.json")))
b = json.load(open(os.path.join(D, "backtest.json")))["v2"]
f = s["fields"]
rows = {d: dict(zip(f, v)) for d, v in s["rows"].items()}
days = sorted(d for d in rows if d >= "2023-04-01")
x = [dt.date.fromisoformat(d) for d in days]
mc = [rows[d]["alt_mcap_bn"] for d in days]
alr = [rows[d]["alr_v2"] for d in days]
kind = {e["date"]: e["kind"] for e in b["eval"]["all"]["list"]}

BG, FG, GRID, RED, GREEN, GREY = "#0f1115", "#e6e8eb", "#2a2e36", "#ef5350", "#26a69a", "#9aa0a6"
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7.2), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]},
                               facecolor=BG)
for ax in (ax1, ax2):
    ax.set_facecolor(BG)
    ax.tick_params(colors=FG, labelsize=9)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.6)
ax1.plot(x, mc, color=FG, linewidth=1.1)
for d in b["alerts"]:
    if d < days[0]:
        continue
    k = kind.get(d)
    c = RED if k == "down" else GREEN if k else GREY
    ax1.scatter([dt.date.fromisoformat(d)], [rows[d]["alt_mcap_bn"] * 1.06], marker="v", s=55, color=c, zorder=5)
ax1.set_ylabel("山寨市值（十億美元）", color=FG)
ax1.set_title("山寨槓桿雷達 v2 回測：每一次警報（2023/04–2026/10）", color=FG, fontsize=13, loc="left")
ax1.scatter([], [], marker="v", color=RED, label="之後 30 天跌多於漲")
ax1.scatter([], [], marker="v", color=GREEN, label="先跌後漲")
ax1.scatter([], [], marker="v", color=GREY, label="觀察期未滿 30 天")
leg = ax1.legend(loc="upper left", fontsize=9, facecolor=BG, edgecolor=GRID)
for t in leg.get_texts():
    t.set_color(FG)
ax2.plot(x, alr, color="#64b5f6", linewidth=0.9)
ax2.axhline(95, color=RED, linewidth=0.8, linestyle="--")
ax2.set_ylim(0, 102)
ax2.set_ylabel("ALR", color=FG)
ax2.text(x[0], 97, "95：警報門檻", color=RED, fontsize=8, va="bottom")
ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m"))
fig.text(0.01, 0.01, "山寨市值不含 BTC、ETH、穩定幣（Coin Metrics）；槓桿用 Binance 永續合約持倉（data.binance.vision）",
         color=GREY, fontsize=8)
fig.tight_layout(rect=(0, 0.03, 1, 1))
out = os.path.join(D, "alt_radar_backtest.png")
fig.savefig(out, dpi=150, facecolor=BG)
print(out)
