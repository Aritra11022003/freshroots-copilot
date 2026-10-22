"""[C5] Rules engine: Raghav's 5 rules, applied in order to every SKU.

Adapted from freshroots-rules-engine/ to the real columns (SKU-level weekly
sales, calendar-driven festival months, no launch date, no on-order feed).

Per SKU:
  1. Festival uplift: if the calendar marks the run month as festival,
     expected weekly demand = 12-week avg x 1.25, BEFORE any check.
  2. Cover window: high-perishability 3-4 weeks, all others 6-14 weeks.
  3. Checks: cover = stock / expected weekly demand.
       below min  -> stockout risk  -> REORDER_NOW at standard qty
       above max  -> overstock risk -> PAUSE_NEXT_ORDER
       4w avg > 12w avg by 20%      -> high demand -> REORDER_1.5X
  4. Combinations: stockout + high demand -> REORDER_NOW at 1.5x;
     overstock + high demand -> conflict, exception queue; else NO_ACTION.
  5. Guards -> EXCEPTION_QUEUE (Raghav's judgment): under 12 weeks of history,
     no sales in 12 weeks, distributor drop (only with distributor-level sales),
     order pushes cover above max, SKU defaulted under --allow-defaults.

Pure pandas: no database, no I/O. Nothing here places an order.
"""
from __future__ import annotations

import calendar as _cal
import math
from datetime import timedelta

import pandas as pd

from .config import Config
from .weeks import last_complete_week, week_label, week_monday, week_range, year_month

REORDER_NOW = "REORDER_NOW"
REORDER_HIGH_DEMAND = "REORDER_1.5X"
PAUSE = "PAUSE_NEXT_ORDER"
NO_ACTION = "NO_ACTION"
NEEDS_REVIEW = "NEEDS_REVIEW"          # no demand signal: the engine cannot recommend
ACTIONS = [REORDER_NOW, REORDER_HIGH_DEMAND, PAUSE, NO_ACTION, NEEDS_REVIEW]
REORDER_ACTIONS = {REORDER_NOW, REORDER_HIGH_DEMAND}

APPROVE_QUEUE = "APPROVE_QUEUE"
EXCEPTION_QUEUE = "EXCEPTION_QUEUE"

_PRIORITY = {REORDER_NOW: 0, REORDER_HIGH_DEMAND: 1, PAUSE: 2, NEEDS_REVIEW: 3, NO_ACTION: 4}


def is_festival(calendar: pd.DataFrame, check_date) -> bool:
    row = calendar.loc[calendar["year_month"].astype(str) == year_month(check_date), "is_festival_month"]
    return bool(row.iloc[0]) if len(row) else False


def standard_qty_default(avg_12w: float, cfg: Config) -> int | None:
    """Labelled default (needs sign-off): 4 x 12-week average, rounded up to the nearest 10."""
    if not avg_12w or avg_12w <= 0:
        return None
    step = cfg.standard_qty_default_round_to
    return int(math.ceil(cfg.standard_qty_default_weeks * avg_12w / step) * step)


def sales_end_week(sales: pd.DataFrame, run_date):
    """Latest complete week that has sales data (windows end here)."""
    cutoff = last_complete_week(run_date)
    weeks = [w for w in sales["week"].map(week_monday) if w is not None and w <= cutoff]
    return max(weeks) if weeks else None


def sku_averages(sales: pd.DataFrame, end, cfg: Config) -> pd.DataFrame:
    """Per SKU: avg_4w, avg_12w, history_weeks, first_sale, sold_12w.

    History starts at the first week with sales > 0 (launch proxy: no launch date in the
    data). Averages use only weeks since then; pre-launch zeros are never averaged in."""
    s = sales.assign(_m=sales["week"].map(week_monday),
                     _u=pd.to_numeric(sales["units_sold"], errors="coerce").fillna(0))
    s = s[s["_m"].notna() & (s["_m"] <= end)]
    long_n, short_n = cfg.long_window_weeks, cfg.short_window_weeks
    out = {}
    for sku, g in s.groupby("sku_id"):
        by_week = g.groupby("_m")["_u"].sum()
        sold = by_week[by_week > 0]
        if sold.empty:
            out[sku] = dict(avg_4w=0.0, avg_12w=0.0, history_weeks=0, first_sale=None, sold_12w=0.0)
            continue
        first = sold.index.min()
        grid = week_range(first, end)
        series = by_week.reindex(grid, fill_value=0)
        last12_start = end - timedelta(weeks=long_n - 1)
        out[sku] = dict(
            avg_4w=float(series.iloc[-short_n:].mean()),
            avg_12w=float(series.iloc[-long_n:].mean()),
            history_weeks=len(grid),
            first_sale=first,
            sold_12w=float(by_week[by_week.index >= last12_start].sum()),
        )
    return pd.DataFrame.from_dict(out, orient="index")


