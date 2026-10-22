"""Freshroots Replenishment Copilot: Streamlit app (Streamlit Community Cloud, private).

Pages: Queues [C7, C8] · Data gaps [C4] · Sign-off [C4] · Backtest [C10] · Export [C9] · History
Sidebar: "Run now" [C1], which calls the same run_pipeline() as the cron job.

    streamlit run app.py                  # needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
    COPILOT_DEMO=1 streamlit run app.py   # in-memory DB seeded from data/raw (nothing saved)
"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from copilot.backtest import PAST_DECISIONS_FORMAT, run_and_save
from copilot.config import ASSUMPTION_HELP, Config
from copilot.engine import ACTIONS, APPROVE_QUEUE, EXCEPTION_QUEUE
from copilot.pipeline import run_pipeline
from copilot.zoho_client import ZohoClient, ZohoError
from copilot.zoho_config import missing_keys, settings, zoho_enabled
from copilot.service import (assumptions_signed, banner, build_export, export_filename, latest_run, queue_frame,
                             record_decision, sign_assumption)

st.set_page_config(page_title="Freshroots Replenishment Copilot", page_icon="📦", layout="wide")

DEMO = os.getenv("COPILOT_DEMO") == "1"
CFG = Config.load()


@st.cache_resource
def get_database():
    if DEMO:
        from copilot.db import FakeDB
        from copilot.seed import seed
        db = FakeDB()
        seed(db)
        if zoho_enabled() and not missing_keys():      # demo + Zoho: reuse the existing item mapping
            try:
                from copilot.zoho_sync import setup_mapping
                setup_mapping(db, ZohoClient.from_settings(settings()))
            except ZohoError:
                pass
        return db
    from copilot.db import get_db
    return get_db()


def viewer_email() -> str | None:
    """Logged-in viewer's email on a private Streamlit Cloud app, when Streamlit provides it."""
    for attr in ("user", "experimental_user"):
        try:
            u = getattr(st, attr)
            email = u.get("email") if hasattr(u, "get") else getattr(u, "email", None)
            if email and "@" in str(email) and str(email) != "test@example.com":
                return str(email)
        except Exception:
            continue
    return None


db = get_database()

# ---- Sidebar ----------------------------------------------------------------------------
st.sidebar.title("📦 Replenishment Copilot")
if DEMO:
    st.sidebar.warning("DEMO mode: in-memory data, nothing is saved.")
page = st.sidebar.radio("Page", ["Queues", "Data gaps", "Sign-off", "Backtest", "Export", "History", "Zoho"])

email = viewer_email()
if email:
    st.sidebar.caption(f"Signed in as **{email}**")
    who = email
else:
    who = st.sidebar.text_input("Your name (recorded on decisions)", key="who")

st.sidebar.divider()
st.sidebar.subheader("Run now")
allow_defaults = st.sidebar.checkbox("Allow signed-off defaults", help="Same as --allow-defaults. "
                                     "Only defaults Raghav signed off are applied; those SKUs go to exceptions.")
if st.sidebar.button("▶ Run now", type="primary", use_container_width=True):
    with st.spinner("Validating data and applying the rules..."):
        res = run_pipeline(db, triggered_by="manual", allow_defaults=allow_defaults, cfg=CFG)
    {"ok": st.sidebar.success, "blocked": st.sidebar.warning, "error": st.sidebar.error}[res["status"]](
        f"Run {res['run_id']}: {res['status']} · {res['recommendations']} recommendations"
        + (f" · {len(res['gaps'])} data gaps (see Data gaps)" if res["gaps"] else ""))

st.sidebar.divider()
st.sidebar.caption("Scheduled: Mon & Thu 07:00 IST (GitHub Actions). Nothing is ever ordered "
                   "automatically: procurement imports approved rows into Zoho.")

# ---- Banner on every screen -------------------------------------------------------------
b = banner(db)
if b:
    st.error(f"⚠️ **{b}**. Raghav has not signed every assumption (see Sign-off). Export is disabled.")


def fmt_queue(df: pd.DataFrame) -> pd.DataFrame:
    cols = {"sku": "SKU", "sku_name": "Name", "action": "Action", "recommended_qty": "Qty",
            "weeks_of_cover": "Cover (wk)", "cover_window": "Window", "avg_4w": "4w avg",
            "avg_12w": "12w avg", "demand_change_pct": "Δ %", "on_hand": "On hand",
            "region": "Region", "decision": "Decision", "final_qty": "Final qty", "decided_by": "By"}
    return df[[c for c in cols if c in df.columns]].rename(columns=cols)


