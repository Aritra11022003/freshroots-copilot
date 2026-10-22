"""Run the replenishment rules on a folder of exports.

    python run.py --data data --run-date 2026-10-22 --out output
"""
import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from freshroots_rules import Config, run_rules, EXCEPTION_QUEUE


def main():
    ap = argparse.ArgumentParser(description="Freshroots replenishment rules engine")
    ap.add_argument("--data", default="data", help="folder with sales.csv, stock.csv, sku_master.csv")
    ap.add_argument("--run-date", default=date.today().isoformat())
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="output")
    args = ap.parse_args()

    data = Path(args.data)
    sales = pd.read_csv(data / "sales.csv")
    stock = pd.read_csv(data / "stock.csv")
    skus = pd.read_csv(data / "sku_master.csv")
    cfg = Config.from_json(args.config)

    recs = run_rules(sales, stock, skus, args.run_date, cfg)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = args.run_date
    recs.to_csv(out / f"recommendations_{stamp}.csv", index=False)
    recs[recs["route"] == EXCEPTION_QUEUE].to_csv(out / f"exceptions_{stamp}.csv", index=False)

    print(f"Run date {stamp}: {len(recs)} SKUs")
    print(pd.crosstab(recs["action"], recs["route"], margins=True, margins_name="Total").to_string())
    print(f"\nWritten to {out.resolve()}")


if __name__ == "__main__":
    main()
