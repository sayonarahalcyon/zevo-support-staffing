"""Streamlit screens for the Leave log and Forecast tabs.

app.py owns the data loading (Intercom, Slack, Supabase, with caching) and
passes what these screens need in as arguments, so nothing here talks to an
outside service except the leave writes and the Notion copy the user triggers.
"""
from __future__ import annotations

import hmac
import time
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import forecast as fc_mod
import leave as leave_mod
import leave_store
import notion_backup
import recommend
import roster
import style

LOCAL_TZ = ZoneInfo("America/Chicago")
FORECAST_HISTORY_DAYS = 28


def _fmt_day(d) -> str:
    return leave_mod.to_date(d).strftime("%a %b %-d")


def _range(value) -> tuple[date, date] | None:
    """date_input returns a 1-tuple while the user has clicked only the first
    day of a range; treat that as 'not ready yet'."""
    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            return value[0], value[1]
        return None
    return value, value


# ------------------------------------------------------------------ login --
def login_gate(users: dict[str, str]) -> str | None:
    """Shows a sign-in form until someone is signed in. Returns the signed-in
    display name, or None. `users` maps a lowercase name to a password and
    comes from the LEAVE_USERS table in Streamlit secrets."""
    current = st.session_state.get("leave_user")
    if current:
        return current
    if not users:
        st.warning(
            "Leave log sign-in isn't set up yet. Add a [LEAVE_USERS] table (name = \"password\" for "
            "Erwin, Weng and Kristine) in Streamlit secrets - see .streamlit/secrets.toml.example."
        )
        return None
    st.info("The leave log is limited to Erwin, Weng and Kristine. Sign in to view or change it.")
    with st.form("leave_login"):
        name = st.selectbox("Name", sorted(users), format_func=str.title)
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        expected = str(users.get(name, ""))
        if expected and hmac.compare_digest(password.encode(), expected.encode()):
            st.session_state["leave_user"] = name.title()
            st.rerun()
        time.sleep(1)  # slows down password guessing
        st.error("Wrong password.")
    return None


# -------------------------------------------------------------- leave tab --
def _persist(action: Callable[[], dict], done_msg: str, notion_cfg, invalidate: Callable[[], None]) -> bool:
    """Runs a Supabase write, copies the result to Notion (best effort), and
    leaves a one-line message for the next render. Safe inside a callback."""
    try:
        row = action()
    except leave_store.LeaveStoreError as e:
        st.session_state["leave_flash"] = ("error", str(e))
        return False
    level, msg = "success", done_msg
    if notion_cfg:
        try:
            notion_backup.sync_row(notion_cfg[0], notion_cfg[1], row)
        except notion_backup.NotionBackupError as e:
            level = "warning"
            msg = f"{done_msg} The Notion backup copy didn't go through ({e}); the nightly sync will retry it."
    invalidate()
    st.session_state["leave_flash"] = (level, msg)
    return True


