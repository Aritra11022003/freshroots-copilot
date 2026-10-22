"""[C2] Seed data: data/raw/*.csv -> rows for the 5 seed tables (snake_case columns)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import ROOT, Config, assumption_values

RAW_DIR = ROOT / "data" / "raw"

SEED_FILES = {
    "skus": "sku_master.csv",
    "distributors": "distributor_master.csv",
    "calendar": "calendar.csv",
    "inventory": "current_inventory.csv",
    "weekly_sales": "weekly_sales.csv",
}


def _snake(col: str) -> str:
    return col.strip().lower().replace(" ", "_")


def _clean(v):
    if pd.isna(v):
        return None
    if hasattr(v, "item"):          # numpy scalar -> python
        return v.item()
    return v


def load_seed_rows(raw_dir: str | Path = RAW_DIR) -> dict[str, list[dict]]:
    out = {}
    for table, fname in SEED_FILES.items():
        df = pd.read_csv(Path(raw_dir) / fname, dtype=object)  # keep values exactly as written
        df.columns = [_snake(c) for c in df.columns]
        for c in ("standard_reorder_qty", "units_on_hand", "units_sold", "year", "month"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        if "is_festival_month" in df.columns:
            df["is_festival_month"] = df["is_festival_month"].astype(str).str.upper().eq("TRUE")
        out[table] = [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]
    return out


def seed(db, raw_dir: str | Path = RAW_DIR, cfg: Config | None = None) -> dict[str, int]:
    """Idempotent: upsert on each table's primary key. Assumption rows are inserted only if
    missing, so re-seeding never clears a sign-off."""
    counts = {}
    for table, rows in load_seed_rows(raw_dir).items():
        db.upsert(table, rows)
        counts[table] = len(rows)
    vals = assumption_values(cfg or Config.load())
    db.upsert("assumptions", [{"key": k, "value": v, "signed_off_by": None, "signed_off_at": None}
                              for k, v in vals.items()], ignore_duplicates=True)
    counts["assumptions"] = len(vals)
    return counts
