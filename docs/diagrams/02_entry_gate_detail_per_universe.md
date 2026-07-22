# 02 — Entry Gate Detail Per Universe

This expands each of the four universes (`gates_energy`, `gates_tech`,
`gates_medtech`, `gates_semi` in `engine/tester.py`) into its real,
sub-sector-specific G1–G8 logic. Every branch below is copied from the
current code — nothing is generalized. Values in **bold** come from
`config/strategy_params.json` (tunable by the optimizer); plain values are
hardcoded directly in `tester.py` and are **not** reachable by the
optimizer (confirmed against `optimizers/trade_optimizer.py`'s own comment:
*"tester.py contains sector-specific hardcoded thresholds that are NOT
tunable through strategy_params.json"*).

All four universes share the same dynamic P/S adjustment for any gate built
with `gate_valuation()`: the base P/S threshold is multiplied by 2.0x if
revenue growth > 80%, 1.5x if > 40%, 1.2x if > 20%, else unchanged
(`dynamic_g1_threshold`).

---

## Energy (solar_hw / solar_install / renewables)

```mermaid
flowchart TD
    E0([Energy candidate: sub = solar_hw | solar_install | renewables]) --> EG1{G1 Valuation}
    EG1 -->|solar_hw| EG1A["PS/Growth < dyn(5.0) PASS\n(NA if PS/Growth unavailable)"]
    EG1 -->|solar_install| EG1B["PS/Growth < dyn(2.0) PASS"]
    EG1 -->|renewables| EG1C["PS_Ratio < 3.0 PASS (fixed, no dyn adj)\nNA if PS_Ratio unavailable/999"]

    EG1A & EG1B & EG1C --> EG2{"G2 Gross Margin\n(gm_relative overrides if enabled — see diagram 03)"}
    EG2 -->|solar_hw| EG2A["fixed fallback: top=**50%** mid=**22%**\nfull wt if GM>=top, x0.7 if mid<=GM<top"]
    EG2 -->|solar_install| EG2B["fixed fallback: top=**26%** mid=**12%**"]
    EG2 -->|renewables| EG2C["fixed fallback: top=**28%** mid=**18%**"]
    EG2A & EG2B & EG2C --> EG2N["+0.2 delta bonus if GM erosion improving (<0%)\nand base score > 0"]

    EG2N --> EG3{G3 Capital Efficiency}
    EG3 -->|solar_hw| EG3A["ROIC > -5% PASS"]
    EG3 -->|solar_install| EG3B["ROIC > 0% OR FCF_Margin > 0% PASS"]
    EG3 -->|renewables| EG3C["ROIC > -4% OR FCF_Margin > 3% PASS"]

    EG3A & EG3B & EG3C --> EG4{G4 Inventory / Ops}
    EG4 -->|solar_hw| EG4A["Inv Days < 120 OR Inv Trend < 5 PASS"]
    EG4 -->|solar_install| EG4B["Inv Trend <= 10 PASS (ops trend)"]
    EG4 -->|renewables| EG4C["Rule40 > -20 PASS (ops efficiency proxy)"]

    EG4A & EG4B & EG4C --> EG5["G5 Growth Signal (all subs)\nRevenue growth > -12% PASS"]

    EG5 --> EG6["G6 Leverage (all subs)\nShare growth < 10% PASS"]

    EG6 --> EG7{"G7 Margin Stability\n(gm_floor = sub top x mult; erosion combine logic differs per sub)"}
    EG7 -->|solar_hw AND| EG7A["floor = 35 x 0.4 = 14%\nerosion NA: (GM>=floor) AND (ROIC>0 or FCF>0)\nerosion known: (erosion<6%) AND (GM>=14%)"]
    EG7 -->|"solar_install AND(strict4)"| EG7B["floor = 20 x 0.5 = 10%\nerosion NA: (GM>=floor) AND (ROIC>0 or FCF>0)\nerosion known: (erosion<4%) AND (GM>=10%)"]
    EG7 -->|"renewables OR(guarded)"| EG7C["floor = 28 x 0.5 = 14%\nerosion NA: (GM>=floor) AND (ROIC>0 or FCF>0)\nerosion<6%: GM>=14% alone suffices\nerosion>=6%: automatic FAIL"]

    EG7A & EG7B & EG7C --> EG8{"G8 Momentum — gate_momentum(), wt=**1.5**"}
    EG8 --> EG8BEAR{"Regime = BEAR_VOLATILE/BEAR_GRIND\nAND below MA200?"}
    EG8BEAR -->|Yes| EG8ZERO(["0 pts — no partial credit in bear"])
    EG8BEAR -->|No| EG8GREEN{"sub in (solar_hw, solar_install)\nAND below MA200\nAND 6M return < 16%?"}
    EG8GREEN -->|Yes| EG8VETO(["0 pts — VETO: green-energy\nbelow-MA200 needs 6M>=16%"])
    EG8GREEN -->|No| EG8LADDER["Above MA200: 1.0 pt\nAbove MA100 only: 0.75 (RS>=70) / 0.6\nBelow MAs, 6M>=12.5% & RS>=65: 0.43\nBelow MAs, 6M>=12.5%: 0.33\nElse: 0"]

    EG8ZERO & EG8VETO & EG8LADDER --> ESUM(["Weighted sum -> compare vs\npass_threshold('energy') = 5.5 + regime_adj\n(see diagram 01)"])
```

---

## Tech (cyber / infra_saas / fintech)

```mermaid
flowchart TD
    T0([Tech candidate: sub = cyber | infra_saas | fintech]) --> TG1{"G1 Valuation\nSales-PEG = PS/Growth"}
    TG1 -->|cyber| TG1A["PS/Growth < dyn(**2.5**) PASS"]
    TG1 -->|infra_saas| TG1B["PS/Growth < dyn(**2.0**) PASS"]
    TG1 -->|fintech| TG1C["PS/Growth < dyn(**1.0**) PASS"]

    TG1A & TG1B & TG1C --> TG2{"G2 Gross Margin\n(gm_relative overrides if enabled — diagram 03)"}
    TG2 -->|cyber| TG2A["fixed fallback: top=**75%** mid=**65%**"]
    TG2 -->|infra_saas| TG2B["fixed fallback: top=**71%** mid=**59%**"]
    TG2 -->|fintech| TG2C["fixed fallback: top=**50%** mid=**38%**"]

    TG2A & TG2B & TG2C --> TG3{"G3 Rule of 40\n(V30: no bear-regime adjustment)"}
    TG3 -->|cyber| TG3A["Rule40 > 33 PASS"]
    TG3 -->|infra_saas| TG3B["Rule40 > 28 PASS"]
    TG3 -->|fintech| TG3C["Rule40 > 20 PASS"]

    TG3A & TG3B & TG3C --> TG4["G4 Retention (NRR proxy, all subs, weight 0.9)\nHARD FAIL if FCF_Margin <= -15%\nelif pricing=Strong or FCF_Margin>8%: PASS (0.9)\nelse: FAIL (0)"]

    TG4 --> TG5{G5 Op. Leverage}
    TG5 --> TG5HK{"HARD KILL:\nFCF_Margin<-10% AND Rev<5%?"}
    TG5HK -->|Yes| TG5FAIL(["FAIL"])
    TG5HK -->|No, fintech| TG5A["pricing=Strong OR Rev>15% OR FCF_Margin>5% PASS"]
    TG5HK -->|"No, cyber/infra_saas"| TG5B["FCF_Margin>5% OR (Rev>15% AND FCF_Margin>-5%) PASS"]

    TG5A & TG5B --> TG6{G6 Dilution}
    TG6 -->|cyber| TG6A["Share growth < 10% PASS"]
    TG6 -->|infra_saas| TG6B["Share growth < 8% PASS"]
    TG6 -->|fintech| TG6C["Share growth < 5% PASS"]

    TG6A & TG6B & TG6C --> TG7{G7 Platform Power}
    TG7 -->|fintech| TG7A["(Rev>5% AND GM>40%) OR (GM>50% AND Rev>-10%)\nOR pricing=Strong OR (GM>55% AND Rev>5%) PASS"]
    TG7 -->|"cyber/infra_saas"| TG7B["(Rev>5% AND (GM>50% OR pricing=Strong))\nOR (GM>68% AND Rev>15%) [V30 tightened\nhigh-GM escape, was GM>65+Rev>5] PASS"]

    TG7A & TG7B --> TG8{"G8 Momentum, wt=**0.8** (W_G8_TECH)"}
    TG8 --> TG8ABS{"Absolute override:\nGM>=65% AND R40>=30 AND Rev>=10%\nAND -15% <= MA200 < 0% AND MA100>=-15%?"}
    TG8ABS -->|Yes| TG8OV(["override = 1.0 x 0.8 x 0.5 = 0.4 pt\n'Fundamentals override' (temporarily weak price)"])
    TG8ABS -->|No| TG8BEAR{"Regime=BEAR_VOLATILE/BEAR_GRIND\nAND below MA200?"}
    TG8BEAR -->|Yes| TG8ZERO(["0 pts — no partial credit in bear"])
    TG8BEAR -->|No| TG8LADDER["Above MA200: 1.0\nAbove MA100: 0.75 (RS>=70) / 0.6\n6M return>0: 0.33\nelse 0\nCAP: if MA200<-15% and raw>0.4 -> raw=0.4"]
    TG8LADDER --> TG8RESCUE{"raw==0 AND regime in\n(BULL_WEAK, BEAR_GRIND) AND\nG2>=0.9 & G3>=0.9(direct pass) & G5 pass?"}
    TG8RESCUE -->|Yes| TG8RES(["raw = 0.5 'FUNDAMENTALS RESCUE'"])
    TG8RESCUE -->|No| TG8FINAL(["raw as computed"])

    TG8OV & TG8ZERO & TG8RES & TG8FINAL --> TSUM["Weighted sum ->\npass_threshold('tech') = 6.3 + regime_adj"]
    TSUM --> TPOST["Then: tech_quality_kill, tech_val_or_quality_check,\ntech_expensive_check, tech_strong_arm_check,\ntech_ma200_check, TECH_REV_FLOOR(-5%)\n(see diagram 01 TECHBLOCKS subgraph)"]
```

---

## MedTech (surgical / monitoring / implants)

```mermaid
flowchart TD
    M0([MedTech candidate: sub = surgical | monitoring | implants]) --> MG1{"G1 Valuation\nPS/Growth"}
    MG1 -->|surgical| MG1A["PS/Growth < dyn(2.5) PASS"]
    MG1 -->|monitoring| MG1B["PS/Growth < dyn(2.0) PASS"]
    MG1 -->|implants| MG1C["PS/Growth < dyn(2.0) PASS"]

    MG1A & MG1B & MG1C --> MG2{"G2 Gross Margin (hardcoded, not in strategy_params.json)\n(gm_relative can still override — diagram 03)"}
    MG2 -->|surgical| MG2A["top=65% mid=50%"]
    MG2 -->|monitoring| MG2B["top=70% mid=55%"]
    MG2 -->|implants| MG2C["top=55% mid=45%\n(was 60/45 — pilot floor raise)"]

    MG2A & MG2B & MG2C --> MG3{G3 ROIC}
    MG3 -->|surgical| MG3A["ROIC > 12% PASS"]
    MG3 -->|monitoring| MG3B["ROIC > 10% PASS"]
    MG3 -->|implants| MG3C["ROIC > 10% PASS"]

    MG3A & MG3B & MG3C --> MG4{"G4 Innovation (R&D proxy)"}
    MG4 -->|surgical| MG4A["Rule40 > 10 OR ROIC > 20 (bypass) PASS"]
    MG4 -->|monitoring| MG4B["Rule40 > 15 OR ROIC > 20 (bypass) PASS"]
    MG4 -->|implants| MG4C["Rule40 > 5 OR ROIC > 20 (bypass) PASS"]

    MG4A & MG4B & MG4C --> MG5["G5 Recurring Revenue (all subs)\nHard fail if >=2 of: Rev<=-20%, ROIC<=0%, FCF<=0%\nelse: Rev>2% OR (ROIC>15% AND Rev>-5%) PASS"]

    MG5 --> MG6{"G6 Regulatory Risk (proxy)"}
    MG6 -->|surgical| MG6A["Share growth < 5% PASS"]
    MG6 -->|monitoring| MG6B["Share growth < 8% PASS"]
    MG6 -->|implants| MG6C["Share growth < 5% PASS"]

    MG6A & MG6B & MG6C --> MG7{"G7 Market Moat (proxy)\ninv_thr = 25% (implants) else 15%"}
    MG7 --> MG7NA{"GM Erosion = N/A?"}
    MG7NA -->|Yes| MG7A["ok = (Inv Trend < inv_thr) AND ROIC > 15%"]
    MG7NA -->|No| MG7B["moat_quality = pricing=Strong OR erosion<2%\nmoat_ops = Inv Trend < inv_thr\nok = moat_quality AND moat_ops"]

    MG7A & MG7B --> MG8{"G8 Momentum — gate_momentum(), wt=**1.5**\n(same generic function as energy)"}
    MG8 --> MG8BEAR{"Regime=BEAR_VOLATILE/BEAR_GRIND\nAND below MA200?"}
    MG8BEAR -->|Yes| MG8ZERO(["0 pts — no partial credit in bear"])
    MG8BEAR -->|No| MG8LADDER["Above MA200: 1.0\nAbove MA100: 0.75(RS>=70)/0.6\nBelow MAs, 6M>=12.5% & RS>=65: 0.43\nBelow MAs, 6M>=12.5%: 0.33\nElse: 0\n(green-energy subsector veto never applies —\nmedtech subsectors are not solar_hw/solar_install)"]

    MG8ZERO & MG8LADDER --> MSUM["Weighted sum ->\npass_threshold('medtech') = 5.7 + regime_adj"]
    MSUM --> MPOST["Post-pass veto: if BULL_WEAK and G8 score==0 -> VETO\n(V27, medtech-specific)"]
```

---

## Semiconductors (proc_ai / connectivity / foundry_analog / memory_smallcap)

```mermaid
flowchart TD
    S0([Semi candidate: sub = proc_ai | connectivity | foundry_analog | memory_smallcap]) --> SG1{G1 Valuation}
    SG1 -->|proc_ai| SG1A["PS/Growth < dyn(2.2) PASS"]
    SG1 -->|connectivity| SG1B["PS/Growth < dyn(2.2) PASS"]
    SG1 -->|foundry_analog| SG1C["PS_Ratio < 5.0 PASS (fixed, uses raw P/S not PS/Growth)"]
    SG1 -->|memory_smallcap| SG1D["PS/Growth < dyn(1.5) PASS [P] proxy (P/B proxy), weight x0.9"]

    SG1A & SG1B & SG1C & SG1D --> SG2{"G2 Gross Margin (hardcoded, not in strategy_params.json)"}
    SG2 -->|proc_ai| SG2A["top=60% mid=45%"]
    SG2 -->|connectivity| SG2B["top=50% mid=35%"]
    SG2 -->|foundry_analog| SG2C["top=45% mid=30%"]
    SG2 -->|memory_smallcap| SG2D["top=30% mid=15%"]

    SG2A & SG2B & SG2C & SG2D --> SG3{G3 Inventory}
    SG3 -->|proc_ai| SG3A["Inv Trend <= 10 OR Inv Days < 130 PASS"]
    SG3 -->|connectivity| SG3B["Inv Trend <= 15 OR Inv Days < 150 PASS"]
    SG3 -->|foundry_analog| SG3C["Inv Days < 250 OR Inv Trend < 0 PASS"]
    SG3 -->|memory_smallcap| SG3D["Inv Days < 160 OR Inv Trend < 0 PASS"]

    SG3A & SG3B & SG3C & SG3D --> SG4{"G4 FCF Conversion (proxy, all subs)"}
    SG4 -->|proc_ai| SG4A["ROIC > 3% PASS"]
    SG4 -->|connectivity| SG4B["ROIC > 5% PASS"]
    SG4 -->|foundry_analog| SG4C["ROIC > 7% PASS"]
    SG4 -->|memory_smallcap| SG4D["ROIC > -10% AND GM erosion < 10% PASS"]

    SG4A & SG4B & SG4C & SG4D --> SG5{G5 R&D Efficiency}
    SG5 -->|"proc_ai [P]"| SG5A["Rev>5% OR pricing=Strong PASS"]
    SG5 -->|"connectivity [P]"| SG5B["Rev>0% OR pricing=Strong OR ROIC>8% PASS"]
    SG5 -->|"foundry_analog (direct)"| SG5C["Rev>-15% OR pricing=Strong OR ROIC>8% PASS"]
    SG5 -->|"memory_smallcap [P]"| SG5D["pricing=Strong OR Rev>0% OR ROIC>5% PASS ('design wins')"]

    SG5A & SG5B & SG5C & SG5D --> SG6{G6 Dilution}
    SG6 -->|proc_ai| SG6A["Share growth < 5% PASS"]
    SG6 -->|connectivity| SG6B["Share growth < 5% PASS"]
    SG6 -->|"foundry_analog [P]"| SG6C["Share growth < 3% PASS"]
    SG6 -->|"memory_smallcap [P]"| SG6D["Share growth < 15% PASS"]

    SG6A & SG6B & SG6C & SG6D --> SG7{G7 Moat/Pricing}
    SG7 -->|proc_ai| SG7A["pricing=Strong PASS (ASP trend only)"]
    SG7 -->|connectivity| SG7B["pricing=Strong OR Rev>5% OR GM>46% PASS"]
    SG7 -->|"foundry_analog [P]"| SG7C["Rev>5% OR pricing=Strong OR GM>55% PASS"]
    SG7 -->|"memory_smallcap [P]"| SG7D["pricing=Strong OR Rev>5% OR ROIC>8% PASS ('IP proxy')"]

    SG7A & SG7B & SG7C & SG7D --> SG8{"G8 Momentum, wt=**1.5**"}
    SG8 -->|proc_ai| SG8A["RS>=60 & above MA200: 1.0\nabove MA200 only: 0.7\nRS>=60 only: 0.5\n6M>0: 0.33\nelse 0"]
    SG8 -->|connectivity| SG8B["identical ladder to proc_ai\n(RS>=60 & MA200: 1.0 / 0.7 / 0.5 / 0.33 / 0)"]
    SG8 -->|foundry_analog| SG8C["uses generic gate_momentum()\n(same bear-block + MA100/MA200/6M ladder\nas energy/medtech — no green-energy veto,\nweight 1.5)"]
    SG8 -->|memory_smallcap| SG8D["6M return >= 11%: 1.0\n6M return > 0: 0.5\nelse: 0 (no RS/MA component)"]

    SG8A & SG8B & SG8C & SG8D --> SSUM["Weighted sum ->\npass_threshold('semi') = 5.0 flat\n(semi is COUNTER_CYCLICAL — no regime adjustment)"]
    SSUM --> SPOST["Post-pass veto: Rev<-30% AND R40<0 -> VETO (V28)"]
```
