# 04 — Exit Strategy (Daily Checks Per Open Position)

`check_exits()` in `engine/portfolio_simulator.py` runs once per trading
day for every open position and evaluates conditions **in a strict
priority cascade** — as soon as one condition sets `exit_reason`, every
later `if exit_reason is None:` check is skipped for that position that
day. Two conditions (`DELISTED`, `MA100_BREAKDOWN`) and the profit-taking
pair (`TRAIL_STOP`, `TAKE_PROFIT`) can fire from day 1; all the rest
(`MA50_CROSS`, `BELOW_MA_DECLINING`, `GM_EROSION_VETO`, `MAX_HOLD`) are
gated behind `trading_days_held >= MIN_HOLD_DAYS`. All threshold values
below are the current live values loaded from
`config/strategy_params.json` at simulator startup.

Note: `engine/virtual_trader.py` is a **separate, older paper-trading
script** with its own independently hardcoded exit rules (tiered trailing
stop, fixed stop-loss per confidence tier, time stop, drawdown-duration
stop). It does not read `strategy_params.json` and is not driven by the
optimizer loop (`trade_optimizer.py` only ever invokes
`portfolio_simulator.py`) — it is not part of the exit cascade documented
here.

```mermaid
flowchart TD
    START(["New trading day: for each open position"]) --> D0{"0. DELISTED:\nprice series ended >= 15 trading days\nbefore sim end, and today is past last price?"}
    D0 -->|Yes| EXIT_DELIST(["EXIT: DELISTED\n(uses last available close)"])
    D0 -->|No| UPDATE["Update days_below_ma100 counter:\ncurrent_price < MA100 x (1 - 3%)?\nincrement counter, else reset to 0"]

    UPDATE --> D05{"0.5 MA100_BREAKDOWN:\ndays_below_ma100 >= 10\nconsecutive days?"}
    D05 -->|Yes| EXIT_MA100(["EXIT: MA100_BREAKDOWN (10d below MA100)\n-- fires regardless of MIN_HOLD_DAYS"])
    D05 -->|No| TRAILACT["Compute gain_from_cost = (price - avg_cost)/avg_cost\nActivate trailing stop once gain >= 10%\n(trail_activate_gain_pct)"]

    TRAILACT --> D1{"1. TRAIL_STOP:\ntrail_activated == True\nAND price <= peak x (1 - 15.5%)?\n(trailing_stop_pct)"}
    D1 -->|Yes| EXIT_TRAIL(["EXIT: TRAIL_STOP\n(peak=.. stop=..)\n-- fires regardless of MIN_HOLD_DAYS"])
    D1 -->|No| D15{"1.5 TAKE_PROFIT:\ngain_from_cost >= 75%?\n(take_profit_pct)"}
    D15 -->|Yes| EXIT_TP(["EXIT: TAKE_PROFIT\n-- fires regardless of MIN_HOLD_DAYS"])

    D15 -->|No| MINHOLD{"past_min_hold =\ntrading_days_held >= 5?\n(min_hold_days)"}
    MINHOLD -->|No| HOLD(["No exit today — position held\n(only DELIST/MA100_BREAKDOWN/\nTRAIL_STOP/TAKE_PROFIT could fire pre-min-hold)"])

    MINHOLD -->|Yes| D2{"2. MA50_CROSS:\ntrading_days_held >= ma_confirm_days(4)\nAND last 4 closes all\n< MA50 x (1 - 3%)\nAND price was ever above MA50\nsince entry?"}
    D2 -->|Yes| EXIT_MACROSS(["EXIT: MA50_CROSS (4d confirm)\n(momentum_exit_ma = 50)"])
    D2 -->|No| D3{"3. BELOW_MA_DECLINING:\nbelow BOTH MA50 and MA100\nAND 20-day return < -8.5%?\n(below_ma_trend_floor)"}
    D3 -->|Yes| EXIT_BELOWMA(["EXIT: BELOW_MA_DECLINING\n(below MA50+MA100, 20d_ret < -8.5%)"])

    D3 -->|No| D4{"4. GM_EROSION_VETO:\nGM erosion (as-of PIT snapshot)\n> 20% if cyclical sector\n> 12% if non-cyclical?\n(gm_erosion_cyclical_thr / noncyc_thr)"}
    D4 -->|Yes| EXIT_GMEROSION(["EXIT: GM_EROSION_VETO\n(cyclical sectors: Semi/Solar/Semiconductor/\nMajor Proc/Memory/Foundry/Connectivity/\nAnalog/Emerging/Small/CleanTech keywords)"])
    D4 -->|No| D5{"5. MAX_HOLD:\ntrading_days_held >= 250?\n(max_hold_days)"}
    D5 -->|Yes| EXIT_MAXHOLD(["EXIT: MAX_HOLD (250d)"])
    D5 -->|No| HOLD

    EXIT_DELIST & EXIT_MA100 & EXIT_TRAIL & EXIT_TP & EXIT_MACROSS & EXIT_BELOWMA & EXIT_GMEROSION & EXIT_MAXHOLD --> CLOSE["Position closed at exit_price:\nshares sold, commission deducted,\npnl_pct / pnl_dollars recorded,\ntrade appended to closed_trades[]"]
```

## Priority order summary (first match wins, evaluated in this exact order)

1. `DELISTED` — no min-hold gate
2. `MA100_BREAKDOWN` (10 consecutive days beyond -3% of MA100) — no min-hold gate
3. `TRAIL_STOP` (peak x -15.5%, only once trail activated at +10% gain) — no min-hold gate
4. `TAKE_PROFIT` (+75% gain from avg cost) — no min-hold gate
5. *(gate: `trading_days_held >= MIN_HOLD_DAYS(5)`)*
6. `MA50_CROSS` (4-day confirm below MA50, only if price was ever above MA50 since entry)
7. `BELOW_MA_DECLINING` (below MA50 and MA100, 20d return < -8.5%)
8. `GM_EROSION_VETO` (cyclical > 20%, non-cyclical > 12%)
9. `MAX_HOLD` (250 trading days)
