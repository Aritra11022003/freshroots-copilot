"""App-side logic behind the Streamlit pages, kept free of Streamlit so it can be tested.

[C4] sign-off, [C7] queues, [C8] decisions, [C9] export.
"""
from __future__ import annotations

import pandas as pd

from .config import Config, assumption_values
from .db import now_iso
from .engine import APPROVE_QUEUE, EXCEPTION_QUEUE

BANNER = "UNSIGNED ASSUMPTIONS: not for procurement"
DECISIONS = ("approve", "override", "reject")


# ---- [C4] Sign-off -------------------------------------------------------------------------
def sync_assumptions(db, cfg: Config) -> list[str]:
    """Make the assumptions table match config.json. A changed value clears its sign-off
    (Raghav must re-sign what he is agreeing to). Returns the keys whose sign-off was cleared."""
    current = db.fetch_all("assumptions")
    have = dict(zip(current["key"], current["value"])) if not current.empty else {}
    changed = []
    for key, value in assumption_values(cfg).items():
        if key not in have:
            db.upsert("assumptions", [{"key": key, "value": value, "signed_off_by": None,
                                       "signed_off_at": None}])
        elif have[key] != value:
            db.update("assumptions", {"value": value, "signed_off_by": None, "signed_off_at": None},
                      {"key": key})
            changed.append(key)
    return changed


def signed_keys(db) -> set[str]:
    a = db.fetch_all("assumptions")
    if a.empty:
        return set()
    return set(a.loc[a["signed_off_by"].notna() & a["signed_off_at"].notna(), "key"])


def unsigned_keys(db) -> list[str]:
    a = db.fetch_all("assumptions")
    keys = set(assumption_values(Config()).keys()) | (set(a["key"]) if not a.empty else set())
    return sorted(keys - signed_keys(db))


def assumptions_signed(db) -> bool:
    return not unsigned_keys(db)


def banner(db) -> str | None:
    """Shown on every screen while any assumption is unsigned."""
    return None if assumptions_signed(db) else BANNER


def sign_assumption(db, key: str, who: str) -> None:
    if not who or not who.strip():
        raise ValueError("Sign-off needs a name or email")
    db.update("assumptions", {"signed_off_by": who.strip(), "signed_off_at": now_iso()}, {"key": key})


# ---- [C7] Queues --------------------------------------------------------------------------
def latest_run(db, status: str | None = None) -> dict | None:
    runs = db.fetch_all("runs")
    if runs.empty:
        return None
    if status:
        runs = runs[runs["status"] == status]
    if runs.empty:
        return None
    return runs.sort_values("id").iloc[-1].to_dict()


def queue_frame(db, run_id: int) -> pd.DataFrame:
    """Recommendations for a run joined to SKU name, perishability and region, plus the
    latest decision per SKU."""
    recs = db.fetch_all("recommendations", {"run_id": run_id})
    if recs.empty:
        return recs
    skus = db.fetch_all("skus")
    dist = db.fetch_all("distributors")
    if not skus.empty:
        recs = recs.merge(skus[["sku_id", "sku_name", "perishability", "distributor_id"]]
                          .rename(columns={"sku_id": "sku"}), on="sku", how="left")
    if not dist.empty and "distributor_id" in recs.columns:
        recs = recs.merge(dist[["distributor_id", "region"]], on="distributor_id", how="left")
    dec = latest_decisions(db, run_id)
    if not dec.empty:
        recs = recs.merge(dec[["sku", "decision", "final_action", "final_qty", "reason_code", "note",
                               "decided_by", "decided_at"]], on="sku", how="left")
    else:
        for c in ("decision", "final_action", "final_qty", "reason_code", "note", "decided_by", "decided_at"):
            recs[c] = None
    return recs


def latest_decisions(db, run_id: int) -> pd.DataFrame:
    """decisions is append-only; the newest row per SKU wins."""
    d = db.fetch_all("decisions", {"run_id": run_id})
    if d.empty:
        return d
    return d.sort_values("id").groupby("sku").tail(1).reset_index(drop=True)


# ---- [C8] Approve / override / reject -----------------------------------------------------
def record_decision(db, run_id: int, rec: dict, decision: str, decided_by: str, *,
                    final_action: str | None = None, final_qty: int | None = None,
                    reason_code: str | None = None, note: str | None = None,
                    cfg: Config | None = None) -> dict:
    """Write exactly one decisions row. Override needs a new qty/action plus a reason code;
    reject needs a note."""
    cfg = cfg or Config.load()
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if not decided_by or not str(decided_by).strip():
        raise ValueError("decided_by is required (your email or name)")
    engine_action, engine_qty = rec["action"], int(rec.get("recommended_qty") or 0)
    if decision == "approve":
        final_action, final_qty = engine_action, engine_qty
    elif decision == "override":
        final_action = final_action or engine_action
        final_qty = engine_qty if final_qty is None else int(final_qty)
        if final_qty < 0:
            raise ValueError("Override quantity cannot be negative")
        if (final_action, final_qty) == (engine_action, engine_qty):
            raise ValueError("Override needs a new quantity or action")
        if reason_code not in cfg.override_reason_codes:
            raise ValueError(f"Override needs a reason code: {cfg.override_reason_codes}")
    else:
        if not note or not note.strip():
            raise ValueError("Reject needs a note")
        final_action, final_qty = None, 0
    row = {"run_id": int(run_id), "sku": rec["sku"], "engine_action": engine_action,
           "final_action": final_action, "final_qty": final_qty, "decision": decision,
           "reason_code": reason_code if decision == "override" else None,
           "note": (note or None), "decided_by": str(decided_by).strip()}
    return db.insert("decisions", [row])[0]


# ---- [C9] Export --------------------------------------------------------------------------
def build_export(db) -> tuple[pd.DataFrame | None, str]:
    """Draft-PO CSV rows (sku, qty, notes): approved and overridden rows with qty > 0 from the
    latest successful run only. Returns (None, why) when export is not allowed."""
    if not assumptions_signed(db):
        return None, f"Export disabled: {BANNER}. Unsigned: {', '.join(unsigned_keys(db))}"
    run = latest_run(db, "ok")
    if run is None:
        return None, "No successful run yet"
    dec = latest_decisions(db, run["id"])
    if dec.empty:
        return pd.DataFrame(columns=["sku", "qty", "notes"]), f"Run {run['id']}: no decisions yet"
    keep = dec[dec["decision"].isin(["approve", "override"]) & (dec["final_qty"].fillna(0) > 0)]
    rows = []
    for _, d in keep.sort_values("sku").iterrows():
        note = f"Run {run['id']} ({run['run_date']}): {d['final_action']} {d['decision']}d by {d['decided_by']}"
        if d["decision"] == "override":
            note += f"; override {d['reason_code']}, engine said {d['engine_action']}"
        if isinstance(d.get("note"), str) and d["note"]:
            note += f"; {d['note']}"
        rows.append({"sku": d["sku"], "qty": int(d["final_qty"]), "notes": note})
    return pd.DataFrame(rows, columns=["sku", "qty", "notes"]), f"Run {run['id']} ({run['run_date']})"


def export_filename(run_date: str) -> str:
    return f"zoho_draft_po_{run_date}.csv"


__all__ = ["APPROVE_QUEUE", "EXCEPTION_QUEUE"]
