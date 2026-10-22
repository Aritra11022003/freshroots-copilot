"""[C1] Scheduled run entry point (GitHub Actions cron, or by hand).

    python run.py                          # today (IST), triggered_by=manual
    python run.py --triggered-by cron
    python run.py --run-date 2026-10-22 --allow-defaults
"""
import argparse
import sys

from copilot.db import get_db
from copilot.pipeline import run_pipeline


def main() -> int:
    p = argparse.ArgumentParser(description="Freshroots Replenishment Copilot run")
    p.add_argument("--run-date", help="YYYY-MM-DD (default: today in IST)")
    p.add_argument("--triggered-by", choices=["cron", "manual"], default="manual")
    p.add_argument("--allow-defaults", action="store_true",
                   help="apply only defaults Raghav has signed off; defaulted SKUs go to exceptions")
    a = p.parse_args()

    res = run_pipeline(get_db(), run_date=a.run_date, triggered_by=a.triggered_by,
                       allow_defaults=a.allow_defaults)
    print(f"run {res['run_id']}: {res['status']}, {res['recommendations']} recommendations")
    print(f"notes: {res['notes']}")
    for g in res["gaps"]:
        print(f"  GAP {g['sku'] or '-'}: {g['issue']} -> {g['fix_needed']}")
    if res["status"] == "error":
        print(res.get("traceback", ""), file=sys.stderr)
    return 0 if res["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