# ---- Pages ------------------------------------------------------------------------------
if page == "Queues":
    st.title("Queues")
    run = latest_run(db, "ok")
    last_any = latest_run(db)
    if last_any and run and last_any["id"] != run["id"]:
        st.warning(f"The latest run ({last_any['id']}, {last_any['run_date']}) was "
                   f"**{last_any['status']}**. Showing the last successful run. See Data gaps / History.")
    if run is None:
        st.info("No successful run yet. Use **Run now** in the sidebar.")
        st.stop()
    st.caption(f"Run {run['id']} · {run['run_date']} · {run['triggered_by']} · notes: {run['notes']}")
    src = next((n for n in str(run["notes"]).split(" | ") if n.startswith("on_order")), "")
    st.caption("📦 On-order source: " + (src.replace("on_order source: ", "") if "Zoho" in src
                                         else "default 0 (no on-order feed; Zoho off)"))

    q = queue_frame(db, run["id"])
    f1, f2, f3 = st.columns(3)
    actions = f1.multiselect("Action", sorted(q["action"].unique()))
    perish = f2.multiselect("Perishability", sorted(q["perishability"].dropna().unique()))
    regions = f3.multiselect("Region", sorted(q["region"].dropna().unique()))
    if actions:
        q = q[q["action"].isin(actions)]
    if perish:
        q = q[q["perishability"].isin(perish)]
    if regions:
        q = q[q["region"].isin(regions)]

    appr, exc = q[q["route"] == APPROVE_QUEUE], q[q["route"] == EXCEPTION_QUEUE]
    tab_a, tab_e = st.tabs([f"✅ Approve queue ({len(appr)})", f"⚠️ Exceptions ({len(exc)})"])

    def decision_form(r: dict, key: str):
        with st.form(key):
            choice = st.radio("Decision", ["approve", "override", "reject"], horizontal=True,
                              key=f"{key}-d")
            c1, c2, c3 = st.columns(3)
            new_action = c1.selectbox("Override action", ACTIONS, index=ACTIONS.index(r["action"]),
                                      key=f"{key}-a")
            new_qty = c2.number_input("Override qty", min_value=0, step=10,
                                      value=int(r["recommended_qty"] or 0), key=f"{key}-q")
            code = c3.selectbox("Reason code (override)", [""] + CFG.override_reason_codes, key=f"{key}-c")
            note = st.text_input("Note (required to reject)", key=f"{key}-n")
            if st.form_submit_button("Save decision"):
                try:
                    record_decision(db, run["id"], r, choice, who, final_action=new_action,
                                    final_qty=new_qty, reason_code=code or None, note=note, cfg=CFG)
                    st.success(f"{r['sku']}: {choice} saved")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    def render(df: pd.DataFrame, tab_key: str):
        if df.empty:
            st.info("Nothing in this queue for the current filters.")
            return
        st.dataframe(fmt_queue(df), hide_index=True, use_container_width=True)
        for r in df.to_dict("records"):
            status = f" · **{r['decision']}d** by {r['decided_by']}" if isinstance(r.get("decision"), str) else ""
            with st.expander(f"{r['sku']} · {r.get('sku_name') or ''} · {r['action']} {r['recommended_qty'] or ''}{status}"):
                st.write(r["reason"])
                if r.get("exceptions"):
                    st.warning("Why it's an exception: " + r["exceptions"])
                st.caption(f"Flags: {r.get('flags') or 'none'} · expected {r['expected_weekly']}/wk "
                           f"(festival uplift {r['festival_uplift_pct']}%) · on order {r['on_order']}")
                decision_form(r, f"{tab_key}-{r['sku']}")

    with tab_a:
        undecided = appr[appr["decision"].isna()]
        if len(undecided) and st.button(f"Approve all {len(undecided)} undecided rows shown"):
            if not who:
                st.error("Enter your name in the sidebar first.")
            else:
                for r in undecided.to_dict("records"):
                    record_decision(db, run["id"], r, "approve", who, cfg=CFG)
                st.rerun()
        render(appr, "a")
    with tab_e:
        render(exc, "e")

elif page == "Data gaps":
    st.title("Data gaps")
    blocked, ok = latest_run(db, "blocked"), latest_run(db, "ok")
    if blocked is None:
        st.success("No blocked runs. The validation gate has passed every time.")
        st.stop()
    if ok and ok["id"] > blocked["id"]:
        st.info(f"Resolved: run {ok['id']} passed after this block. Showing the most recent blocked run.")
    else:
        st.error(f"Run {blocked['id']} ({blocked['run_date']}) is BLOCKED. No recommendations were "
                 "written. Fix the data (or sign a default and run with defaults), then Run now.")
    gaps = db.fetch_all("data_gaps", {"run_id": blocked["id"]})
    st.dataframe(gaps[["sku", "issue", "fix_needed"]] if not gaps.empty else gaps,
                 hide_index=True, use_container_width=True)

