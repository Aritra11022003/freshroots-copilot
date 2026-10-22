"""[C6] Database layer.

Supabase returns at most 1,000 rows per request, so every read goes through
fetch_all(), which pages with .range() until a page comes back empty.

FakeDB has the same interface and keeps everything in memory, so the engine,
pipeline and app logic can be tested without a network.
"""
from __future__ import annotations

import copy
import itertools
import os
from datetime import datetime, timezone

import pandas as pd

PAGE_SIZE = 1000

# Stable ordering is required for paging to be correct.
ORDER = {
    "skus": ("sku_id",),
    "distributors": ("distributor_id",),
    "calendar": ("year_month",),
    "inventory": ("sku_id", "location"),
    "weekly_sales": ("sku_id", "week"),
    "assumptions": ("key",),
    "zoho_items": ("sku_id",),
}
PRIMARY_KEYS = {
    "skus": ("sku_id",),
    "distributors": ("distributor_id",),
    "calendar": ("year_month",),
    "inventory": ("sku_id", "location"),
    "weekly_sales": ("sku_id", "week"),
    "assumptions": ("key",),
    "zoho_items": ("sku_id",),
}
RUN_COLUMNS = ["run_date", "started_at", "finished_at", "status", "triggered_by",
               "assumptions_signed", "notes"]
REC_COLUMNS = ["sku", "route", "action", "recommended_qty", "on_hand", "on_order", "avg_4w",
               "avg_12w", "demand_change_pct", "festival_uplift_pct", "expected_weekly",
               "weeks_of_cover", "cover_window", "flags", "exceptions", "reason"]
GAP_COLUMNS = ["sku", "issue", "fix_needed"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseDB:
    """Thin wrapper over supabase-py. Server-side only (service role key)."""

    def __init__(self, url: str, key: str):
        from supabase import create_client
        self.client = create_client(url, key)

    def fetch_all(self, table: str, filters: dict | None = None) -> pd.DataFrame:
        rows, start = [], 0
        while True:
            q = self.client.table(table).select("*")
            for col, val in (filters or {}).items():
                q = q.eq(col, val)
            for col in ORDER.get(table, ("id",)):
                q = q.order(col)
            page = q.range(start, start + PAGE_SIZE - 1).execute().data
            if not page:
                break
            rows.extend(page)
            start += len(page)
        return pd.DataFrame(rows)

    def insert(self, table: str, rows: list[dict]) -> list[dict]:
        out = []
        for i in range(0, len(rows), 500):
            out.extend(self.client.table(table).insert(rows[i:i + 500]).execute().data)
        return out

    def upsert(self, table: str, rows: list[dict], ignore_duplicates: bool = False) -> None:
        conflict = ",".join(PRIMARY_KEYS[table])
        for i in range(0, len(rows), 500):
            self.client.table(table).upsert(rows[i:i + 500], on_conflict=conflict,
                                            ignore_duplicates=ignore_duplicates).execute()

    def update(self, table: str, values: dict, filters: dict) -> None:
        q = self.client.table(table).update(values)
        for col, val in filters.items():
            q = q.eq(col, val)
        q.execute()

    def record_run(self, run: dict, recs: list[dict], gaps: list[dict]) -> int:
        """Atomic: run + recommendations + gaps in one Postgres transaction."""
        res = self.client.rpc("record_run", {"p_run": run, "p_recs": recs, "p_gaps": gaps}).execute()
        return int(res.data)


class FakeDB:
    """In-memory stand-in with the same interface, for tests."""

    APPEND_ONLY = {"runs", "decisions", "zoho_po_pushes"}

    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables: dict[str, list[dict]] = {k: copy.deepcopy(v) for k, v in (tables or {}).items()}
        self._ids = itertools.count(1)

    def fetch_all(self, table, filters=None):
        rows = [r for r in self.tables.get(table, [])
                if all(r.get(c) == v for c, v in (filters or {}).items())]
        return pd.DataFrame(copy.deepcopy(rows))

    def insert(self, table, rows):
        out = []
        for r in rows:
            r = dict(r)
            if table not in PRIMARY_KEYS:
                r.setdefault("id", next(self._ids))
            if table == "decisions":
                r.setdefault("decided_at", now_iso())
            if table == "backtest_results":
                r.setdefault("ran_at", now_iso())
            self.tables.setdefault(table, []).append(r)
            out.append(dict(r))
        return out

    def upsert(self, table, rows, ignore_duplicates=False):
        pk = PRIMARY_KEYS[table]
        existing = self.tables.setdefault(table, [])
        for r in rows:
            match = next((e for e in existing if all(e.get(k) == r.get(k) for k in pk)), None)
            if match is None:
                existing.append(dict(r))
            elif not ignore_duplicates:
                match.update(r)

    def update(self, table, values, filters):
        if table in self.APPEND_ONLY:
            raise PermissionError(f"{table} is append-only")
        for r in self.tables.get(table, []):
            if all(r.get(c) == v for c, v in filters.items()):
                r.update(values)

    def record_run(self, run, recs, gaps):
        run_id = self.insert("runs", [{k: run.get(k) for k in RUN_COLUMNS}])[0]["id"]
        self.insert("recommendations", [{"run_id": run_id, **{k: r.get(k) for k in REC_COLUMNS}}
                                        for r in recs])
        self.insert("data_gaps", [{"run_id": run_id, **{k: g.get(k) for k in GAP_COLUMNS}}
                                  for g in gaps])
        return run_id


def get_db() -> SupabaseDB:
    """Credentials from env / .env (local, Actions) or st.secrets (Streamlit Cloud)."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        try:
            import streamlit as st
            url = url or st.secrets.get("SUPABASE_URL")
            key = key or st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
        except Exception:
            pass
    if not (url and key):
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (.env, Actions secrets "
                           "or Streamlit secrets).")
    return SupabaseDB(url, key)
