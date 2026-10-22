"""Generate synthetic exports shaped like the SAP and Zoho files.

Replace these with the real exports once IT confirms the format. 40 SKUs,
60 distributors, 60 weeks, seeded so every scenario appears.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from freshroots_rules import last_complete_weeks

RUN_DATE = "2026-10-22"
WEEKS = 60
rng = np.random.default_rng(7)

SCENARIOS = (["normal"] * 20 + ["high_demand"] * 4 + ["stockout"] * 4 + ["overstock"] * 4
             + ["perishable_ok"] * 2 + ["perishable_over"] * 2 + ["new_launch"] * 2
             + ["distributor_drop"] + ["conflict"])
TARGET_COVER = {"high_demand": 9, "stockout": 4, "overstock": 18, "perishable_ok": 3.5,
                "perishable_over": 6, "new_launch": 5, "distributor_drop": 9, "conflict": 18}
NAMES = ["Masala Chakli", "Banana Chips", "Poha", "Besan", "Farsan Mix", "Shev", "Rava",
         "Kokum Sherbet", "Bhakarwadi", "Jaggery", "Peanut Chikki", "Moong Dal Namkeen"]

grid = last_complete_weeks(RUN_DATE, WEEKS)
distributors = [f"D{i:04d}" for i in range(1, 61)]
sales_rows, sku_rows, stock_rows = [], [], []

for i, scenario in enumerate(SCENARIOS, start=1):
    sku = f"FR{i:03d}"
    base = int(rng.integers(300, 2000))
    dists = rng.choice(distributors, size=int(rng.integers(8, 20)), replace=False)
    shares = rng.dirichlet(np.ones(len(dists)))
    weeks = grid[-6:] if scenario == "new_launch" else grid
    biggest = dists[np.argmax(shares)]
    for w_idx, week in enumerate(weeks):
        weeks_from_end = len(weeks) - w_idx
        mult = 1.5 if scenario in ("high_demand", "conflict") and weeks_from_end <= 4 else 1.0
        for d, sh in zip(dists, shares):
            units = base * sh * mult * rng.normal(1, 0.08)
            if scenario == "distributor_drop" and d == biggest and weeks_from_end == 1:
                units *= 0.25
            sales_rows.append((week.date().isoformat(), sku, d, max(0, round(units))))

    perishable = "high" if scenario.startswith("perishable") or (i % 9 == 0 and scenario == "normal") else "normal"
    sku_rows.append({
        "sku": sku,
        "sku_name": f"{NAMES[i % len(NAMES)]} {[100, 200, 500][i % 3]}g",
        "perishability": perishable,
        "standard_order_qty": int(round(base * (2 if perishable == "high" else 4), -1)),
        "launch_date": grid[-6].date().isoformat() if scenario == "new_launch" else "",
    })

sales = pd.DataFrame(sales_rows, columns=["week_start", "sku", "distributor_id", "units"])

# Stock levels set to land each SKU in its scenario (October = festival uplift)
avg12 = (sales[sales.week_start >= grid[-12].date().isoformat()]
         .groupby("sku")["units"].sum() / 12)
for row in sku_rows:
    sku = row["sku"]
    scenario = SCENARIOS[int(sku[2:]) - 1]
    if scenario == "normal":
        target = 3.5 if row["perishability"] == "high" else rng.uniform(7, 12)
    else:
        target = TARGET_COVER[scenario]
    expected = avg12[sku] * 1.25
    on_order = round(expected * 2) if scenario == "normal" and int(sku[2:]) % 5 == 0 else 0
    stock_rows.append({"sku": sku, "on_hand": max(0, round(expected * target) - on_order),
                       "on_order": on_order})

out = Path("data")
out.mkdir(exist_ok=True)
sales.to_csv(out / "sales.csv", index=False)
pd.DataFrame(sku_rows).to_csv(out / "sku_master.csv", index=False)
pd.DataFrame(stock_rows).to_csv(out / "stock.csv", index=False)
print(f"Wrote {len(sales):,} sales rows, {len(sku_rows)} SKUs to {out.resolve()}")
