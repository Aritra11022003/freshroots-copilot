"""[C3] Validation gate. Runs at the start of EVERY run.

Any gap blocks the run: the caller saves the gaps, marks the run 'blocked' and
writes no recommendations. A partial output is worse than none.

Each gap is {"sku", "issue", "fix_needed", "code"}. `code` is internal (used to
decide which gaps an approved default may clear); it is not stored.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .config import Config
from .weeks import last_complete_week, week_monday, year_month

REQUIRED_COLUMNS = {
    "skus": ["sku_id", "sku_name", "perishability", "standard_reorder_qty", "distributor_id"],
    "weekly_sales": ["sku_id", "week", "units_sold"],
    "inventory": ["sku_id", "location", "units_on_hand"],
    "calendar": ["year_month", "is_festival_month"],
    "distributors": ["distributor_id", "region"],
}

MISSING_STD_QTY = "MISSING_STD_QTY"


def _gap(sku, issue, fix, code="GENERIC"):
    return {"sku": sku, "issue": issue, "fix_needed": fix, "code": code}


def validate(tables: dict[str, pd.DataFrame], run_date, cfg: Config | None = None) -> list[dict]:
    cfg = cfg or Config()
    gaps: list[dict] = []

    # 1. Required columns (stop here if any are missing; later checks need them)
    for name, cols in REQUIRED_COLUMNS.items():
        df = tables.get(name)
        if df is None or df.empty:
            gaps.append(_gap(None, f"Table '{name}' is empty", f"Load {name} (scripts/seed.py)"))
            continue
        missing = [c for c in cols if c not in df.columns]
        if missing:
            gaps.append(_gap(None, f"Table '{name}' is missing columns {missing}",
                             f"Re-export {name} with columns {cols}"))
    if gaps:
        return gaps

    skus, sales, inv, cal = (tables[t] for t in ("skus", "weekly_sales", "inventory", "calendar"))

    # 2. No duplicate SKUs in inventory or the SKU table
    for sku in sorted(skus.loc[skus["sku_id"].duplicated(), "sku_id"].unique()):
        gaps.append(_gap(sku, "Duplicate SKU in SKU table", "Keep one row per SKU in sku_master"))
    dup_inv = inv[inv.duplicated(["sku_id", "location"])]
    for sku in sorted(dup_inv["sku_id"].unique()):
        gaps.append(_gap(sku, "Duplicate inventory row for the same location",
                         "Keep one row per SKU and location in current_inventory"))

    # 3. SKU keys match across sales, inventory and SKU tables
    s_set, a_set, i_set = set(skus["sku_id"]), set(sales["sku_id"]), set(inv["sku_id"])
    for sku in sorted(a_set - s_set):
        gaps.append(_gap(sku, "In sales but not in SKU table", "Add the SKU to sku_master or fix the ID"))
    for sku in sorted(i_set - s_set):
        gaps.append(_gap(sku, "In inventory but not in SKU table", "Add the SKU to sku_master or fix the ID"))
    for sku in sorted(s_set - i_set):
        gaps.append(_gap(sku, "No inventory row", "Add on-hand stock for this SKU (0 if none)"))
    for sku in sorted(s_set - a_set):
        gaps.append(_gap(sku, "No sales rows", "Add weekly sales (0 for weeks with no sales)"))

    # 4. No negative (or non-numeric) stock or sales
    on_hand = pd.to_numeric(inv["units_on_hand"], errors="coerce")
    for sku in sorted(inv.loc[on_hand.isna(), "sku_id"].unique()):
        gaps.append(_gap(sku, "Stock on hand is blank or not a number", "Fill units_on_hand"))
    for sku in sorted(inv.loc[on_hand < 0, "sku_id"].unique()):
        gaps.append(_gap(sku, "Negative stock on hand", "Correct units_on_hand in the SAP export"))
    sold = pd.to_numeric(sales["units_sold"], errors="coerce")
    for sku in sorted(sales.loc[sold.isna(), "sku_id"].unique()):
        gaps.append(_gap(sku, "Sales units blank or not a number", "Fill units_sold (0 if none)"))
    for sku in sorted(sales.loc[sold < 0, "sku_id"].unique()):
        gaps.append(_gap(sku, "Negative weekly sales", "Correct units_sold (returns are not sales)"))

    # 5. Standard quantity and perishability present for every active SKU
    std = pd.to_numeric(skus["standard_reorder_qty"], errors="coerce")
    for sku in sorted(skus.loc[std.isna() | (std <= 0), "sku_id"]):
        gaps.append(_gap(sku, "Standard reorder quantity missing", "Fill standard_reorder_qty in "
                         "sku_master, or sign the default rule and run with --allow-defaults",
                         MISSING_STD_QTY))
    known = set(cfg.known_perishability_values)
    for _, r in skus.iterrows():
        p = r["perishability"]
        if p is None or (isinstance(p, float) and pd.isna(p)) or str(p).strip() == "":
            gaps.append(_gap(r["sku_id"], "Perishability missing", f"Set perishability to one of {sorted(known)}"))
        elif str(p).strip() not in known:
            gaps.append(_gap(r["sku_id"], f"Unknown perishability '{p}'", f"Use one of {sorted(known)}"))

    # 6. At least 12 weeks of sales for non-new SKUs (no holes in the 12-week window)
    weeks = sales["week"].map(week_monday)
    for w in sorted(sales.loc[weeks.isna(), "week"].astype(str).unique()):
        gaps.append(_gap(None, f"Unreadable sales week '{w}'", "Use ISO format YYYY-Www"))
    cutoff = last_complete_week(run_date)
    ok = sales.assign(_monday=weeks, _units=sold).dropna(subset=["_monday"])
    ok = ok[ok["_monday"] <= cutoff]
    if ok.empty:
        gaps.append(_gap(None, "No complete sales week before the run date",
                         "Load weekly sales up to the last complete week"))
    else:
        end = ok["_monday"].max()
        n = cfg.long_window_weeks
        window_start = end - timedelta(weeks=n - 1)
        for sku, g in ok.groupby("sku_id"):
            sold_weeks = g.loc[g["_units"] > 0, "_monday"]
            if sold_weeks.empty:
                continue                                  # no-sales guard handles this in the engine
            if sold_weeks.min() > window_start:
                continue                                  # new SKU: under 12 weeks since first sale
            have = g.loc[g["_monday"] >= window_start, "_monday"].nunique()
            if have < n:
                gaps.append(_gap(sku, f"Only {have} of the last {n} weeks have sales rows",
                                 "Export every week, with 0 for weeks with no sales"))

    # 7. Calendar covers the run date (and the festival look-ahead date)
    cal_months = set(cal["year_month"].astype(str))
    check = pd.Timestamp(run_date) + pd.Timedelta(weeks=cfg.festival_lookahead_weeks)
    for d in {year_month(run_date), year_month(check)}:
        if d not in cal_months:
            gaps.append(_gap(None, f"Calendar has no row for {d}", "Add the month to calendar.csv "
                             "with Is_Festival_Month"))
    return gaps