def distributor_drops(dist_sales: pd.DataFrame, end, cfg: Config) -> dict[str, list[str]]:
    """Guard: a distributor with >= 5% of the SKU's volume dropped >= 30% last week vs its own
    8-week average. Needs sales by (sku_id, distributor_id, week, units_sold)."""
    d = dist_sales.assign(_m=dist_sales["week"].map(week_monday))
    base_weeks = [end - timedelta(weeks=i) for i in range(1, cfg.distributor_baseline_weeks + 1)]
    pivot = (d.groupby(["sku_id", "distributor_id", "_m"])["units_sold"].sum()
             .unstack(fill_value=0).reindex(columns=base_weeks + [end], fill_value=0))
    base = pivot[base_weeks].mean(axis=1)
    last = pivot[end]
    share = base / base.groupby(level="sku_id").transform("sum").where(lambda x: x > 0)
    drop = 1 - last / base.where(base > 0)
    mask = (drop >= cfg.distributor_drop_threshold) & (share >= cfg.distributor_min_share)
    out: dict[str, list[str]] = {}
    for (sku, dist), v in drop[mask].items():
        out.setdefault(sku, []).append(
            f"Distributor {dist} ({share[(sku, dist)]:.0%} of volume) sold {v:.0%} less last week "
            f"than its {cfg.distributor_baseline_weeks}-week average")
    return out


