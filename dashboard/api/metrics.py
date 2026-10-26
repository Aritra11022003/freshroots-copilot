"""Read-only metrics API for the public ops dashboard (Vercel Python function: GET /api/metrics).

The Supabase key lives only in this server-side function (Vercel env vars SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY). The browser receives aggregates and per-SKU recommendation numbers
only: no people's names or emails, no notes, no secrets. Queries are fixed; no request input
reaches the database.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

PAGE = 1000


# ---- data access -----------------------------------------------------------------------
def supabase_fetch(table: str, params: dict | None = None) -> list[dict]:
    """GET every row of a PostgREST query, paging with limit/offset (Supabase caps at 1,000)."""
    import requests
    url, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    rows, offset = [], 0
    while True:
        q = {**(params or {}), "limit": PAGE, "offset": offset}
        r = requests.get(f"{url}/rest/v1/{table}", params=q, headers=headers, timeout=20)
        r.raise_for_status()
        page = r.json()
        if not page:
            return rows
        rows.extend(page)
        offset += len(page)


# ---- pure aggregation (unit-tested with a fake fetch) -----------------------------------
def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _on_order_source(notes: str) -> str:
    part = next((n for n in str(notes or "").split(" | ") if n.startswith("on_order")), "")
    return part.replace("on_order source: ", "") if "Zoho" in part else "default 0 (no on-order feed)"


def _run_view(r: dict | None) -> dict | None:
    if not r:
        return None
    return {"id": r["id"], "run_date": r["run_date"], "status": r["status"],
            "triggered_by": r["triggered_by"], "finished_at": r.get("finished_at"),
            "assumptions_signed": bool(r.get("assumptions_signed")),
            "on_order_source": _on_order_source(r.get("notes"))}


def build_metrics(fetch) -> dict:
    runs = sorted(fetch("runs", {"select": "id,run_date,status,triggered_by,finished_at,assumptions_signed,notes",
                                 "order": "id.asc"}), key=lambda r: r["id"])
    latest_any = runs[-1] if runs else None
    ok_runs = [r for r in runs if r["status"] == "ok"]
    latest_ok = ok_runs[-1] if ok_runs else None

    skus = {s["sku_id"]: s for s in fetch("skus", {"select": "sku_id,sku_name,category,perishability,distributor_id,standard_reorder_qty"})}
    regions = {d["distributor_id"]: d["region"] for d in fetch("distributors", {"select": "distributor_id,region"})}
    assumptions = fetch("assumptions", {"select": "key,signed_off_at"})

    recs, decisions_summary = [], {"approve": 0, "override": 0, "reject": 0, "undecided": 0}
    if latest_ok:
        rid = latest_ok["id"]
        raw = fetch("recommendations", {"run_id": f"eq.{rid}", "order": "id.asc",
                                        "select": "sku,route,action,recommended_qty,on_hand,on_order,avg_4w,avg_12w,"
                                                  "demand_change_pct,festival_uplift_pct,expected_weekly,"
                                                  "weeks_of_cover,cover_window,exceptions,reason,flags"})
        dec = fetch("decisions", {"run_id": f"eq.{rid}", "order": "id.asc",
                                  "select": "id,sku,decision,final_action,final_qty"})
        latest_dec = {}
        for d in sorted(dec, key=lambda d: d["id"]):
            latest_dec[d["sku"]] = d                         # newest decision per SKU wins
        for r in raw:
            s = skus.get(r["sku"], {})
            lo, hi = (str(r.get("cover_window") or "0-0").split("-") + ["0"])[:2]
            d = latest_dec.get(r["sku"])
            expected = _num(r.get("expected_weekly"))
            qty_after = (d["final_qty"] or 0) if d and d["decision"] in ("approve", "override") else 0
            projected = None
            if expected and qty_after:
                projected = round(((r.get("on_hand") or 0) + (r.get("on_order") or 0) + qty_after) / expected, 1)
            recs.append({
                "sku": r["sku"], "name": s.get("sku_name"), "category": s.get("category"),
                "perishability": s.get("perishability"), "expected": expected, "projected_cover": projected,
                "reason": r.get("reason"), "std_qty": s.get("standard_reorder_qty"),
                "region": regions.get(s.get("distributor_id")), "route": r["route"], "action": r["action"],
                "qty": int(r.get("recommended_qty") or 0), "on_hand": r.get("on_hand"), "on_order": r.get("on_order"),
                "avg_4w": _num(r.get("avg_4w")), "avg_12w": _num(r.get("avg_12w")),
                "change_pct": _num(r.get("demand_change_pct")), "uplift_pct": _num(r.get("festival_uplift_pct")),
                "cover": _num(r.get("weeks_of_cover")), "window_min": _num(lo), "window_max": _num(hi),
                "has_exception": bool(r.get("exceptions")),
                # the engine's own verdicts (weeks_of_cover is stored rounded, so never re-derive these)
                "stockout": "STOCKOUT_RISK" in str(r.get("flags") or ""),
                "overstock": "OVERSTOCK_RISK" in str(r.get("flags") or ""),
                "decision": d["decision"] if d else None,
                "final_qty": int(d["final_qty"] or 0) if d and d.get("final_qty") is not None else None,
            })
            decisions_summary[d["decision"] if d else "undecided"] += 1

    pushes = []
    try:
        pushes = fetch("zoho_po_pushes", {"select": "id,run_id,reference_number,zoho_po_number,status,total_qty,"
                                                   "line_count,pushed_at", "order": "id.desc"})[:10]
        snaps = fetch("zoho_on_order", {"select": "synced_at,units_on_order", "order": "synced_at.desc"})
    except Exception:                                    # Zoho tables not migrated: dashboard still works
        snaps = []
    last_sync = snaps[0]["synced_at"] if snaps else None
    skus_on_order = sum(1 for s in snaps if s["synced_at"] == last_sync and (s["units_on_order"] or 0) > 0)

    bt = fetch("backtest_results", {"select": "id,ran_at,metrics,gate_status", "order": "id.desc", "limit": 1})
    backtest = None
    if bt:
        m = bt[0]["metrics"] or {}
        backtest = {"gate_status": bt[0]["gate_status"], "gate_message": (m.get("gate") or {}).get("message"),
                    "match_rate": (m.get("gate") or {}).get("match_rate"),
                    "avg_flip_rate": m.get("avg_flip_rate"), "low_confidence": m.get("low_confidence"),
                    "decision_weeks": len(m.get("decision_weeks") or []), "ran_at": bt[0]["ran_at"]}

    gaps = 0
    if latest_any and latest_any["status"] == "blocked":
        gaps = len(fetch("data_gaps", {"run_id": f"eq.{latest_any['id']}", "select": "id"}))

    reorder = [r for r in recs if r["action"] in ("REORDER_NOW", "REORDER_1.5X")]

    # Trend across the last 8 successful runs (reorder units / SKUs per run)
    trend = []
    recent = ok_runs[-8:]
    if recent:
        ids = ",".join(str(r["id"]) for r in recent)
        rows = fetch("recommendations", {"run_id": f"in.({ids})", "select": "run_id,action,recommended_qty"})
        for run in recent:
            mine = [x for x in rows if x["run_id"] == run["id"]]
            ro = [x for x in mine if x["action"] in ("REORDER_NOW", "REORDER_1.5X")]
            trend.append({"run_id": run["id"], "run_date": run["run_date"],
                          "reorder_units": sum(int(x.get("recommended_qty") or 0) for x in ro),
                          "reorder_skus": len(ro),
                          "pause_skus": sum(1 for x in mine if x["action"] == "PAUSE_NEXT_ORDER")})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest_run": _run_view(latest_ok),
        "latest_any_run": _run_view(latest_any),
        "blocked_gaps": gaps,
        "assumptions": {"signed": sum(1 for a in assumptions if a.get("signed_off_at")), "total": len(assumptions)},
        "summary": {
            "skus": len(recs), "reorder_skus": len(reorder), "reorder_units": sum(r["qty"] for r in reorder),
            "pause_skus": sum(1 for r in recs if r["action"] == "PAUSE_NEXT_ORDER"),
            "exceptions": sum(1 for r in recs if r["route"] == "EXCEPTION_QUEUE"),
            "decisions": decisions_summary,
        },
        "recommendations": recs,
        "trend": trend,
        "runs": [_run_view(r) for r in runs[-12:]][::-1],
        "zoho": {"last_sync": last_sync, "skus_with_on_order": skus_on_order,
                 "pushes": [{k: p.get(k) for k in ("run_id", "reference_number", "zoho_po_number", "status",
                                                    "total_qty", "line_count", "pushed_at")} for p in pushes]},
        "backtest": backtest,
    }


# ---- Vercel entry point ----------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            body, status = build_metrics(supabase_fetch), 200
        except Exception as e:                           # never leak details or secrets
            body, status = {"error": "metrics unavailable", "kind": type(e).__name__}, 503
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "public, s-maxage=120, stale-while-revalidate=600")
        self.end_headers()
        self.wfile.write(data)