def _coverage_preview(leaves: list[dict], agent: str, rng, min_agents: int, ignore_id=None) -> None:
    rows = leave_mod.coverage_warnings(leaves, agent, rng[0], rng[1], min_agents, ignore_id=ignore_id)
    if not rows:
        st.success(f"Coverage check: no rostered hour would drop below {min_agents} agent(s).")
        return
    by_day: dict[date, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    lines = []
    for d, items in by_day.items():
        lowest = min(items, key=lambda r: r["remaining"])
        at = datetime(2000, 1, 1, lowest["hour"]).strftime("%-I %p")
        lines.append(f"{_fmt_day(d)}: {len(items)} hour(s) below {min_agents} (lowest is {lowest['remaining']} at {at})")
    st.warning("Coverage check: this leave would thin out the roster.\n\n" + "\n\n".join(lines)
               + "\n\nYou can still save it. The Forecast tab shows whether demand needs more than that.")
    with st.expander("Hour-by-hour detail"):
        st.dataframe(pd.DataFrame([{
            "Date": _fmt_day(r["date"]), "Hour": datetime(2000, 1, 1, r["hour"]).strftime("%-I %p"),
            "Agents left": r["remaining"], "Others also off": ", ".join(r["others_off"]) or "",
        } for r in rows]), width="stretch", hide_index=True)


def render_leave_tab(*, actor: str, store, notion_cfg, fetch_leaves: Callable[[bool], list[dict]],
                     fetch_audit: Callable[[], list[dict]], invalidate: Callable[[], None]) -> None:
    today = datetime.now(LOCAL_TZ).date()
    head_l, head_r = st.columns([4, 1])
    head_l.markdown(f"Signed in as **{actor}**. Erwin, Weng and Kristine all have the same access.")
    if head_r.button("Sign out", key="leave_signout"):
        st.session_state.pop("leave_user", None)
        st.rerun()

    flash = st.session_state.pop("leave_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])

    try:
        active = fetch_leaves(False)
    except leave_store.LeaveStoreError as e:
        st.error(str(e))
        return

    # ---------------------------------------------------------- add a leave --
    st.subheader("Log an approved leave")
    st.caption(
        "Only log leaves that are already approved. Every leave logged here lowers the number of agents "
        "the forecast counts for those dates. Dates are calendar dates in US Central time. A shift that "
        "runs past midnight shows up in the roster on two dates, so log both dates for an overnight shift."
    )
    c1, c2 = st.columns([1, 2])
    agent = c1.selectbox("Agent", sorted(roster.ALL_AGENT_CODES), index=None, placeholder="Choose an agent",
                         key="lv_add_agent")
    picked = c2.date_input("First and last day off (pick the same day twice for one day)",
                           value=(today, today), key="lv_add_dates")
    c3, c4 = st.columns([2, 1])
    note = c3.text_input("Note (optional)", key="lv_add_note", max_chars=200)
    min_agents = c4.number_input("Warn if fewer agents than this are rostered in an hour", min_value=0, max_value=20,
                                 value=2, step=1, key="lv_min_agents")
    rng = _range(picked)

    blocking: list[str] = []
    if agent and rng:
        blocking += leave_mod.validate(agent, rng[0], rng[1])
        for ov in leave_mod.find_overlaps(active, agent, rng[0], rng[1]):
            blocking.append(f"{agent} already has a leave {_fmt_day(ov['start_date'])} to {_fmt_day(ov['end_date'])}. "
                            "Edit that one instead.")
        for msg in blocking:
            st.error(msg)
        if not blocking:
            _coverage_preview(active, agent, rng, int(min_agents))
    elif agent and not rng:
        st.caption("Pick the last day off to finish the range.")

    def _save_new() -> None:
        a, d, n = st.session_state.get("lv_add_agent"), _range(st.session_state.get("lv_add_dates")), \
            st.session_state.get("lv_add_note", "")
        if not a or not d:
            return
        ok = _persist(lambda: store.add(a, d[0], d[1], n, actor),
                      f"Logged leave for {a}, {_fmt_day(d[0])} to {_fmt_day(d[1])}.", notion_cfg, invalidate)
        if ok:
            st.session_state["lv_add_agent"] = None
            st.session_state["lv_add_note"] = ""

    st.button("Save leave", type="primary", on_click=_save_new,
              disabled=bool(blocking) or not agent or not rng, key="lv_save_new")

    # ----------------------------------------------- current and upcoming --
    st.subheader("Current and upcoming leaves")
    current = [lv for lv in active if leave_mod.to_date(lv["end_date"]) >= today]
    if not current:
        st.caption("No current or upcoming leaves logged.")
    else:
        st.dataframe(pd.DataFrame([{
            "Agent": lv["agent_code"], "From": _fmt_day(lv["start_date"]), "To": _fmt_day(lv["end_date"]),
            "Days": (leave_mod.to_date(lv["end_date"]) - leave_mod.to_date(lv["start_date"])).days + 1,
            "Note": lv.get("note") or "", "Logged by": lv.get("created_by") or "",
            "Last changed by": lv.get("updated_by") or "",
        } for lv in current]), width="stretch", hide_index=True)

        by_id = {str(lv["id"]): lv for lv in current}
        chosen = st.selectbox(
            "Edit or cancel a leave", list(by_id), index=None, placeholder="Choose a leave",
            format_func=lambda i: f"{by_id[i]['agent_code']}: {_fmt_day(by_id[i]['start_date'])} to "
                                  f"{_fmt_day(by_id[i]['end_date'])}",
            key="lv_edit_pick",
        )
        if chosen:
            lv = by_id[chosen]
            e1, e2 = st.columns([1, 2])
            codes = sorted(roster.ALL_AGENT_CODES)
            e_agent = e1.selectbox("Agent", codes, index=codes.index(lv["agent_code"]) if lv["agent_code"] in codes else 0,
                                   key=f"lv_e_agent_{chosen}")
            e_dates = e2.date_input("First and last day off",
                                    value=(leave_mod.to_date(lv["start_date"]), leave_mod.to_date(lv["end_date"])),
                                    key=f"lv_e_dates_{chosen}")
            e_note = st.text_input("Note", value=lv.get("note") or "", key=f"lv_e_note_{chosen}", max_chars=200)
            e_rng = _range(e_dates)
            e_block: list[str] = []
            if e_rng:
                e_block += leave_mod.validate(e_agent, e_rng[0], e_rng[1])
                for ov in leave_mod.find_overlaps(active, e_agent, e_rng[0], e_rng[1], ignore_id=chosen):
                    e_block.append(f"{e_agent} already has a leave {_fmt_day(ov['start_date'])} to "
                                   f"{_fmt_day(ov['end_date'])}.")
                for msg in e_block:
                    st.error(msg)
                if not e_block:
                    _coverage_preview(active, e_agent, e_rng, int(min_agents), ignore_id=chosen)

            def _save_edit(i=chosen) -> None:
                a = st.session_state.get(f"lv_e_agent_{i}")
                d = _range(st.session_state.get(f"lv_e_dates_{i}"))
                n = st.session_state.get(f"lv_e_note_{i}", "")
                if not a or not d:
                    return
                _persist(lambda: store.update(i, a, d[0], d[1], n, actor),
                         f"Updated leave for {a}, {_fmt_day(d[0])} to {_fmt_day(d[1])}.", notion_cfg, invalidate)

            def _cancel(i=chosen) -> None:
                row = by_id[i]
                if _persist(lambda: store.cancel(i, actor),
                            f"Cancelled leave for {row['agent_code']}, {_fmt_day(row['start_date'])} to "
                            f"{_fmt_day(row['end_date'])}. It stays in the change history.", notion_cfg, invalidate):
                    st.session_state["lv_edit_pick"] = None

            b1, b2, _ = st.columns([1, 1, 3])
            b1.button("Save changes", on_click=_save_edit, disabled=bool(e_block) or not e_rng, key="lv_save_edit")
            confirm = b2.checkbox("Confirm cancel", key=f"lv_confirm_{chosen}")
            b2.button("Cancel this leave", on_click=_cancel, disabled=not confirm, key="lv_cancel_btn")

    # ------------------------------------------------------ history + backup --
    with st.expander("Past leaves (last 90 days)"):
        past = [lv for lv in active if leave_mod.to_date(lv["end_date"]) < today
                and leave_mod.to_date(lv["end_date"]) >= today - timedelta(days=90)]
        if past:
            st.dataframe(pd.DataFrame([{
                "Agent": lv["agent_code"], "From": _fmt_day(lv["start_date"]), "To": _fmt_day(lv["end_date"]),
                "Note": lv.get("note") or "", "Logged by": lv.get("created_by") or "",
            } for lv in sorted(past, key=lambda x: str(x["start_date"]), reverse=True)]),
                width="stretch", hide_index=True)
        else:
            st.caption("None.")

    with st.expander("Change history (who added, edited or cancelled what, and when)"):
        try:
            audit = fetch_audit()
        except leave_store.LeaveStoreError as e:
            st.error(str(e))
            audit = []
        if audit:
            rows = []
            for a in audit:
                snap = a.get("after") or {}
                when = (pd.to_datetime(a["at"], utc=True).tz_convert(LOCAL_TZ).strftime("%b %-d, %-I:%M %p")
                        if a.get("at") else "")
                rows.append({
                    "When (Central)": when, "Who": a.get("actor") or "", "Action": str(a["action"]).title(),
                    "Agent": snap.get("agent_code", ""),
                    "Dates": f"{_fmt_day(snap['start_date'])} to {_fmt_day(snap['end_date'])}" if snap.get("start_date") else "",
                    "Note": snap.get("note") or "",
                })
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.caption("No changes recorded yet.")

    st.subheader("Notion backup")
    if notion_cfg:
        st.caption(
            "Each save is copied to the Notion backup database right away. A nightly job also re-copies "
            "everything, so a copy that failed gets caught up. Notion is a one-way backup: editing a row there "
            "does not change the forecast."
        )
        if st.button("Back up everything to Notion now", key="lv_notion_all"):
            with st.spinner("Copying to Notion..."):
                try:
                    counts = notion_backup.sync_all(notion_cfg[0], notion_cfg[1], fetch_leaves(True))
                    st.success(f"Done: {counts['created']} added, {counts['updated']} updated, "
                               f"{counts['unchanged']} already up to date, {counts['failed']} failed.")
                    for err in counts["errors"]:
                        st.error(err)
                except (notion_backup.NotionBackupError, leave_store.LeaveStoreError) as e:
                    st.error(str(e))
    else:
        st.info("Notion backup isn't configured. Add NOTION_TOKEN and NOTION_LEAVE_DB_ID in Streamlit secrets "
                "to turn it on. Leaves are still saved in Supabase either way.")


# ----------------------------------------------------------- forecast tab --
def render_forecast_tab(*, load_history: Callable[[datetime, datetime], pd.DataFrame],
                        load_actual: Callable[[datetime, datetime], tuple], lmap: dict[date, set[str]],
                        leave_connected: bool, sla_target_rate: float, slack_on: bool,
                        show_names: bool) -> None:
    today = datetime.now(LOCAL_TZ).date()
    st.caption(
        f"Looks ahead using the last {FORECAST_HISTORY_DAYS} days of ticket volume and the roster, "
        "minus every approved leave in the leave log."
        + ("" if leave_connected else " The leave log isn't connected, so this forecast ignores leave.")
    )

    c1, c2, c3, c4 = st.columns(4)
    start = c1.date_input("Forecast from", value=today, key="fc_start")
    days = c2.selectbox("Days ahead", [7, 14, 28], index=1, key="fc_days")
    level = c3.selectbox("Planning level", list(fc_mod.PLANNING_LEVELS), key="fc_level",
                         help="Average plans for a typical day. Busy plans for the 75th-percentile day.")
    adjust = c4.number_input("Volume adjustment %", min_value=-50, max_value=100, value=0, step=5, key="fc_adjust",
                             help="Scales expected tickets, for example +10 for a promo or holiday week.")
    use_attendance = st.checkbox(
        "Scale scheduled agents by typical attendance from Slack", value=False, key="fc_attendance",
        disabled=not slack_on,
        help="Scheduled agents are not all online every minute (breaks, late logins). This scales the roster by "
             "the share that was actually online in the history window. Needs Slack attendance.",
    )

    if st.button("Build forecast", type="primary", key="fc_build"):
        st.session_state["fc_built"] = True
    if not st.session_state.get("fc_built"):
        st.info(f"Click Build forecast to load {FORECAST_HISTORY_DAYS} days of history. The first load takes a "
                "couple of minutes, then it is cached for 30 minutes.")
        return

    end_local = datetime.combine(today, datetime.min.time(), tzinfo=LOCAL_TZ)
    start_local = end_local - timedelta(days=FORECAST_HISTORY_DAYS)
    utc = ZoneInfo("UTC")
    hist = load_history(start_local.astimezone(utc), end_local.astimezone(utc))
    if hist is None or hist.empty:
        st.info("No ticket history came back, so there is nothing to forecast from.")
        return

    adf, _ = load_actual(start_local.astimezone(utc), end_local.astimezone(utc))
    if adf is not None and not adf.empty:
        hist = hist.merge(adf[["hour", "actual_online"]], on="hour", how="left")
    else:
        hist["actual_online"] = pd.NA
    hist = leave_mod.annotate_hourly(hist, lmap)
    hist["effective_agents"] = hist["actual_online"].where(hist["actual_online"].notna(), hist["scheduled_after_leave"])
    capacity, estimated = recommend.estimate_capacity(hist, sla_target_rate)

    attendance = 1.0
    if use_attendance and slack_on:
        factor = fc_mod.attendance_factor(hist)
        if factor is None:
            st.warning("No Slack attendance in the history window, so attendance scaling was skipped.")
        else:
            attendance = factor

    baseline = fc_mod.baseline_by_weekday_hour(hist, level)
    fc = fc_mod.build_forecast(baseline, start, int(days), lmap, capacity, float(adjust), attendance)
    summary = fc_mod.daily_summary(fc)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Expected tickets", f"{int(round(fc['forecast_tickets'].sum())):,}")
    m2.metric("Agent-hours on leave", int(fc["on_leave"].sum()))
    m3.metric("Understaffed hours", int((fc["recommendation"] == "Increase").sum()))
    m4.metric("Short only because of leave", int(fc["leave_caused"].sum()),
              help="Hours that are understaffed with leave applied but would have been fine on the roster alone.")
    st.caption(
        f"Capacity used: {capacity:.1f} tickets per agent per hour "
        + ("(estimated from hours in the history window that met your SLA target)." if estimated else
           "(default placeholder: not enough SLA-passing hours in the history window to estimate yet).")
        + (f" Attendance scaling: x{attendance:.2f}." if attendance != 1.0 else "")
    )

    st.subheader("Day by day")
    table = summary.assign(Date=summary["date"].map(_fmt_day))
    cols = {"Date": "Date", "forecast_tickets": "Expected tickets", "agent_hours_on_leave": "Agent-hours on leave",
            "understaffed_hours": "Understaffed hours", "understaffed_due_to_leave": "Short due to leave",
            "worst_gap": "Worst gap (agents)"}
    if show_names:
        cols["agents_on_leave"] = "On leave"
    shown = table[list(cols)].rename(columns=cols)
    shown["Expected tickets"] = shown["Expected tickets"].round(0).astype(int)
    shown["Worst gap (agents)"] = shown["Worst gap (agents)"].round(1)
    st.dataframe(shown, width="stretch", hide_index=True)
    if not show_names:
        st.caption("Names of agents on leave are shown to people signed in on the Leave log tab.")

    st.subheader("Where the gaps are")
    pivot = fc.pivot_table(index="date", columns="hour_of_day", values="gap")
    labels = [_fmt_day(d) for d in pivot.index]
    fig_heat = go.Figure(data=go.Heatmap(
        z=pivot.values, x=[datetime(2000, 1, 1, h).strftime("%-I %p") for h in pivot.columns], y=labels,
        zmin=-4, zmax=4, zmid=0,
        colorscale=[[0, "#256abf"], [0.5, "#ffffff"], [1, style.STATUS_CRITICAL]],
        colorbar=dict(title="Agents short (+)<br>or spare (-)"),
        hovertemplate="%{y} %{x}<br>gap %{z:.1f}<extra></extra>",
    ))
    fig_heat.update_layout(height=max(260, 24 * len(labels) + 80), margin=dict(t=20, b=10),
                           yaxis=dict(autorange="reversed"))
    st.plotly_chart(fig_heat, width="stretch", key="fc_heatmap")
    st.caption("Red hours need more agents than the roster has after leave. Blue hours have spare agents. "
               "Each cell is needed minus available for that hour.")

    st.subheader("One day in detail")
    pick = st.selectbox("Day", list(summary["date"]), format_func=_fmt_day, key="fc_day_pick")
    day = fc[fc["date"] == pick]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=day["hour_label"], y=day["forecast_tickets"], mode="lines", fill="tozeroy",
                             line=dict(color=style.SERIES_TICKETS, width=1), fillcolor="rgba(42, 120, 214, 0.15)",
                             name="Expected tickets (right axis)", yaxis="y2"))
    fig.add_bar(x=day["hour_label"], y=day["roster_agents"], marker_color=style.SERIES_AGENTS,
                name="Roster (before leave)")
    fig.add_trace(go.Scatter(x=day["hour_label"], y=day["available_agents"], mode="lines+markers",
                             line=dict(color=style.SERIES_ACTUAL, width=2), marker=dict(size=7),
                             name="Available after leave"))
    fig.add_trace(go.Scatter(x=day["hour_label"], y=day["required_agents"], mode="lines+markers",
                             line=dict(color=style.TEXT_SECONDARY, dash="dot", width=2), marker=dict(size=7),
                             name="Agents needed"))
    fig.update_layout(
        title=dict(text=f"{_fmt_day(pick)}: roster, leave and demand", y=0.97, yanchor="top"), height=320,
        margin=dict(t=90, b=10), plot_bgcolor="white",
        legend=dict(orientation="h", y=1.1, yanchor="bottom", x=0.5, xanchor="center"),
        yaxis=dict(title="Agents"),
        yaxis2=dict(title="Tickets", overlaying="y", side="right", showgrid=False, rangemode="tozero"),
    )
    st.plotly_chart(fig, width="stretch", key="fc_day_chart")

    leave_hours = fc[fc["leave_caused"]]
    if not leave_hours.empty:
        with st.expander(f"{len(leave_hours)} hour(s) that are short only because of leave"):
            detail = leave_hours.assign(Date=leave_hours["date"].map(_fmt_day))
            keep = {"Date": "Date", "hour_label": "Hour", "forecast_tickets": "Expected tickets",
                    "required_agents": "Needed", "available_agents": "Available"}
            if show_names:
                keep["on_leave_names"] = "On leave"
            out = detail[list(keep)].rename(columns=keep)
            out["Expected tickets"] = out["Expected tickets"].round(1)
            out["Available"] = out["Available"].round(1)
            st.dataframe(out, width="stretch", hide_index=True)

    with st.expander("How this forecast works"):
        st.write(
            "Expected tickets for each weekday and hour come from the same weekday-hour over the last "
            f"{FORECAST_HISTORY_DAYS} days (average, or the 75th percentile at the Busy level), scaled by the "
            "volume adjustment. Available agents start from the roster template and drop by one for every "
            "rostered agent on approved leave that date. Agents needed is expected tickets divided by the "
            "capacity estimate, rounded up. A gap means needed minus available. It is a planning aid: it does not "
            "model growth, holidays or incidents unless you set the volume adjustment."
        )