elif page == "Sign-off":
    st.title("Sign-off: assumptions")
    st.write("These are the gaps in the written rules. The values come from `config.json` "
             "(changed by code review). If a value changes, its sign-off is cleared on the next run.")
    a = db.fetch_all("assumptions")
    for r in a.sort_values("key").to_dict("records"):
        signed = isinstance(r.get("signed_off_by"), str) and r["signed_off_by"]
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{r['key']}** = `{r['value']}`")
            c1.caption(ASSUMPTION_HELP.get(r["key"], ""))
            if signed:
                c2.success(f"Signed by {r['signed_off_by']}\n\n{str(r['signed_off_at'])[:16]}")
            elif c2.button("Sign", key=f"sign-{r['key']}"):
                try:
                    sign_assumption(db, r["key"], who)
                    st.rerun()
                except ValueError as e:
                    c2.error(str(e))

elif page == "Backtest":
    st.title("Backtest: the kill gate")
    if st.button("Run backtest now"):
        with st.spinner("Replaying the engine week by week..."):
            run_and_save(db, CFG)
    bt = db.fetch_all("backtest_results")
    if bt.empty:
        st.info("No backtest yet. Click **Run backtest now**.")
        st.stop()
    latest = bt.sort_values("id").iloc[-1]
    m = latest["metrics"]
    g = m["gate"]
    {"PASS": st.success, "FAIL": st.error}.get(g["status"], st.warning)(f"**{g['message']}**")
    if g["status"] == "NOT_EVALUABLE":
        st.code(PAST_DECISIONS_FORMAT, language="text")
        st.caption("Put this file at data/raw/raghav_past_decisions.csv, then re-run the backtest.")
    if m["low_confidence"]:
        st.warning("LOW CONFIDENCE: " + "; ".join(m["low_confidence_reasons"]))
    c1, c2, c3 = st.columns(3)
    c1.metric("History", f"{m['history_weeks']} weeks", m["sales_range"], delta_color="off")
    c2.metric("Replayed decision weeks", len(m["decision_weeks"]))
    c3.metric("Avg week-to-week flip rate", f"{m['avg_flip_rate']:.0%}")
    st.subheader("Action mix")
    st.bar_chart(pd.Series(m["action_mix"], name="SKU-weeks"))
    st.subheader("Stockouts flagged in advance")
    so = m["stockouts_flagged_in_advance"]
    st.warning(f"{so['status']}: {so['message']}")
    st.write("For SKUs at REORDER_NOW today, the number of consecutive earlier weeks that already "
             "flagged stockout risk:")
    st.dataframe(pd.Series(so["early_warning_weeks_for_current_reorders"], name="weeks of warning"))
    st.subheader("Flip rate by SKU, and actions by decision week")
    tl = pd.DataFrame(m["timeline"])
    tl["flip rate"] = pd.Series(m["flip_rate_by_sku"])
    st.dataframe(tl, use_container_width=True)
    for c in m["caveats"]:
        st.caption("⚠️ " + c)
    st.caption(f"Backtest {latest['id']} ran at {str(latest['ran_at'])[:16]}")

elif page == "Export":
    st.title("Export: Zoho draft POs")
    st.write("Approved and overridden rows with a quantity above 0, from the **latest successful "
             "run only**. Procurement imports this file into Zoho Inventory as draft POs. Nothing "
             "is ordered automatically.")
    out, msg = build_export(db)
    if out is None:
        st.error(msg)
        st.button("⬇ Download draft POs", disabled=True)
    else:
        st.caption(msg)
        st.dataframe(out, hide_index=True, use_container_width=True)
        run = latest_run(db, "ok")
        st.download_button(f"⬇ Download {export_filename(run['run_date'])}", out.to_csv(index=False),
                           file_name=export_filename(run["run_date"]), mime="text/csv",
                           disabled=out.empty, type="primary")

elif page == "History":
    st.title("History")
    runs = db.fetch_all("runs")
    if runs.empty:
        st.info("No runs yet.")
        st.stop()
    st.subheader("Runs")
    st.dataframe(runs.sort_values("id", ascending=False), hide_index=True, use_container_width=True)
    st.subheader("Decisions")
    dec = db.fetch_all("decisions")
    if dec.empty:
        st.info("No decisions yet.")
    else:
        st.dataframe(dec.sort_values("id", ascending=False), hide_index=True, use_container_width=True)

