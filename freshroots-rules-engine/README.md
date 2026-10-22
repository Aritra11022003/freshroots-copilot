# Freshroots Replenishment Rules Engine

Encodes Raghav Kulkarni's documented demand-planning rules. Every SKU gets one recommendation, routed to one of two queues:

- **APPROVE_QUEUE**: rule-covered. Raghav approves with one click, and it becomes a draft PO.
- **EXCEPTION_QUEUE**: needs his judgment. This covers new launches, distributor drops, conflicts, missing data, and orders that would overshoot the cover window.

The engine never places an order.

## Run it

```bash
pip install pandas pytest
python generate_sample_data.py              # synthetic data; replace with real exports
python run.py --data data --run-date 2026-10-22 --out output
python -m pytest -q                         # 16 tests, one per rule and guard
```

The run writes two files to `output/`:

- `recommendations_<date>.csv`: all SKUs, exceptions first.
- `exceptions_<date>.csv`: Raghav's review list.

## Input files (in `--data`)

| File | Source | Columns |
| --- | --- | --- |
| `sales.csv` | SAP, weekly by distributor | `week_start, sku, distributor_id, units` |
| `stock.csv` | SAP stock + Zoho open POs | `sku, on_hand, on_order` |
| `sku_master.csv` | SAP / Raghav | `sku, sku_name, perishability (high/normal), standard_order_qty, launch_date (optional)` |

Weeks start on Monday. The current, incomplete week is ignored.

## Rule order (per SKU)

1. **Festival uplift.** In October, November and March, expected weekly demand is multiplied by 1.25 before any check.
2. **Cover window.** High-perishability SKUs use 3–4 weeks. All other SKUs use 6–14 weeks.
3. **Checks:**
   - Cover below the minimum means stockout risk, so reorder now.
   - Cover above the maximum means overstock risk, so pause the next order.
   - A 4-week average more than 20% above the 12-week average means high demand, so reorder at 1.5x the standard quantity.
4. **Combinations:**
   - Stockout plus high demand: reorder now at 1.5x.
   - Overstock plus high demand: conflict, sent to Raghav.
5. **Guards (send to the exception queue):**
   - Under 12 weeks of history.
   - No sales in 12 weeks.
   - A distributor holding 5% or more of the SKU's volume dropped 30% or more last week.
   - The proposed order would push cover above the maximum.
   - Missing master or stock data.

All thresholds live in `config.json`.

## Assumptions to confirm with Raghav

These are the gaps in the written rules. Each one is a config switch, not a code change.

1. **Does "current stock" include stock on order?** The default is yes (`include_on_order_in_cover`). Without it, a twice-weekly run would reorder the same SKU again before the last order arrives.
2. **What does the festival uplift apply to?** The default applies it to expected demand only. If it also applied to the high-demand check, almost every SKU would be flagged 1.5x in festival months (`festival_uplift_applies_to_high_demand_check`).
3. **When does the festival start counting?** The default is the run date's month. If supplier lead time is about 2 weeks, a late-September run should already uplift for October (`festival_lookahead_weeks`).
4. **What is the "standard quantity"?** It is read from `sku_master.csv` and must be filled in for each SKU.
5. **Which SKUs are high-perishability?** This is set by the `perishability` column.
6. **What counts as a distributor drop?** The default is a 30% or larger fall, from a distributor with at least 5% of the SKU's volume, against its own 8-week average. Raghav to tune.

## Next steps

- **Backtest (the week-3 gate).** Replay the last 12 months week by week. Compare the engine's actions to Raghav's actual decisions. The target is at least 80% agreement on rule-covered SKUs.
- **Zoho export.** Convert the approved queue into a draft-PO import file.
- **Override log.** Record Raghav's decisions with a reason code, so the 15–20% he handles by judgment gets documented over time.
- **LLM layer.** Rewrite the `reason` column into short notes for procurement.
