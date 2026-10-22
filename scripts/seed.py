"""[C2] One-time seed: load data/raw/*.csv into Supabase. Idempotent; safe to re-run.

    python scripts/seed.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.db import get_db  # noqa: E402
from copilot.seed import seed  # noqa: E402

if __name__ == "__main__":
    for table, n in seed(get_db()).items():
        print(f"{table:14s} {n:5d} rows upserted")
