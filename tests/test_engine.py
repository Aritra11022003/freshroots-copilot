"""[C5] One test per rule, combination and guard.

Each case is one SKU with numbers you can check on paper. JULY is not a
festival month; OCTOBER is (per the calendar)."""
from datetime import timedelta

import pandas as pd
import pytest

from copilot.config import Config
from copilot.engine import (APPROVE_QUEUE, EXCEPTION_QUEUE, NEEDS_REVIEW, NO_ACTION, PAUSE,
                            REORDER_HIGH_DEMAND, REORDER_NOW, run_rules, standard_qty_default)
from copilot.weeks import last_complete_week, week_label

JULY = "2026-07-16"
OCTOBER = "2026-10-22"
CAL = pd.DataFrame([{"year_month": f"2026-{m:02d}", "is_festival_month": m in (3, 10, 11)}
                    for m in range(1, 13)])


def case(weekly, on_hand, *, run=JULY, weeks=20, perish="Low", std=100, on_order=None,
         cfg=None, defaults=None, dist=None):
    """weekly: units/week (number) or list, newest last (shorter list = later launch)."""
    end = last_complete_week(run)
    series = weekly if isinstance(weekly, list) else [weekly] * weeks
    sales = pd.DataFrame([{"sku_id": "S1", "week": week_label(end - timedelta(weeks=len(series) - 1 - i)),
                           "units_sold": u} for i, u in enumerate(series)])
    inv = {"sku_id": "S1", "location": "WH", "units_on_hand": on_hand}
    if on_order is not None:
        inv["units_on_order"] = on_order
    skus = pd.DataFrame([{"sku_id": "S1", "sku_name": "Test", "perishability": perish,
                          "standard_reorder_qty": std}])
    recs, notes = run_rules(sales, pd.DataFrame([inv]), skus, CAL, run, cfg,
                            defaults=defaults, distributor_sales=dist)
    r = recs.iloc[0]
    r.notes = notes
    return r


# ---- Rule 2/3: cover window and checks -------------------------------------------------
def test_inside_window_no_action():
    r = case(100, 1000)                                  # 10 weeks, inside 6-14
    assert (r.action, r.route, r.weeks_of_cover, r.recommended_qty) == (NO_ACTION, APPROVE_QUEUE, 10.0, 0)


def test_stockout_risk_reorder_now_standard_qty():
    r = case(100, 500)                                   # 5 weeks < 6
    assert (r.action, r.recommended_qty, r.route) == (REORDER_NOW, 100, APPROVE_QUEUE)
    assert "STOCKOUT_RISK" in r["flags"]


def test_overstock_risk_pause():
    r = case(100, 1500)                                  # 15 weeks > 14
    assert (r.action, r.recommended_qty, r.route) == (PAUSE, 0, APPROVE_QUEUE)


def test_high_demand_reorder_1_5x():
    r = case([100] * 16 + [150] * 4, 1000)               # 150 vs 116.7 = +28.6%; cover 8.6
    assert (r.action, r.recommended_qty, r.route) == (REORDER_HIGH_DEMAND, 150, APPROVE_QUEUE)
    assert r.demand_change_pct == 28.6


def test_just_under_high_demand_threshold():
    r = case([100] * 16 + [125] * 4, 1000)               # 125 vs 108.3 = +15.4%
    assert r.action == NO_ACTION


def test_high_perishability_uses_3_to_4_week_window():
    assert case(100, 250, perish="High").action == REORDER_NOW       # 2.5 < 3
    assert case(100, 350, perish="High").action == NO_ACTION         # 3.5 inside 3-4
    r = case(100, 500, perish="High")                                # 5 > 4 (fine for normal SKUs)
    assert (r.action, r.cover_window) == (PAUSE, "3-4")


# ---- Rule 1: festival uplift before any check -------------------------------------------
def test_festival_uplift_applied_before_checks():
    r = case(100, 700, run=OCTOBER)                       # July: 7.0 weeks, fine. October: 700/125 = 5.6
    assert (r.action, r.expected_weekly, r.weeks_of_cover, r.festival_uplift_pct) == (REORDER_NOW, 125.0, 5.6, 25)
    assert "October uplift 25% applied" in r.reason
    assert case(100, 700).action == NO_ACTION


def test_festival_uplift_not_applied_to_high_demand_check_by_default():
    r = case([100] * 16 + [115] * 4, 1000, run=OCTOBER)   # +11% raw; x1.25 would look like +39%
    assert "HIGH_DEMAND" not in r["flags"]
    cfg = Config(festival_uplift_applies_to_high_demand_check=True)
    assert "HIGH_DEMAND" in case([100] * 16 + [115] * 4, 1000, run=OCTOBER, cfg=cfg)["flags"]


def test_festival_lookahead_reads_next_month():
    cfg = Config(festival_lookahead_weeks=2)
    r = case(100, 700, run="2026-09-24", cfg=cfg)          # 24 Sep + 2 weeks = October
    assert r.festival_uplift_pct == 25


