# 06 — Regime Detection and Downstream Effects

The strategy classifies the market into one of four regimes every trading
day using `compute_ndx_regime()` in `engine/portfolio_simulator.py`, based
on QQQ (falls back to SPY if QQQ price data is unavailable) versus its
100-day moving average and its 20-day realized volatility. That single
label — recomputed fresh each day — then fans out to influence gate
thresholds, sector bans, and position sizing everywhere else in the
system.

**Discrepancy found while building this diagram:** `config/strategy_params.json`
contains a `regime_detection` block (`ndx_ma_period: 100`,
`bear_volatile_threshold: -0.25`, `bear_grind_threshold: -0.15`,
`bull_weak_threshold: -0.05`) that looks like it should parameterize this
classifier. **It is dead configuration** — a repo-wide grep for
`regime_detection`, `bear_volatile_threshold`, `bear_grind_threshold`,
`bull_weak_threshold`, and `ndx_ma_period` outside `strategy_params.json`
returns zero hits. `compute_ndx_regime()` hardcodes its own logic (100-day
window is hardcoded via `min(100, len(avail))`, and the split is
"above/below 100-day MA" x "20-day annualized vol >= 20.0", not the
percentage-decline thresholds the JSON implies). The optimizer could
"tune" these four JSON values indefinitely with zero effect on simulated
behavior.

```mermaid
flowchart TD
    START(["Each trading day: compute_ndx_regime(ndx_closes, date)"]) --> DATA["ndx_closes = QQQ daily closes\n(falls back to SPY closes if QQQ fetch failed)"]
    DATA --> ENOUGH{"Fewer than 25\nclose observations\navailable as of date?"}
    ENOUGH -->|Yes| DEFAULT(["Default: BULL_STRONG\n(insufficient history)"])
    ENOUGH -->|No| MA100["ma100 = mean of last min(100, available) closes\n(hardcoded 100-day window)"]
    MA100 --> ABOVE{"Latest close > ma100?"}
    ABOVE -->|Yes/No| VOLCHECK{"Fewer than 5\ndaily returns available?"}
    VOLCHECK -->|Yes| DEFAULT
    VOLCHECK -->|No| VOL["vol20 = stdev(last 20 daily returns)\nx sqrt(252) x 100\n(annualized realized vol, %)"]
    VOL --> HIGHVOL{"vol20 >= 20.0%?\n(HIGH_VOL_THR, hardcoded)"}

    HIGHVOL -->|"above MA100, vol < 20%"| BS(["BULL_STRONG"])
    HIGHVOL -->|"above MA100, vol >= 20%"| BW(["BULL_WEAK"])
    HIGHVOL -->|"below MA100, vol >= 20%"| BV(["BEAR_VOLATILE"])
    HIGHVOL -->|"below MA100, vol < 20%"| BG(["BEAR_GRIND"])

    BS & BW & BV & BG --> LABEL(["Regime label for today,\nstored as ndx_regime / _t._ndx_regime"])

    LABEL --> EFFECT1["EFFECT 1 — Entry gate pass_threshold()\n(diagram 01): base + REGIME_ADJUSTMENTS\nBULL_STRONG -0.5 / BULL_WEAK -0.2 /\nBEAR_GRIND +0.3 / BEAR_VOLATILE +0.6\n(semi universe exempt — COUNTER_CYCLICAL)"]
    LABEL --> EFFECT2["EFFECT 2 — G8 momentum bear-block\n(diagrams 01-02): in BEAR_VOLATILE/BEAR_GRIND,\nstocks below MA200 get 0 momentum points,\nno partial credit, in every universe"]
    LABEL --> EFFECT3["EFFECT 3 — Sector bans (BEAR_BANNED_SECTORS,\ndiagram 01 veto stage):\nBEAR_VOLATILE bans Cybersecurity, Solar\nInstallation, Renewable Utilities,\nCleanTech/Emerging, FinTech & Payments\nBEAR_GRIND bans Solar Installation,\nCleanTech/Emerging, FinTech & Payments"]
    LABEL --> EFFECT4["EFFECT 4 — Post-pass regime vetoes\n(diagram 01): energy/medtech G8=0\nin BULL_WEAK forces VETO"]
    LABEL --> EFFECT5["EFFECT 5 — Position sizing\nregime_position_mult (diagram 05):\nBULL_STRONG x1.0 / BULL_WEAK x0.8 /\nBEAR_GRIND x0.5 / BEAR_VOLATILE x0.35"]
    LABEL --> EFFECT6["EFFECT 6 — regime_exposure_cap\n(diagram 05, portfolio-level brake):\nBULL_STRONG 100% / BULL_WEAK 85% /\nBEAR_GRIND 60% / BEAR_VOLATILE 40%"]
    LABEL --> EFFECT7["EFFECT 7 — regime_max_positions\n(position count ceiling):\nBULL_STRONG 30 / BULL_WEAK 25 /\nBEAR_GRIND 15 / BEAR_VOLATILE 10"]
    LABEL --> EFFECT8["EFFECT 8 — Pyramiding gate (diagram 05):\nBEAR_VOLATILE blocks all adds;\nBEAR_GRIND caps adds at 1 per position"]
```
