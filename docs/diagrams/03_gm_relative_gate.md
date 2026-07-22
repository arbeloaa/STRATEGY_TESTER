# 03 — GM-Relative Gate (Market-Connected Gross Margin)

This is the newest and most complex mechanism in the strategy: instead of
(or in addition to) comparing a stock's gross margin to a fixed top/mid
band hardcoded per sub-sector, G2 can compare it to the **live
cross-sectional distribution of gross margins for that stock's actual
SHARADAR industry**, computed monthly from the feature cache. The diagram
below traces the full path in two parts: (A) how the percentile table is
built offline by `data_pipeline/build_feature_cache.py`, and (B) how
`gm_gate()` in `engine/tester.py` consumes it at gate-scoring time,
including the fallback to the fixed top/mid thresholds when no percentile
row exists.

Current live config (`config/strategy_params.json` → `gates.gm_relative`):
`enabled = true`, `percentile = 50` (median of the industry — a stock
must beat the p50 gross margin of its own SHARADAR industry that month to
get relative credit).

**Important nuance confirmed in code**: the percentile lookup key is the
stock's *raw SHARADAR industry string* (`SharadarIndustry`, e.g. loaded
from the `_sharadar_tickers` table), **not** the strategy's internal
`Sector` bucket label (e.g. "Cybersecurity") used for universe routing in
diagram 01. These are two different taxonomies that happen to correlate
loosely — see the self-check notes in the README for why this matters.

```mermaid
flowchart TD
    subgraph BUILD["(A) Offline build — data_pipeline/build_feature_cache.py, run per month_start"]
        direction TB
        B0([For each month_start in ~2020-01 .. 2026-06]) --> B1["For each ticker in all_tickers:\ncompute metrics from SF1 fundamentals\n+ SHARADAR/DAILY marketcap (_sf1_row_to_metrics)"]
        B1 --> B2["Look up ticker's raw SHARADAR industry\n(sf1_meta[ticker]['industry'])"]
        B2 --> B3["INSERT row into feature_cache table:\ndate, ticker, sharadar_industry, gm_pct,\nps_ratio, rule40, revenue_growth, ..."]
        B3 --> B4{"All tickers done\nfor this month_start?"}
        B4 -->|No| B1
        B4 -->|Yes| B5["compute_industry_percentiles(conn, date_str):\nGROUP BY sharadar_industry\nfor each of PERCENTILE_METRICS\n(ps_ratio, gm_pct, rule40, revenue_growth)"]
        B5 --> B6["Per industry x metric:\ncollect all non-null values that month\ncompute p25 / p40 / p50 / p60 / p75"]
        B6 --> B7[("INSERT OR REPLACE INTO industry_percentiles\n(date, industry, metric, n_tickers,\np25, p40, p50, p60, p75)\n-- date is always a month_start (YYYY-MM-01)")]
    end

    B7 --> LOAD["engine/tester.py startup: load_percentiles()\nreads entire industry_percentiles table into\nin-memory dict _PERCENTILES[(date, industry, metric)]\n= {25:.., 40:.., 50:.., 60:.., 75:..}\n(WARNs and leaves _PERCENTILES empty if table/DB missing)"]

    LOAD --> RUNTIME["(B) Runtime — gm_gate(gm, gm_erosion, top, mid, date, sector)\ncalled from gates_energy/tech/medtech/semi for every candidate"]

    RUNTIME --> SECTORARG["sector arg = row['SharadarIndustry'] or row['Sector']\n(portfolio_simulator.py sets SharadarIndustry from\n_TICKER_INDUSTRY, loaded from _sharadar_tickers table —\nfalls back to internal Sector bucket only if that lookup is empty)"]

    SECTORARG --> ENCHECK{"_P['gates']['gm_relative']['enabled']\n== true? (currently: true)"}
    ENCHECK -->|False| FIXED["Skip straight to fixed-threshold path"]
    ENCHECK -->|True| DATECHECK{"date and sector\nboth provided?"}
    DATECHECK -->|No| FIXED
    DATECHECK -->|Yes| PCTREAD["pct = gm_relative.percentile = 50\nmonth_start = date[:7] + '-01'\n(truncate the PIT date down to its month)"]

    PCTREAD --> LOOKUP{"_PERCENTILES.get((month_start,\nsector, 'gm_pct')) found,\nand pct(50) key present?"}
    LOOKUP -->|"No row for that\nindustry/month"| FALLBACKPRINT["print [FALLBACK] ... falling back\nto fixed thresholds (top=X%, mid=Y%)"]
    FALLBACKPRINT --> FIXED
    LOOKUP -->|Yes| RELCOMP["p_val = p_data[50]  (industry's p50 GM that month)\npassed = GM >= p_val"]
    RELCOMP --> RELRESULT["score = full weight if passed else 0\nnote: '[RELATIVE] GM=X% >= p50(industry)=Y% PASS/FAIL'\n-- NO erosion delta-bonus in this path\n(the +0.2 improving-erosion bonus only applies\nin the fixed-threshold branch below)"]
    RELRESULT --> DONE([Gate result: (passed, weight, note, proxy, score)])

    FIXED --> BAND["gm_band_score(gm, top, mid):\nGM >= top -> full weight, band='full'\nmid <= GM < top -> weight x0.7, band='mid'\nGM < mid -> 0, band='fail'\n(top/mid are the per-sub hardcoded/config\nvalues from diagram 02)"]
    BAND --> EROSIONBONUS{"GM erosion < 0%\n(improving) AND band score > 0?"}
    EROSIONBONUS -->|Yes| BONUS["bonus = min(0.2, weight - score)\nscore = score + bonus\nnote: '+bonus delta bonus (improving)'"]
    EROSIONBONUS -->|No| NOBONUS["no bonus; if score>0, note shows\n'(erosion=+X% no bonus)'"]
    BONUS & NOBONUS --> DONE
```
