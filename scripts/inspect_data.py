"""Step 0: profile every CSV in data/raw/ (columns, dtypes, rows, samples, nulls, dates, key uniqueness)."""
from pathlib import Path
import pandas as pd

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
KEYS = {
    "sku_master.csv": ["SKU_ID"],
    "distributor_master.csv": ["Distributor_ID"],
    "current_inventory.csv": ["SKU_ID", "Location"],
    "weekly_sales.csv": ["SKU_ID", "Week"],
    "calendar.csv": ["Year_Month"],
}
DATES = {"current_inventory.csv": "As_Of_Date", "weekly_sales.csv": "Week", "calendar.csv": "Year_Month"}

for f in sorted(RAW.glob("*.csv")):
    df = pd.read_csv(f)
    print(f"\n===== {f.name}: {len(df)} rows")
    print(df.dtypes.to_string())
    print("-- sample\n" + df.head(5).to_string(index=False))
    print("-- nulls:", df.isna().sum().to_dict())
    if f.name in DATES:
        c = DATES[f.name]
        print(f"-- range {c}: {df[c].min()} .. {df[c].max()} ({df[c].nunique()} distinct)")
    if f.name in KEYS:
        k = KEYS[f.name]
        print(f"-- key {k} unique: {not df.duplicated(k).any()}")
