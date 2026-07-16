# Strategy Tester

A Python-based quantitative strategy research system that scores stocks against an 8-gate fundamental + momentum framework, backtests portfolio performance with point-in-time data, and uses AI agents to continuously optimize the parameters.

---

## Project Structure

```
STRATEGY_TESTER/
│
├── engine/                      # Core strategy logic
│   ├── tester.py                # Gate scoring engine — produces gate_report_latest.json
│   ├── portfolio_simulator.py   # Day-by-day portfolio backtest
│   └── virtual_trader.py        # Paper-trading simulator on live BUY signals
│
├── data_pipeline/               # Data acquisition & preparation
│   ├── data_fetcher.py          # Fetch prices + fundamentals (yfinance + NDL)
│   ├── build_feature_cache.py   # Build data/feature_cache.db (one-time, ~hours)
│   ├── db.py                    # SQLite abstraction layer
│   ├── pit_fundamentals.py      # Point-in-time fundamentals builder
│   └── sf1_loader.py            # SHARADAR SF1 historical data loader
│
├── optimizers/                  # AI-driven optimization tools
│   ├── auto_optimizer.py        # 3-agent gate/code optimizer (Haiku + GPT + Opus)
│   ├── trade_optimizer.py       # 2-agent parameter optimizer driven by trade evidence
│   ├── walk_forward.py          # Walk-forward robustness tester (4 market regimes)
│   ├── stop_optimizer.py        # Stop-loss parameter grid search
│   └── trade_analyzer_prep.py   # Stratified trade sampler for optimizer
│
├── reporting/                   # Report generation
│   └── opportunity_report.py    # Missed winners / dodged losers analysis
│
├── config/                      # All configuration (single source of truth)
│   ├── strategy_manifest.json   # Strategy constants V30 (gates, weights, thresholds)
│   ├── strategy_params.json     # Live tunable parameters (read at runtime)
│   └── current_best_params.json # Best confirmed parameter snapshot
│
├── data/                        # Databases and market data CSVs
│   ├── feature_cache.db         # (gitignored — 1.5 GB, rebuild with data_pipeline/build_feature_cache.py)
│   ├── market_data.db           # (gitignored — 150 MB, rebuild with data_pipeline/data_fetcher.py)
│   ├── pit_fundamentals.csv     # Point-in-time fundamentals export
│   └── multi_sector_trend_*.csv # Sector trend data for gate scoring
│
├── reports/                     # Generated outputs (recreated on each run)
│   ├── gate_report_latest.json  # Live BUY/WATCH/AVOID signals (uploaded to VPS daily)
│   ├── gate_report_latest.xlsx  # Excel version of gate report
│   ├── strategy_advisor_report.txt
│   └── vt_results_latest.json
│
├── logs/                        # Runtime state & history
│   ├── change_log.txt           # Every optimizer change, kept or reverted
│   ├── params_history.json      # Full parameter history (read by Historian agent)
│   ├── performance_history.txt  # Backtest results across optimizer sessions
│   ├── cost_tracker.txt         # API cost tracking per session
│   └── last_run_state.json      # Optimizer session state
│
├── strategy_advisor.py          # Manual strategic review (Claude Opus)
├── run_daily_live.bat           # Daily runner: tester → upload to VPS
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Set API keys as environment variables:
```bash
set ANTHROPIC_API_KEY=sk-ant-...
set OPENAI_API_KEY=sk-...
set NASDAQ_DATA_LINK_API_KEY=...    # for data_pipeline/sf1_loader.py only
```

---

## Daily Operation

```bash
run_daily_live.bat
```

This runs `engine/tester.py` → writes `reports/gate_report_latest.json` → uploads to VPS.

---

## One-Time Database Setup

```bash
# 1. Fetch price + fundamentals into market_data.db
python data_pipeline/data_fetcher.py

# 2. Build feature cache for opportunity analysis (~hours)
python data_pipeline/build_feature_cache.py
```

---

## Backtesting

```bash
# Full 2020-2024 backtest
python engine/portfolio_simulator.py

# Walk-forward robustness test (4 market regimes)
python optimizers/walk_forward.py
```

---

## AI Optimization

```bash
# Gate/code optimizer (3-agent loop)
python optimizers/auto_optimizer.py --iterations 5 --budget 5.00

# Parameter optimizer driven by trade evidence
python optimizers/trade_optimizer.py --iterations 5 --budget 3.00

# Strategic advisor (manual, ~$0.50-1.50/run)
python strategy_advisor.py
```

---

## Strategy Architecture

- **Universe**: ~200 US tech, growth, and clean-energy stocks
- **Gates**: 8-gate fundamental + momentum scoring system (G1 Valuation → G8 Momentum)
- **Sectors**: Tech, MedTech, Semiconductors, Clean Energy (each with sector-specific thresholds)
- **Data**: Point-in-time fundamentals from SQLite — no look-ahead bias
- **Reporting lag**: 90 days (fundamentals available 90 days after fiscal quarter end)
- **Parameters**: Loaded at runtime from `config/strategy_params.json`
