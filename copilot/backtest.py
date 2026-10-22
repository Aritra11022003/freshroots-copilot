"""[C10] Backtest: the kill gate.

Replays the engine week by week over the available history:
  - decision point k = "a Thursday run with sales through week k", for every week k
    that has at least 12 weeks of data before it;
  - stock at point k is reconstructed from the one inventory snapshot by adding back
    the sales after week k, ASSUMING NO RECEIPTS (there is no receipts or stock history).

Reports the action mix, week-to-week flip rate per SKU, how early current stockout risks
were flagged, and the 80% gate against Raghav's past decisions if
data/raw/raghav_past_decisions.csv (sku, week, action) exists. The gate number is never
faked: without that file it is reported as not evaluable.

    python -m copilot.backtest          # reads Supabase, writes backtest_results
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd

from .config import ROOT, Config
from .engine import ACTIONS, APPROVE_QUEUE, REORDER_NOW, run_rules
from .weeks import week_label, week_monday

PAST_DECISIONS = ROOT / "data" / "raw" / "raghav_past_decisions.csv"
PAST_DECISIONS_FORMAT = ("sku,week,action\nSKU001,2026-W40,REORDER_NOW\n"
                         "  week = ISO week the decision was made (YYYY-Www); "
                         f"action = one of {', '.join(ACTIONS)}")


def run_backtest(tables: dict[str, pd.DataFrame], cfg: Config | None = None,
                 past_decisions: pd.DataFrame | None = None) -> dict:
    cfg = cfg or Config()
    sales, inv = tables["weekly_sales"].copy(), tables["inventory"].copy()
    sales["_m"] = sales["week"].map(week_monday)
    sales["units_sold"] = pd.to_numeric(sales["units_sold"], errors="coerce").fillna(0)
    weeks = sorted(w for w in sales["_m"].dropna().unique())
    history_weeks = len(weeks)
    first, last = weeks[0], weeks[-1]
    caveats = ["Stock is reconstructed from the single inventory snapshot assuming NO receipts "
               "(no receipts or stock history in the data)."]

    snapshot = pd.to_datetime(inv["as_of_date"]).max().date() if "as_of_date" in inv else None
    if snapshot and snapshot != last + timedelta(weeks=1):
        caveats.append(f"Inventory snapshot {snapshot} is not the Monday after the last sales week "
                       f"{week_label(last)}; reconstruction treats it as if it were.")
    on_hand_now = pd.to_numeric(inv["units_on_hand"], errors="coerce").groupby(inv["sku_id"]).sum()
    weekly = sales.groupby(["sku_id", "_m"])["units_sold"].sum().unstack(fill_value=0)

    points = [w for w in weeks if (w - first).days // 7 + 1 >= cfg.min_history_weeks]
    timeline: dict[str, dict[str, dict]] = {}
    for end in points:
        after = [c for c in weekly.columns if c > end]
        stock_k = on_hand_now.add(weekly[after].sum(axis=1) if after else 0, fill_value=0)
        inv_k = pd.DataFrame({"sku_id": stock_k.index, "location": "ALL",
                              "units_on_hand": stock_k.values})
        run_date = end + timedelta(weeks=1, days=3)          # the Thursday after week `end`
        recs, _ = run_rules(sales[sales["_m"] <= end].drop(columns="_m"), inv_k, tables["skus"],
                            tables["calendar"], run_date, cfg)
        timeline[week_label(end + timedelta(weeks=1))] = {
            r["sku"]: {"action": r["action"], "route": r["route"], "flags": r["flags"],
                       "cover": r["weeks_of_cover"]} for r in recs.to_dict("records")}

    decision_weeks = list(timeline)
    skus = sorted(tables["skus"]["sku_id"].unique())

    mix: dict[str, int] = {}
    for wk in decision_weeks:
        for v in timeline[wk].values():
            mix[v["action"]] = mix.get(v["action"], 0) + 1

    flips = {}
    for s in skus:
        acts = [timeline[w][s]["action"] for w in decision_weeks if s in timeline[w]]
        changes = sum(a != b for a, b in zip(acts, acts[1:]))
        flips[s] = round(changes / (len(acts) - 1), 2) if len(acts) > 1 else 0.0

    # Early warning: for SKUs at REORDER_NOW in the latest week, how many consecutive earlier
    # decision weeks already flagged stockout risk.
    early = {}
    if decision_weeks:
        latest = timeline[decision_weeks[-1]]
        for s, v in latest.items():
            if v["action"] == REORDER_NOW:
                n = 0
                for wk in reversed(decision_weeks[:-1]):
                    if "STOCKOUT_RISK" in timeline[wk].get(s, {}).get("flags", ""):
                        n += 1
                    else:
                        break
                early[s] = n

    gate = {"threshold": cfg.backtest_gate}
    if past_decisions is None and PAST_DECISIONS.exists():
        past_decisions = pd.read_csv(PAST_DECISIONS)
    if past_decisions is None or past_decisions.empty:
        gate.update(status="NOT_EVALUABLE",
                    message="80% gate: not evaluable, needs Raghav's past decisions",
                    file_format=PAST_DECISIONS_FORMAT)
    else:
        pd_ = past_decisions.rename(columns=str.lower)
        compared = matched = 0
        for _, r in pd_.iterrows():
            v = timeline.get(str(r["week"]), {}).get(str(r["sku"]))
            if v is None or v["route"] != APPROVE_QUEUE:      # rule-covered SKUs only
                continue
            compared += 1
            matched += int(v["action"] == str(r["action"]).strip())
        if compared == 0:
            gate.update(status="NOT_EVALUABLE", message="80% gate: not evaluable, no past decision "
                        "overlaps a replayed week on a rule-covered SKU", file_format=PAST_DECISIONS_FORMAT)
        else:
            rate = matched / compared
            gate.update(status="PASS" if rate >= cfg.backtest_gate else "FAIL", match_rate=round(rate, 3),
                        compared=compared, matched=matched,
                        message=f"80% gate: {'PASS' if rate >= cfg.backtest_gate else 'FAIL'} "
                                f"({matched}/{compared} = {rate:.0%} vs {cfg.backtest_gate:.0%})")

    low_conf_reasons = []
    if history_weeks < cfg.backtest_min_history_weeks:
        low_conf_reasons.append(f"history is {history_weeks} weeks (< {cfg.backtest_min_history_weeks})")
    if len(decision_weeks) < 8:
        low_conf_reasons.append(f"only {len(decision_weeks)} replayable decision weeks")
    low_conf_reasons.append("stock reconstructed without receipts")

    return {
        "history_weeks": history_weeks,
        "sales_range": f"{week_label(first)}..{week_label(last)}",
        "decision_weeks": decision_weeks,
        "low_confidence": bool(low_conf_reasons),
        "low_confidence_reasons": low_conf_reasons,
        "action_mix": mix,
        "flip_rate_by_sku": flips,
        "avg_flip_rate": round(sum(flips.values()) / len(flips), 3) if flips else 0.0,
        "stockouts_flagged_in_advance": {
            "status": "NOT_EVALUABLE",
            "message": "No stock history or receipts, so past stockout weeks cannot be observed. "
                       "Reconstructed stock only rises going back in time.",
            "early_warning_weeks_for_current_reorders": early,
        },
        "gate": gate,
        "caveats": caveats,
        "timeline": {wk: {s: v["action"] for s, v in t.items()} for wk, t in timeline.items()},
    }


def save_backtest(db, metrics: dict) -> dict:
    return db.insert("backtest_results", [{"metrics": metrics, "gate_status": metrics["gate"]["status"]}])[0]


def run_and_save(db, cfg: Config | None = None) -> dict:
    tables = {t: db.fetch_all(t) for t in ("weekly_sales", "inventory", "skus", "calendar")}
    metrics = run_backtest(tables, cfg or Config.load())
    save_backtest(db, metrics)
    return metrics


if __name__ == "__main__":
    import json

    from .db import get_db
    m = run_and_save(get_db())
    print(json.dumps({k: v for k, v in m.items() if k != "timeline"}, indent=2, default=str))
