"""[C1] run_pipeline(): the one function behind the cron job, `python run.py` and "Run now".

  sync assumptions -> load seed tables (fetch_all) -> [C3] validate -> [C5] rules
  -> [C6] record_run (one transaction: run + recommendations + gaps)

Exit semantics for run.py: ok -> 0; blocked or error -> 1 (the Action shows red).
"""
from __future__ import annotations

import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .config import Config
from .db import REC_COLUMNS, now_iso
from .engine import run_rules, sales_end_week, sku_averages, standard_qty_default
from .service import assumptions_signed, signed_keys, sync_assumptions
from .validate import MISSING_STD_QTY, validate
from .zoho_config import zoho_enabled

IST = ZoneInfo("Asia/Kolkata")
SEED_TABLES = ["skus", "weekly_sales", "inventory", "calendar", "distributors"]


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def _json_safe(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def run_pipeline(db, run_date: str | None = None, triggered_by: str = "manual",
                 allow_defaults: bool = False, cfg: Config | None = None,
                 on_order_provider=None) -> dict:
    """on_order_provider(db) -> ({sku: qty}, meta). Defaults to Zoho when ZOHO_ENABLED is true,
    otherwise None (on_order stays the engine default, exactly as before)."""
    cfg = cfg or Config.load()
    run_date = run_date or today_ist()
    started = now_iso()
    notes: list[str] = []
    signed = False
    try:
        changed = sync_assumptions(db, cfg)
        if changed:
            notes.append(f"config changed, sign-off cleared for: {', '.join(changed)}")
        signed = assumptions_signed(db)
        if not signed:
            notes.append("UNSIGNED ASSUMPTIONS: not for procurement")

        tables = {t: db.fetch_all(t) for t in SEED_TABLES}

        # [C11] On-order stock from Zoho (only when ZOHO_ENABLED). A failed sync BLOCKS the
        # run: stale on-order numbers cause double orders, so there is no silent fallback.
        if on_order_provider is None and zoho_enabled():
            from .zoho_sync import zoho_on_order_provider
            on_order_provider = zoho_on_order_provider
        if on_order_provider is not None:
            try:
                on_order, meta = on_order_provider(db)
            except Exception as e:
                gap = {"sku": None, "issue": f"Zoho on-order sync failed: {e}",
                       "fix_needed": "Check the Zoho page (connection / secrets), then Run now. Blocked so "
                                     "stale on-order data cannot cause double orders."}
                run = _run_row(run_date, started, "blocked", triggered_by, signed,
                               notes + ["on_order source: Zoho, SYNC FAILED; no recommendations written"])
                run_id = db.record_run(run, [], [gap])
                return {"run_id": run_id, "status": "blocked", "gaps": [gap], "recommendations": 0,
                        "notes": run["notes"]}
            inv = tables["inventory"].copy()
            inv["units_on_order"] = 0
            first = ~inv["sku_id"].duplicated()             # one row per SKU carries the qty
            inv.loc[first, "units_on_order"] = inv.loc[first, "sku_id"].map(on_order).fillna(0).astype(int)
            tables["inventory"] = inv
            notes.append(f"on_order source: Zoho (synced {meta['synced_at'][:16]}Z; "
                         f"{meta['skus_with_open_pos']} SKUs with open POs)")

        # [C3] validation gate
        gaps = validate(tables, run_date, cfg)
        defaults: dict[str, int] = {}
        if allow_defaults and any(g["code"] == MISSING_STD_QTY for g in gaps):
            if "standard_qty_default_rule" in signed_keys(db):
                end = sales_end_week(tables["weekly_sales"], run_date)
                avgs = sku_averages(tables["weekly_sales"], end, cfg) if end else pd.DataFrame()
                for g in [g for g in gaps if g["code"] == MISSING_STD_QTY]:
                    a12 = avgs.loc[g["sku"], "avg_12w"] if g["sku"] in avgs.index else 0
                    q = standard_qty_default(a12, cfg)
                    if q:
                        defaults[g["sku"]] = q
                        gaps.remove(g)
                notes.append(f"--allow-defaults: standard qty defaulted for {sorted(defaults)}")
            else:
                notes.append("--allow-defaults ignored: standard_qty_default_rule is not signed off")

        if gaps:
            run = _run_row(run_date, started, "blocked", triggered_by, signed,
                           notes + [f"{len(gaps)} data gap(s); no recommendations written"])
            run_id = db.record_run(run, [], [{k: g[k] for k in ("sku", "issue", "fix_needed")} for g in gaps])
            return {"run_id": run_id, "status": "blocked", "gaps": gaps, "recommendations": 0, "notes": run["notes"]}

        # [C5] rules
        recs, engine_notes = run_rules(tables["weekly_sales"], tables["inventory"], tables["skus"],
                                       tables["calendar"], run_date, cfg, defaults=defaults)
        notes.extend(engine_notes)
        rows = [{k: _json_safe(r[k]) for k in REC_COLUMNS} for r in recs.to_dict("records")]
        run = _run_row(run_date, started, "ok", triggered_by, signed, notes)
        run_id = db.record_run(run, rows, [])
        return {"run_id": run_id, "status": "ok", "gaps": [], "recommendations": len(rows),
                "notes": run["notes"], "recs": recs}
    except Exception as e:  # record the failure; never leave a silent half-run
        msg = f"{type(e).__name__}: {e}"
        try:
            run_id = db.record_run(_run_row(run_date, started, "error", triggered_by, signed,
                                            notes + [msg]), [], [])
        except Exception:
            run_id = None
        return {"run_id": run_id, "status": "error", "gaps": [], "recommendations": 0,
                "notes": msg, "traceback": traceback.format_exc()}


def _run_row(run_date, started, status, triggered_by, signed, notes) -> dict:
    return {"run_date": run_date, "started_at": started, "finished_at": now_iso(), "status": status,
            "triggered_by": triggered_by, "assumptions_signed": bool(signed), "notes": " | ".join(notes)}
