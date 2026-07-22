# Trading Strategy Diagrams

Mermaid flowchart diagrams documenting the current, real logic of the
strategy — built by reading `engine/tester.py`, `engine/portfolio_simulator.py`,
`engine/virtual_trader.py`, `config/strategy_params.json`,
`data_pipeline/build_feature_cache.py`, and `optimizers/trade_optimizer.py`
directly, not an idealized description. Each file renders standalone in
GitHub or the VS Code Markdown preview.

1. [01_entry_gates_overview.md](01_entry_gates_overview.md) — the full veto → G1–G8 → score → threshold pipeline, with universe routing and regime-adjusted pass thresholds.
2. [02_entry_gate_detail_per_universe.md](02_entry_gate_detail_per_universe.md) — every sub-sector's exact G1–G8 thresholds for energy, tech, medtech, and semi, four separate diagrams.
3. [03_gm_relative_gate.md](03_gm_relative_gate.md) — how the industry-percentile gross-margin gate is built from the feature cache and consumed at gate time, with its fallback path.
4. [04_exit_strategy.md](04_exit_strategy.md) — all eight exit checks in their real priority order, with current live thresholds and the MIN_HOLD_DAYS gate.
5. [05_position_sizing.md](05_position_sizing.md) — signal → dollar amount, including conviction/regime multipliers, exposure-cap clipping, and pyramiding.
6. [06_regime_detection.md](06_regime_detection.md) — how BULL_STRONG/BULL_WEAK/BEAR_GRIND/BEAR_VOLATILE is computed and every downstream effect it has.
7. [07_full_trade_lifecycle.md](07_full_trade_lifecycle.md) — the big-picture, one-day/one-ticker walk from universe scan to closed trade, referencing all the above.
8. [08_optimizer_loop.md](08_optimizer_loop.md) — the separate meta-system (`trade_optimizer.py`) that tunes `strategy_params.json` via a 3-agent loop with guardrails and walk-forward validation.

## Notable findings from reading the code (not idealizations)

- **`config/strategy_params.json`'s `regime_detection` block is dead
  config.** No code anywhere reads `ndx_ma_period`,
  `bear_volatile_threshold`, `bear_grind_threshold`, or
  `bull_weak_threshold`. `compute_ndx_regime()` in `portfolio_simulator.py`
  hardcodes its own 100-day-MA / 20%-vol classifier. See
  [06_regime_detection.md](06_regime_detection.md).
- **`engine/virtual_trader.py` is a separate, legacy paper-trading tool**,
  not wired into `strategy_params.json` or the optimizer loop
  (`trade_optimizer.py` only ever invokes `portfolio_simulator.py`). It has
  its own independently hardcoded stop-loss/trailing-stop/time-stop rules
  that do not match the exit mechanism in diagram 04. Read for this task,
  but not diagrammed as part of the live strategy since it is not part of
  the tuned/current pipeline.
- **GM gate thresholds are only partially tunable.** `gm_tops`/`gm_mids`
  (energy) and `gm_configs` (tech) live in `strategy_params.json`, but the
  medtech and semi GM top/mid bands are hardcoded directly in
  `gates_medtech()`/`gates_semi()` in `tester.py` and cannot be changed by
  the optimizer. Same is true for most of G3–G7's per-sub-sector
  thresholds across all four universes — see the "hardcoded" callouts in
  [02_entry_gate_detail_per_universe.md](02_entry_gate_detail_per_universe.md).
- **Universe routing and the gm_relative percentile lookup use two
  different taxonomies.** Universe/sub-sector assignment comes from the
  strategy's own `Sector` bucket labels (`SECTOR_MAP`); the gm_relative
  gate looks up percentiles by the ticker's raw SHARADAR industry string.
  See [03_gm_relative_gate.md](03_gm_relative_gate.md).