# ---- Rule 4: combinations ---------------------------------------------------------------
def test_stockout_plus_high_demand_reorder_now_1_5x():
    r = case([100] * 16 + [150] * 4, 500)                 # cover 4.3 < 6, demand +28.6%
    assert (r.action, r.recommended_qty, r.route) == (REORDER_NOW, 150, APPROVE_QUEUE)
    assert "1.5x standard: 150 units" in r.reason


def test_overstock_plus_high_demand_is_a_conflict():
    r = case([100] * 16 + [150] * 4, 2000)                # cover 17.1 > 14, demand +28.6%
    assert (r.action, r.route) == (PAUSE, EXCEPTION_QUEUE)
    assert "Conflict" in r.exceptions


# ---- Rule 5: exception guards -----------------------------------------------------------
def test_guard_new_sku_under_12_weeks_averages_only_since_launch():
    r = case([0, 0, 0] + [100] * 8, 500)                  # pre-launch zeros ignored: avg 100, not 72.7
    assert (r.route, r.avg_12w, r.weeks_of_cover) == (EXCEPTION_QUEUE, 100.0, 5.0)
    assert "8 weeks of history" in r.exceptions


def test_guard_no_sales_in_12_weeks():
    r = case([100] * 8 + [0] * 12, 500)
    assert (r.action, r.route) == (NEEDS_REVIEW, EXCEPTION_QUEUE)
    assert "No sales in the last 12 weeks" in r.exceptions


def test_guard_order_would_push_cover_above_max():
    r = case(100, 250, perish="High", std=300)            # 2.5 wk -> (250+300)/100 = 5.5 > 4
    assert (r.action, r.route) == (REORDER_NOW, EXCEPTION_QUEUE)
    assert "5.5 weeks" in r.exceptions


def test_guard_distributor_drop_when_distributor_sales_exist():
    end = last_complete_week(JULY)
    rows = []
    for i in range(9):
        wk = week_label(end - timedelta(weeks=8 - i))
        rows += [{"sku_id": "S1", "distributor_id": "D1", "week": wk, "units_sold": 60 if i < 8 else 30},
                 {"sku_id": "S1", "distributor_id": "D2", "week": wk, "units_sold": 40}]
    r = case(100, 1000, dist=pd.DataFrame(rows))          # D1: 60% share, -50% last week
    assert r.route == EXCEPTION_QUEUE and "Distributor D1" in r.exceptions


def test_distributor_guard_skipped_and_noted_without_distributor_sales():
    r = case(100, 1000)
    assert r.route == APPROVE_QUEUE
    assert any("distributor_drop guard skipped" in n for n in r.notes)


def test_guard_defaulted_sku_goes_to_exceptions():
    r = case(100, 500, std=None, defaults={"S1": 400})
    assert (r.action, r.recommended_qty, r.route) == (REORDER_NOW, 400, EXCEPTION_QUEUE)
    assert "DEFAULTED" in r.exceptions


def test_standard_qty_default_rule():
    assert standard_qty_default(83.8, Config()) == 340     # 4 x 83.8 = 335.2 -> 340
    assert standard_qty_default(0, Config()) is None


# ---- Stock definition --------------------------------------------------------------------
def test_on_order_counts_toward_cover_when_signed():
    assert case(100, 500, on_order=200).action == NO_ACTION                     # 7 weeks
    cfg = Config(include_on_order_in_cover=False)
    assert case(100, 500, on_order=200, cfg=cfg).action == REORDER_NOW          # 5 weeks


def test_on_order_defaults_to_zero_and_is_noted():
    r = case(100, 1000)
    assert r.on_order == 0 and any("on_order = 0" in n for n in r.notes)


def test_in_progress_week_is_ignored():
    r = case(100, 1000)
    assert r.avg_12w == 100.0                             # windows end at the last complete week


def test_reason_matches_spec_shape():
    r = case(100, 400, run=OCTOBER, std=245)
    assert r.reason == ("Stock covers 3.2 weeks against a 6–14 week window; 4-week avg 100/wk is "
                        "+0% vs 12-week avg 100/wk; October uplift 25% applied; reorder now: 245 units.")


# ---- Seed data: every SKU exactly once, expected actions -------------------------------
def test_seed_data_expected_actions(seed_tables):
    t = seed_tables
    recs, _ = run_rules(t["weekly_sales"], t["inventory"], t["skus"], t["calendar"], OCTOBER)
    assert sorted(recs.sku) == [f"SKU{i:03d}" for i in range(1, 16)]
    got = dict(zip(recs.sku, zip(recs.action, recs.recommended_qty)))
    assert got["SKU001"] == (REORDER_NOW, 750)
    for s, q in (("SKU003", 300), ("SKU004", 150), ("SKU011", 500), ("SKU013", 120)):
        assert got[s] == (REORDER_NOW, q)
    assert got["SKU006"][0] == PAUSE and got["SKU007"][0] == PAUSE
    assert (recs.action == NO_ACTION).sum() == 8
