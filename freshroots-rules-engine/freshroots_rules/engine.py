"""Freshroots replenishment rules engine.

Encodes the forecasting rules Raghav Kulkarni documented (about 80-85% of his
monthly recommendations) and routes everything else to him as an exception.

Order of operations, per SKU:
  1. Festival uplift (Oct, Nov, Mar: +25%) is applied to expected demand
     BEFORE any threshold check.
  2. The cover window is chosen: high-perishability SKUs use 3-4 weeks,
     all others use 6-14 weeks.
  3. Threshold checks: stockout risk (below min cover), overstock risk
     (above max cover), high demand (4-week avg > 12-week avg by 20%).
  4. An action is resolved and a quantity proposed.
  5. Guards send anything outside the documented rules to the exception
     queue: new launches, distributor drops, conflicts, missing data, and
     orders that would overshoot the cover window.

Nothing here places an order. Every output is a recommendation for Raghav.
"""
from __future__ import annotations

import calendar
import json
import math
from dataclasses import dataclass, fields
from pathlib import Path

import pandas as pd

# Actions
REORDER_NOW = "REORDER_NOW"            # stockout risk: reorder immediately
REORDER_HIGH_DEMAND = "REORDER_1.5X"   # demand spike: reorder at 1.5x standard
PAUSE = "PAUSE_NEXT_ORDER"             # overstock risk: skip the next order
NO_ACTION = "NO_ACTION"                # normal ordering schedule
NEEDS_REVIEW = "NEEDS_REVIEW"          # engine cannot recommend; Raghav decides

# Routes
APPROVE_QUEUE = "APPROVE_QUEUE"        # rule-covered; one-click approval
EXCEPTION_QUEUE = "EXCEPTION_QUEUE"    # needs Raghav's judgment

ACTION_PRIORITY = {REORDER_NOW: 0, REORDER_HIGH_DEMAND: 1, PAUSE: 2,
                   NEEDS_REVIEW: 3, NO_ACTION: 4}


@dataclass
class Config:
    # Rule 1: high demand
    short_window_weeks: int = 4
    long_window_weeks: int = 12
    high_demand_ratio: float = 1.20
    high_demand_multiplier: float = 1.5
    # Rules 2-3, 5: cover windows (weeks of average weekly sales)
    normal_min_weeks: float = 6
    normal_max_weeks: float = 14
    perishable_min_weeks: float = 3
    perishable_max_weeks: float = 4
    # Rule 4: festival uplift
    festival_months: tuple = (10, 11, 3)
    festival_uplift: float = 0.25
    festival_lookahead_weeks: int = 0
    festival_uplift_applies_to_high_demand_check: bool = False
    # Stock definition
    include_on_order_in_cover: bool = True
    # Exception guards (Raghav's judgment zone)
    min_history_weeks: int = 12
    distributor_drop_threshold: float = 0.30
    distributor_baseline_weeks: int = 8
    distributor_min_share: float = 0.05

    @classmethod
    def from_json(cls, path: str | Path) -> "Config":
        raw = json.loads(Path(path).read_text())
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        if "festival_months" in raw:
            raw["festival_months"] = tuple(raw["festival_months"])
        return cls(**raw)


def last_complete_weeks(run_date, n: int) -> pd.DatetimeIndex:
    """The n most recent complete Monday-start weeks before run_date, oldest first."""
    run = pd.Timestamp(run_date).normalize()
    this_monday = run - pd.Timedelta(days=run.weekday())
    last_complete = this_monday - pd.Timedelta(weeks=1)
    return pd.date_range(end=last_complete, periods=n, freq="7D")


def _to_monday(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s).dt.normalize()
    return s - pd.to_timedelta(s.dt.weekday, unit="D")


def _require(df: pd.DataFrame, cols: set, name: str) -> None:
    missing = cols - set(df.columns)
    if missing:
        raise ValueError(f"{name} is missing columns: {sorted(missing)}")


def _distributor_drops(window: pd.DataFrame, grid: pd.DatetimeIndex,
                       cfg: Config) -> dict[str, list[str]]:
    """Distributors whose last-week volume fell sharply vs their own baseline."""
    if window.empty:
        return {}
    last_week = grid[-1]
    base_weeks = grid[-1 - cfg.distributor_baseline_weeks:-1]
    sd = (window.groupby(["sku", "distributor_id", "week_start"])["units"].sum()
          .unstack(fill_value=0).reindex(columns=grid, fill_value=0))
    base = sd[base_weeks].mean(axis=1)
    last = sd[last_week]
    sku_total = base.groupby(level="sku").transform("sum")
    share = base / sku_total.where(sku_total > 0)
    drop = 1 - last / base.where(base > 0)
    mask = (drop >= cfg.distributor_drop_threshold) & (share >= cfg.distributor_min_share)
    out: dict[str, list[str]] = {}
    for (sku, dist), d in drop[mask].items():
        out.setdefault(sku, []).append(
            f"Distributor {dist} ({share[(sku, dist)]:.0%} of volume) "
            f"ordered {d:.0%} less last week than its {cfg.distributor_baseline_weeks}-week average"
        )
    return out


