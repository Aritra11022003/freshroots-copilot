"""[C3] One test per validation check."""
import pandas as pd

from copilot.validate import validate, MISSING_STD_QTY
from tests.conftest import RUN_DATE


def issues(gaps):
    return [(g["sku"], g["issue"]) for g in gaps]


def test_clean_seed_data_passes(seed_tables):
    assert validate(seed_tables, RUN_DATE) == []


def test_missing_column_blocks(seed_tables):
    seed_tables["inventory"] = seed_tables["inventory"].drop(columns=["units_on_hand"])
    g = validate(seed_tables, RUN_DATE)
    assert len(g) == 1 and "missing columns" in g[0]["issue"]


def test_empty_table_blocks(seed_tables):
    seed_tables["calendar"] = pd.DataFrame()
    assert "empty" in validate(seed_tables, RUN_DATE)[0]["issue"]


def test_sku_keys_must_match(seed_tables):
    t = seed_tables
    t["inventory"] = t["inventory"][t["inventory"].sku_id != "SKU005"]
    t["weekly_sales"] = pd.concat([t["weekly_sales"], pd.DataFrame(
        [{"sku_id": "SKU999", "week": "2026-W42", "units_sold": 5}])])
    got = issues(validate(t, RUN_DATE))
    assert ("SKU005", "No inventory row") in got
    assert ("SKU999", "In sales but not in SKU table") in got


def test_duplicate_skus(seed_tables):
    t = seed_tables
    t["skus"] = pd.concat([t["skus"], t["skus"].iloc[[0]]])
    t["inventory"] = pd.concat([t["inventory"], t["inventory"].iloc[[1]]])
    got = issues(validate(t, RUN_DATE))
    assert ("SKU001", "Duplicate SKU in SKU table") in got
    assert ("SKU002", "Duplicate inventory row for the same location") in got


def test_negative_stock_and_sales(seed_tables):
    t = seed_tables
    t["inventory"].loc[t["inventory"].sku_id == "SKU003", "units_on_hand"] = -5
    t["weekly_sales"].loc[0, "units_sold"] = -1
    got = issues(validate(t, RUN_DATE))
    assert ("SKU003", "Negative stock on hand") in got
    assert ("SKU001", "Negative weekly sales") in got


def test_standard_qty_missing(seed_tables):
    t = seed_tables
    t["skus"]["standard_reorder_qty"] = t["skus"]["standard_reorder_qty"].astype(object)
    t["skus"].loc[t["skus"].sku_id == "SKU007", "standard_reorder_qty"] = None
    g = validate(t, RUN_DATE)
    assert [(x["sku"], x["code"]) for x in g] == [("SKU007", MISSING_STD_QTY)]


def test_perishability_missing_or_unknown(seed_tables):
    t = seed_tables
    t["skus"].loc[t["skus"].sku_id == "SKU001", "perishability"] = None
    t["skus"].loc[t["skus"].sku_id == "SKU002", "perishability"] = "Medium"
    got = issues(validate(t, RUN_DATE))
    assert ("SKU001", "Perishability missing") in got
    assert ("SKU002", "Unknown perishability 'Medium'") in got


def test_hole_in_12_week_history(seed_tables):
    t = seed_tables
    t["weekly_sales"] = t["weekly_sales"][~((t["weekly_sales"].sku_id == "SKU008")
                                            & (t["weekly_sales"].week == "2026-W40"))]
    assert issues(validate(t, RUN_DATE)) == [("SKU008", "Only 11 of the last 12 weeks have sales rows")]


def test_new_sku_with_short_history_is_not_a_gap(seed_tables):
    t = seed_tables
    ws = t["weekly_sales"]
    t["weekly_sales"] = ws[~((ws.sku_id == "SKU009") & (ws.week < "2026-W37"))]   # 6 weeks only
    assert validate(t, RUN_DATE) == []


def test_calendar_must_cover_run_date(seed_tables):
    got = issues(validate(seed_tables, "2027-01-14"))
    assert (None, "Calendar has no row for 2027-01") in got


def test_unreadable_week(seed_tables):
    seed_tables["weekly_sales"].loc[5, "week"] = "Week 33"
    assert (None, "Unreadable sales week 'Week 33'") in issues(validate(seed_tables, RUN_DATE))