elif page == "Zoho":
    # Every Zoho call sits inside a button handler: nothing hits the network on a plain rerun.
    st.title("Zoho Inventory: draft purchase orders")
    st.caption("The copilot only creates DRAFT purchase orders. Procurement reviews and issues them "
               "inside Zoho. Nothing is ever issued, emailed or received from here.")
    out, msg = build_export(db)

    def csv_fallback():
        st.subheader("Fallback: CSV export")
        if out is None:
            st.caption(msg)
        else:
            run_ok = latest_run(db, "ok")
            st.download_button("⬇ Download draft POs as CSV", out.to_csv(index=False),
                               file_name=export_filename(run_ok["run_date"]), mime="text/csv",
                               disabled=out.empty, key="zoho-csv")

    if not zoho_enabled():
        st.info("Zoho is switched off (`ZOHO_ENABLED` is false), so the app works exactly as before. "
                "Use the Export page (CSV) for Zoho import.")
        csv_fallback()
        st.stop()
    if missing_keys():
        st.error(f"Zoho is switched on but these secrets are missing: {', '.join(missing_keys())}. "
                 "Add them in the app's secrets (see docs/zoho_setup.md).")
        csv_fallback()
        st.stop()

    from copilot.zoho_push import approved_rows, failed_vendors, push_drafts, push_log, retry_failed
    PREFIX = "FRC-DEMO" if DEMO else "FRC"

    @st.cache_resource
    def zoho_client() -> ZohoClient:
        # Cached object; the access token lives (and refreshes) inside it, never in st.cache_data.
        return ZohoClient.from_settings(settings())

    try:
        c1, c2 = st.columns(2)
        if c1.button("Check connection"):
            with st.spinner("Asking Zoho..."):
                try:
                    zoho_client().get("contacts", {"per_page": 1})
                    c1.success("Connected to Zoho Inventory")
                except ZohoError as e:
                    c1.error(f"Zoho: {e.message}")
        snaps = db.fetch_all("zoho_on_order")
        if snaps.empty:
            c2.caption("On-order: not synced yet (it syncs at the start of every run).")
        else:
            last = snaps[snaps["synced_at"] == snaps["synced_at"].max()]
            c2.caption(f"Last on-order sync: {str(last['synced_at'].iloc[0])[:16]}Z · "
                       f"{int((last['units_on_order'] > 0).sum())} SKUs with open POs")

        run = latest_run(db, "ok")
        if run is None:
            st.info("No successful run yet. Use Run now in the sidebar.")
            st.stop()
        st.subheader(f"Approved orders from run {run['id']} ({run['run_date']})")
        rows = approved_rows(db, run["id"])
        if rows.empty:
            st.info("Nothing approved with a quantity above 0 in this run yet (see Queues).")
        else:
            st.dataframe(rows[["sku", "final_action", "final_qty", "decision", "decided_by"]],
                         hide_index=True, use_container_width=True)
        signed = assumptions_signed(db)
        if not signed:
            st.warning("Disabled: every assumption must be signed off first (Sign-off page).")
        pushing = st.session_state.get("pushing", False)
        if st.button("📤 Send approved to Zoho (as drafts)", type="primary",
                     disabled=pushing or not signed or rows.empty or not who):
            st.session_state["pushing"] = True
            try:
                with st.spinner("Creating draft purchase orders in Zoho..."):
                    res = push_drafts(db, zoho_client(), run["id"], pushed_by=who, prefix=PREFIX)
                for r in res:
                    {"created": st.success, "skipped": st.info, "failed": st.error}[r["status"]](
                        f"{r['status'].upper()}: {r['reference_number']} → "
                        f"{r.get('zoho_po_number') or '-'} {('· ' + r['error']) if r.get('error') else ''}")
            except ZohoError as e:
                st.error(f"Zoho: {e.message}")
            finally:
                st.session_state["pushing"] = False
        if not who:
            st.caption("Enter your name in the sidebar to send.")

        failed = failed_vendors(db, run["id"])
        if failed and st.button(f"🔁 Retry failed ({len(failed)})", disabled=pushing or not signed):
            st.session_state["pushing"] = True
            try:
                with st.spinner("Retrying failed vendors..."):
                    retry_failed(db, zoho_client(), run["id"], pushed_by=who, prefix=PREFIX)
                st.rerun()
            except ZohoError as e:
                st.error(f"Zoho: {e.message}")
            finally:
                st.session_state["pushing"] = False

        st.subheader("Results per vendor")
        log = push_log(db, run["id"])
        if log.empty:
            st.caption("Nothing sent for this run yet.")
        else:
            st.dataframe(log[["pushed_at", "vendor_name", "reference_number", "status", "zoho_po_number",
                              "line_count", "total_qty", "error", "pushed_by"]],
                         hide_index=True, use_container_width=True)
    except ZohoError as e:
        st.error(f"Zoho: {e.message}")
    except Exception as e:  # never show a raw traceback to Raghav or procurement
        st.error(f"Zoho page problem: {type(e).__name__}. If the zoho tables are missing, run "
                 "supabase/migrations/002_zoho.sql in Supabase.")
    csv_fallback()