def _num(x, default=0.0) -> float:
    try:
        v = float(x)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def run_rules(sales: pd.DataFrame, stock: pd.DataFrame, skus: pd.DataFrame,
              run_date, cfg: Config | None = None) -> pd.DataFrame:
    """Apply Raghav's rules to every SKU and return one recommendation per SKU.

    sales: week_start, sku, distributor_id, units      (SAP export, weekly)
    stock: sku, on_hand, [on_order]                    (SAP stock + Zoho open POs)
    skus:  sku, sku_name, perishability, standard_order_qty, [launch_date]
    """
    cfg = cfg or Config()
    _require(sales, {"week_start", "sku", "distributor_id", "units"}, "sales")
    _require(stock, {"sku", "on_hand"}, "stock")
    _require(skus, {"sku", "sku_name", "perishability", "standard_order_qty"}, "sku master")

    run_date = pd.Timestamp(run_date).normalize()
    sales = sales.copy()
    sales["sku"] = sales["sku"].astype(str)
    sales["week_start"] = _to_monday(sales["week_start"])
    sales["units"] = pd.to_numeric(sales["units"], errors="coerce").fillna(0)

    n_grid = max(cfg.long_window_weeks, cfg.distributor_baseline_weeks + 1)
    grid = last_complete_weeks(run_date, n_grid)
    last_complete = grid[-1]
    sales = sales[sales["week_start"] <= last_complete]   # drop the in-progress week
    window = sales[sales["week_start"].isin(grid)]

    master = skus.assign(sku=skus["sku"].astype(str)).set_index("sku")
    stock = stock.assign(sku=stock["sku"].astype(str)).set_index("sku")
    for df, name in ((master, "sku master"), (stock, "stock")):
        dupes = df.index[df.index.duplicated()].unique().tolist()
        if dupes:
            raise ValueError(f"{name} has duplicate SKUs: {dupes[:10]}")
    all_skus = sorted(set(master.index) | set(stock.index) | set(window["sku"]))

    sku_week = (window.groupby(["sku", "week_start"])["units"].sum().unstack(fill_value=0)
                .reindex(index=all_skus, columns=grid, fill_value=0))
    avg_long = sku_week[grid[-cfg.long_window_weeks:]].mean(axis=1)
    avg_short = sku_week[grid[-cfg.short_window_weeks:]].mean(axis=1)

    first_sale = sales.groupby("sku")["week_start"].min()
    drops = _distributor_drops(window, grid, cfg)

    check_date = run_date + pd.Timedelta(weeks=cfg.festival_lookahead_weeks)
    festival = check_date.month in cfg.festival_months
    uplift = 1 + cfg.festival_uplift if festival else 1.0

    rows = []
    for sku in all_skus:
        exceptions: list[str] = []
        flags: list[str] = []
        m = master.loc[sku] if sku in master.index else None
        st = stock.loc[sku] if sku in stock.index else None
        if m is None:
            exceptions.append("Not in SKU master")
        if st is None:
            exceptions.append("No stock record")

        # History length: launch date if given, else first recorded sale
        start = None
        if m is not None and "launch_date" in master.columns and pd.notna(m.get("launch_date")):
            start = _to_monday(pd.Series([m["launch_date"]])).iloc[0]
        elif sku in first_sale.index:
            start = first_sale[sku]
        history = 0 if start is None or start > last_complete else (last_complete - start).days // 7 + 1
        if history < cfg.min_history_weeks:
            exceptions.append(f"Only {history} weeks of sales history (new launch; rules need "
                              f"{cfg.min_history_weeks})")

        a_long, a_short = float(avg_long[sku]), float(avg_short[sku])
        if 0 < history < cfg.long_window_weeks:
            # New launch: average only over weeks the SKU has existed, not pre-launch zeros
            row = sku_week.loc[sku]
            a_long = float(row.iloc[-history:].mean())
            a_short = float(row.iloc[-min(history, cfg.short_window_weeks):].mean())
        if a_long == 0 and history >= cfg.min_history_weeks:
            exceptions.append(f"No sales in the last {cfg.long_window_weeks} weeks; check if dormant")

        perishable = m is not None and str(m["perishability"]).strip().lower() == "high"
        min_w, max_w = ((cfg.perishable_min_weeks, cfg.perishable_max_weeks) if perishable
                        else (cfg.normal_min_weeks, cfg.normal_max_weeks))

        on_hand = _num(st["on_hand"]) if st is not None else 0.0
        on_order = _num(st.get("on_order", 0)) if st is not None else 0.0
        stock_for_cover = on_hand + (on_order if cfg.include_on_order_in_cover else 0)

        expected = a_long * uplift                     # Rule 4: uplift before thresholds
        cover = stock_for_cover / expected if expected > 0 else None

        short_for_check = a_short * (uplift if cfg.festival_uplift_applies_to_high_demand_check else 1)
        high = a_long > 0 and short_for_check > a_long * cfg.high_demand_ratio   # Rule 1
        stockout = cover is not None and cover < min_w                            # Rule 2 / 5
        overstock = cover is not None and cover > max_w                           # Rule 3 / 5
        if high: flags.append("HIGH_DEMAND")
        if stockout: flags.append("STOCKOUT_RISK")
        if overstock: flags.append("OVERSTOCK_RISK")
        if festival: flags.append("FESTIVAL_UPLIFT")
        if perishable: flags.append("PERISHABLE")

        std = _num(m["standard_order_qty"], default=float("nan")) if m is not None else float("nan")
        qty = 0.0
        if overstock and high:
            action = PAUSE
            exceptions.append("Conflict: demand is up but stock is already above max cover")
        elif stockout:
            action = REORDER_NOW
            qty = std * (cfg.high_demand_multiplier if high else 1)
        elif overstock:
            action = PAUSE
        elif high:
            action = REORDER_HIGH_DEMAND
            qty = std * cfg.high_demand_multiplier
        else:
            action = NO_ACTION

        if action in (REORDER_NOW, REORDER_HIGH_DEMAND):
            if math.isnan(qty):
                exceptions.append("No standard order quantity in SKU master")
                qty = 0.0
            elif expected > 0:
                projected = (stock_for_cover + qty) / expected
                if projected > max_w:
                    exceptions.append(f"Order would take cover to {projected:.1f} weeks, "
                                      f"above the {max_w:g}-week max")

        exceptions.extend(drops.get(sku, []))

        if cover is None and action == NO_ACTION and exceptions:
            action = NEEDS_REVIEW
        route = EXCEPTION_QUEUE if exceptions else APPROVE_QUEUE
        change = (a_short / a_long - 1) if a_long > 0 else None

        rows.append({
            "run_date": run_date.date().isoformat(),
            "sku": sku,
            "sku_name": m["sku_name"] if m is not None else "",
            "route": route,
            "action": action,
            "recommended_qty": int(math.ceil(qty)) if qty else 0,
            "on_hand": on_hand,
            "on_order": on_order,
            "avg_weekly_4w": round(a_short, 1),
            "avg_weekly_12w": round(a_long, 1),
            "demand_change_pct": round(change * 100, 1) if change is not None else None,
            "festival_uplift_pct": round((uplift - 1) * 100),
            "expected_weekly_demand": round(expected, 1),
            "weeks_of_cover": round(cover, 1) if cover is not None else None,
            "cover_window": f"{min_w:g}-{max_w:g}",
            "history_weeks": history,
            "flags": ", ".join(flags),
            "exceptions": " | ".join(exceptions),
            "reason": _reason(action, qty, cover, min_w, max_w, a_short, a_long, change,
                              festival, check_date, cfg, exceptions),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["_r"] = (out["route"] == APPROVE_QUEUE).astype(int)
    out["_a"] = out["action"].map(ACTION_PRIORITY)
    out["_c"] = out["weeks_of_cover"].fillna(-1)
    return out.sort_values(["_r", "_a", "_c"]).drop(columns=["_r", "_a", "_c"]).reset_index(drop=True)


def _reason(action, qty, cover, min_w, max_w, a_short, a_long, change, festival,
            check_date, cfg, exceptions) -> str:
    """Plain-language explanation for Raghav and procurement."""
    parts = []
    if cover is not None:
        parts.append(f"Stock covers {cover:.1f} weeks against a {min_w:g}-{max_w:g} week window.")
    if change is not None:
        parts.append(f"Last {cfg.short_window_weeks} weeks averaged {a_short:,.0f} units/week vs "
                     f"{a_long:,.0f} over {cfg.long_window_weeks} weeks ({change:+.0%}).")
    if festival:
        parts.append(f"{calendar.month_name[check_date.month]} festival uplift of "
                     f"{cfg.festival_uplift:.0%} applied.")
    parts.append({
        REORDER_NOW: f"Reorder now: {math.ceil(qty):,} units.",
        REORDER_HIGH_DEMAND: f"Demand is up; reorder at {cfg.high_demand_multiplier:g}x "
                             f"standard: {math.ceil(qty):,} units.",
        PAUSE: "Pause the next order.",
        NO_ACTION: "No change to the normal order.",
        NEEDS_REVIEW: "Engine cannot recommend; needs review.",
    }[action])
    if exceptions:
        parts.append("Review: " + "; ".join(exceptions) + ".")
    return " ".join(parts)