def run_rules(sales: pd.DataFrame, inventory: pd.DataFrame, skus: pd.DataFrame,
              calendar: pd.DataFrame, run_date, cfg: Config | None = None, *,
              defaults: dict[str, int] | None = None,
              distributor_sales: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list[str]]:
    """One recommendation per SKU in the SKU table, plus run notes.

    Inputs use the Supabase column names (skus.sku_id, weekly_sales.units_sold, ...).
    `inventory` may carry an optional `units_on_order` column; without it on_order = 0.
    `defaults` = {sku: standard qty} applied under --allow-defaults (each goes to exceptions).
    """
    cfg = cfg or Config()
    defaults = defaults or {}
    notes: list[str] = []

    end = sales_end_week(sales, run_date)
    if end is None:
        raise ValueError("No complete sales week before the run date")
    lag = (last_complete_week(run_date) - end).days // 7
    notes.append(f"Sales through {week_label(end)}" + (f" ({lag} week(s) behind the run date)" if lag else ""))

    avgs = sku_averages(sales, end, cfg)

    inv = inventory.copy()
    inv["units_on_hand"] = pd.to_numeric(inv["units_on_hand"], errors="coerce").fillna(0)
    has_on_order = "units_on_order" in inv.columns
    if has_on_order:
        inv["units_on_order"] = pd.to_numeric(inv["units_on_order"], errors="coerce").fillna(0)
    else:
        inv["units_on_order"] = 0
        notes.append("on_order = 0 for every SKU (DEFAULT: no on-order feed)")
    stock = inv.groupby("sku_id")[["units_on_hand", "units_on_order"]].sum()

    if distributor_sales is not None and not distributor_sales.empty:
        drops = distributor_drops(distributor_sales, end, cfg)
    else:
        drops = {}
        notes.append("distributor_drop guard skipped: sales are SKU-level, not distributor-level")

    # Rule 1 input: festival month from the calendar (run date + look-ahead)
    check_date = pd.Timestamp(run_date) + pd.Timedelta(weeks=cfg.festival_lookahead_weeks)
    festival = is_festival(calendar, check_date)
    uplift = 1 + cfg.festival_uplift if festival else 1.0
    month_name = _cal.month_name[check_date.month]
    if festival:
        notes.append(f"{month_name} is a festival month: +{cfg.festival_uplift:.0%} uplift applied")

    rows = []
    for _, m in skus.drop_duplicates("sku_id").iterrows():
        sku = m["sku_id"]
        a = avgs.loc[sku] if sku in avgs.index else None
        a4 = float(a["avg_4w"]) if a is not None else 0.0
        a12 = float(a["avg_12w"]) if a is not None else 0.0
        history = int(a["history_weeks"]) if a is not None else 0
        sold_12w = float(a["sold_12w"]) if a is not None else 0.0
        on_hand = float(stock.loc[sku, "units_on_hand"]) if sku in stock.index else 0.0
        on_order = float(stock.loc[sku, "units_on_order"]) if sku in stock.index else 0.0

        flags, exceptions = [], []

        # Rule 1: festival uplift on expected demand, before any check
        expected = a12 * uplift

        # Rule 2: cover window
        perishable = str(m.get("perishability", "")).strip() in cfg.high_perishability_values
        min_w, max_w = ((cfg.perishable_min_weeks, cfg.perishable_max_weeks) if perishable
                        else (cfg.normal_min_weeks, cfg.normal_max_weeks))

        # Rule 3: checks
        stock_for_cover = on_hand + (on_order if cfg.include_on_order_in_cover else 0)
        cover = stock_for_cover / expected if expected > 0 else None
        short_signal = a4 * (uplift if cfg.festival_uplift_applies_to_high_demand_check else 1)
        high = a12 > 0 and short_signal > a12 * cfg.high_demand_ratio
        stockout = cover is not None and cover < min_w
        overstock = cover is not None and cover > max_w
        for cond, name in ((stockout, "STOCKOUT_RISK"), (overstock, "OVERSTOCK_RISK"),
                           (high, "HIGH_DEMAND"), (festival, "FESTIVAL_UPLIFT"), (perishable, "HIGH_PERISHABILITY")):
            if cond:
                flags.append(name)

        std_raw = pd.to_numeric(pd.Series([m.get("standard_reorder_qty")]), errors="coerce").iloc[0]
        defaulted = sku in defaults
        std = float(defaults[sku]) if defaulted else (float(std_raw) if pd.notna(std_raw) and std_raw > 0 else None)

        # Rule 4: combinations
        qty = 0.0
        if cover is None:
            action = NEEDS_REVIEW
        elif overstock and high:
            action = PAUSE
            exceptions.append("Conflict: demand is up >20% but stock is already above max cover")
        elif stockout:
            action = REORDER_NOW
            qty = (std or 0) * (cfg.high_demand_multiplier if high else 1)
        elif overstock:
            action = PAUSE
        elif high:
            action = REORDER_HIGH_DEMAND
            qty = (std or 0) * cfg.high_demand_multiplier
        else:
            action = NO_ACTION

        # Rule 5: exception guards
        if 0 < history < cfg.min_history_weeks:
            exceptions.append(f"New SKU: {history} weeks of history since first sale "
                              f"(needs {cfg.min_history_weeks})")
        if sold_12w <= 0:
            exceptions.append(f"No sales in the last {cfg.long_window_weeks} weeks")
        exceptions.extend(drops.get(sku, []))
        if action in REORDER_ACTIONS:
            if std is None:
                exceptions.append("No standard reorder quantity")
            elif expected > 0:
                projected = (stock_for_cover + qty) / expected
                if projected > max_w:
                    exceptions.append(f"Order would push cover to {projected:.1f} weeks, above the "
                                      f"{max_w:g}-week max")
        if defaulted:
            exceptions.append(f"Standard qty DEFAULTED to {defaults[sku]:,} "
                              f"({cfg.standard_qty_default_weeks} x 12-week avg) under --allow-defaults")

        change = (a4 / a12 - 1) if a12 > 0 else None
        qty_int = int(math.ceil(qty)) if qty else 0
        rows.append({
            "sku": sku,
            "sku_name": m.get("sku_name"),
            "route": EXCEPTION_QUEUE if exceptions else APPROVE_QUEUE,
            "action": action,
            "recommended_qty": qty_int,
            "on_hand": int(on_hand),
            "on_order": int(on_order),
            "avg_4w": round(a4, 1),
            "avg_12w": round(a12, 1),
            "demand_change_pct": round(change * 100, 1) if change is not None else None,
            "festival_uplift_pct": round((uplift - 1) * 100),
            "expected_weekly": round(expected, 1),
            "weeks_of_cover": round(cover, 1) if cover is not None else None,
            "cover_window": f"{min_w:g}-{max_w:g}",
            "history_weeks": history,
            "flags": ", ".join(flags),
            "exceptions": " | ".join(exceptions),
            "reason": _reason(action, qty_int, cover, min_w, max_w, a4, a12, change, festival,
                              month_name, high, on_order, cfg, exceptions),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out, notes
    out["_r"] = (out["route"] == APPROVE_QUEUE).astype(int)
    out["_a"] = out["action"].map(_PRIORITY)
    out["_c"] = out["weeks_of_cover"].fillna(-1)
    out = out.sort_values(["_r", "_a", "_c", "sku"]).drop(columns=["_r", "_a", "_c"]).reset_index(drop=True)
    return out, notes


def _reason(action, qty, cover, min_w, max_w, a4, a12, change, festival, month_name, high,
            on_order, cfg, exceptions) -> str:
    """Plain-language reason built only from the numbers (no LLM in v1)."""
    parts = []
    if cover is not None:
        s = f"Stock covers {cover:.1f} weeks against a {min_w:g}–{max_w:g} week window"
        if on_order and cfg.include_on_order_in_cover:
            s += f" (incl. {on_order:,.0f} on order)"
        parts.append(s)
    if change is not None:
        parts.append(f"4-week avg {a4:,.0f}/wk is {change:+.0%} vs 12-week avg {a12:,.0f}/wk")
    if festival:
        parts.append(f"{month_name} uplift {cfg.festival_uplift:.0%} applied")
    parts.append({
        REORDER_NOW: (f"reorder now at {cfg.high_demand_multiplier:g}x standard: {qty:,} units" if high
                      else f"reorder now: {qty:,} units"),
        REORDER_HIGH_DEMAND: f"demand is up; reorder at {cfg.high_demand_multiplier:g}x standard: {qty:,} units",
        PAUSE: "pause the next order",
        NO_ACTION: "no change to the normal order",
        NEEDS_REVIEW: "no recent demand, so the engine cannot recommend",
    }[action])
    text = "; ".join(parts) + "."
    if exceptions:
        text += " Review: " + "; ".join(exceptions) + "."
    return text[0].upper() + text[1:]
