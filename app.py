"""ZEVO Support Staffing Dashboard.

Shows, per hour: ticket volume, scheduled agents, and whether the 15-minute
first-response goal is being met - with a plain Increase/Sufficient/Decrease
staffing recommendation. Includes a weekly/monthly trend view.

Data sources:
  - Ticket volume + response times: Intercom (live, via REST API)
  - Scheduled agents: data/roster_schedule.csv (from the ZEVO Workforce sheet)
  - Actual online agents (optional): #secret-cc-cafe on Slack (live, via REST
    API) - reconstructed from login/break/back/logout messages, shown
    alongside the planned roster rather than replacing it
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, time as dtime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from zoneinfo import ZoneInfo

import attendance
import roster
import style
from attendance import SlackAuthError
from intercom_client import IntercomAuthError
from pipeline import fetch_hourly_table
from recommend import add_recommendations

LOCAL_TZ = ZoneInfo("America/Chicago")
DEFAULT_SLACK_CHANNEL = "C0A95GPM9FX"  # #secret-cc-cafe

st.set_page_config(page_title="ZEVO Support Staffing", page_icon="\U0001F4DE", layout="wide")


def _secret(name: str) -> str | None:
    try:
        val = st.secrets.get(name)
    except Exception:
        val = None
    return val or os.environ.get(name)


def get_token() -> str | None:
    return _secret("INTERCOM_ACCESS_TOKEN")


def get_slack_config() -> tuple[str | None, str]:
    """Returns (bot_token, channel_id). bot_token is None if not configured -
    the Slack attendance series is entirely optional; the app runs fine
    without it, just without the "actual online" comparison."""
    return _secret("SLACK_BOT_TOKEN"), (_secret("SLACK_CHANNEL_ID") or DEFAULT_SLACK_CHANNEL)


@st.cache_data(ttl=1800, show_spinner="Pulling conversations from Intercom...")
def load_hourly(token: str, start_utc: datetime, end_utc: datetime, sla_seconds: int) -> pd.DataFrame:
    return fetch_hourly_table(token, start_utc, end_utc, sla_seconds=sla_seconds)


@st.cache_data(ttl=1800, show_spinner="Reading #secret-cc-cafe on Slack...")
def load_actual(token: str, channel_id: str, start_utc: datetime, end_utc: datetime):
    return attendance.fetch_actual_online_table(token, channel_id, start_utc, end_utc)


def local_day_bounds_utc(local_date) -> tuple[datetime, datetime]:
    start_local = datetime.combine(local_date, dtime.min, tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


# ---------------------------------------------------------------- sidebar --
st.sidebar.title("Settings")
token = get_token()
if token:
    st.sidebar.success("Intercom token loaded")
else:
    st.sidebar.error("No Intercom token found. Add INTERCOM_ACCESS_TOKEN in Streamlit secrets.")

slack_token, slack_channel = get_slack_config()
if slack_token:
    st.sidebar.success("Slack attendance loaded (#secret-cc-cafe)")
else:
    st.sidebar.info(
        "Slack attendance not configured. Add SLACK_BOT_TOKEN in Streamlit secrets to compare "
        "actual online agents against the planned roster."
    )

sla_minutes = st.sidebar.number_input("First-response SLA target (minutes)", min_value=1, value=15, step=1)
sla_target_rate = st.sidebar.slider("Minimum acceptable SLA hit-rate", 0.5, 1.0, 0.90, 0.01,
                                     help="Below this % of tickets answered within the SLA, an hour is flagged as understaffed.")
st.sidebar.caption(
    "'Scheduled agents' comes from the ZEVO Workforce roster (recurring weekly template), "
    "not real-time clock-ins. 'Actual online' (when configured) is reconstructed from Slack "
    "login/break/back/logout messages. Ticket data is live from Intercom. Staffing "
    "recommendations and 'hours understaffed' are based on actual online agents for any "
    "hour Slack has data for, and fall back to the roster only when it doesn't - the "
    "roster isn't updated daily and misses leave, no-shows, and off-day coverage."
)


def load_actual_safe(start_utc: datetime, end_utc: datetime):
    """Returns (df_or_None, unmatched_user_ids). Never raises - Slack attendance
    is a supplementary series, so a Slack-side problem shouldn't take down the
    Intercom/roster half of the app."""
    if not slack_token:
        return None, []
    try:
        adf, ameta = load_actual(slack_token, slack_channel, start_utc, end_utc)
        return adf, ameta.get("unmatched_slack_user_ids", [])
    except SlackAuthError as e:
        st.sidebar.warning(f"Slack attendance error: {e}")
        return None, []

st.title("Support Staffing Dashboard")
tab_daily, tab_trends = st.tabs(["Daily staffing", "Trends"])

# ------------------------------------------------------------- daily tab --
with tab_daily:
    picked_date = st.date_input("Date", value=datetime.now(LOCAL_TZ).date())
    if not token:
        st.stop()

    start_utc, end_utc = local_day_bounds_utc(picked_date)
    try:
        df = load_hourly(token, start_utc, end_utc, int(sla_minutes * 60))
    except IntercomAuthError as e:
        st.error(str(e))
        st.stop()

    if df.empty:
        st.info("No data for this date.")
    else:
        df["hour_label"] = df["hour"].dt.strftime("%-I %p")

        adf, unmatched = load_actual_safe(start_utc, end_utc)
        if adf is not None and not adf.empty:
            df = df.merge(adf[["hour", "actual_online"]], on="hour", how="left")
        else:
            df["actual_online"] = pd.NA

        # Actual online (Slack) is the primary basis for headcount when we have
        # it for an hour - the roster is a template that doesn't get updated
        # daily and misses leave, no-shows, or someone covering an off day.
        # Falls back to the roster only for hours with no Slack data at all.
        df["effective_agents"] = df["actual_online"].where(df["actual_online"].notna(), df["scheduled_agents"])
        df, meta = add_recommendations(df, sla_target=sla_target_rate)

        if unmatched:
            with st.expander(f"{len(unmatched)} Slack poster(s) in #secret-cc-cafe couldn't be matched to a roster agent"):
                st.write(
                    "These Slack user IDs posted in the channel but don't match exactly one name in "
                    "the roster, so they're excluded from 'actual online' rather than guessed at: "
                    + ", ".join(unmatched)
                )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tickets", int(df["tickets"].sum()))
        overall_hit = df["sla_met"].sum() / df["human_handled"].sum() if df["human_handled"].sum() else None
        c2.metric("SLA hit-rate", f"{overall_hit:.0%}" if overall_hit is not None else "n/a")
        c3.metric("Hours understaffed", int((df["recommendation"] == "Increase").sum()),
                  help="Based on actual online agents (Slack) for hours that have that data, "
                       "the scheduled roster otherwise.")
        c4.metric("Est. capacity/agent/hr", f"{meta['capacity_tickets_per_agent']:.1f}"
                   + ("" if meta["capacity_estimated_from_data"] else " (default, low data)"))

        fig_tickets = go.Figure()
        fig_tickets.add_bar(x=df["hour_label"], y=df["tickets"], marker_color=style.SERIES_TICKETS, name="Tickets")
        fig_tickets.update_layout(title="Tickets per hour", height=280, margin=dict(t=40, b=10),
                                   plot_bgcolor="white", showlegend=False)
        st.plotly_chart(fig_tickets, width="stretch")

        fig_agents = go.Figure()
        fig_agents.add_bar(x=df["hour_label"], y=df["scheduled_agents"], marker_color=style.SERIES_AGENTS, name="Scheduled agents")
        if df["actual_online"].notna().any():
            fig_agents.add_trace(go.Scatter(x=df["hour_label"], y=df["actual_online"], mode="lines+markers",
                                             line=dict(color=style.SERIES_ACTUAL), name="Actual online (Slack)"))
        fig_agents.add_trace(go.Scatter(x=df["hour_label"], y=df["required_agents"], mode="lines+markers",
                                         line=dict(color=style.TEXT_SECONDARY, dash="dot"), name="Agents needed"))
        fig_agents.update_layout(
            title=dict(text="Scheduled vs. actual vs. needed agents per hour", y=0.97, yanchor="top"),
            height=300, margin=dict(t=90, b=10), plot_bgcolor="white",
            legend=dict(orientation="h", y=1.1, yanchor="bottom", x=0.5, xanchor="center"))
        st.plotly_chart(fig_agents, width="stretch")
        if df["actual_online"].notna().any():
            st.caption(
                "The recommendation strip below judges each hour against actual online agents "
                "(Slack) where available, and the scheduled roster for any hour Slack has no data for."
            )

        rec_colors = df["recommendation"].map(style.RECOMMENDATION_COLORS)
        fig_rec = go.Figure()
        fig_rec.add_bar(x=df["hour_label"], y=[1] * len(df), marker_color=rec_colors,
                         hovertext=df["recommendation"], hoverinfo="text")
        fig_rec.update_layout(title="Staffing recommendation by hour", height=120, margin=dict(t=40, b=10),
                               plot_bgcolor="white", showlegend=False, yaxis=dict(visible=False))
        st.plotly_chart(fig_rec, width="stretch")

        st.subheader("Hour-by-hour detail")
        show = df[["hour_label", "tickets", "scheduled_agents", "actual_online", "effective_agents",
                    "required_agents", "agent_gap", "sla_hit_rate", "recommendation"]].rename(columns={
            "hour_label": "Hour", "tickets": "Tickets", "scheduled_agents": "Scheduled",
            "actual_online": "Actual (Slack)", "effective_agents": "Effective (basis)",
            "required_agents": "Needed", "agent_gap": "Gap",
            "sla_hit_rate": "SLA hit-rate", "recommendation": "Recommendation",
        })
        show["SLA hit-rate"] = show["SLA hit-rate"].map(lambda x: f"{x:.0%}" if pd.notna(x) else "–")
        st.dataframe(show, width="stretch", hide_index=True)
        st.caption(
            "'Effective (basis)' is whichever number the recommendation actually used for that "
            "hour: actual online when Slack has it, the scheduled roster when it doesn't."
        )

# ------------------------------------------------------------ trends tab --
with tab_trends:
    if not token:
        st.stop()
    window = st.radio("Window", ["Last 7 days", "Last 30 days", "Last 90 days"], horizontal=True, index=1)
    days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}[window]

    end_local = datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=days)
    start_utc, end_utc = start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))

    try:
        tdf = load_hourly(token, start_utc, end_utc, int(sla_minutes * 60))
    except IntercomAuthError as e:
        st.error(str(e))
        st.stop()

    if tdf.empty:
        st.info("No data in this window.")
    else:
        tdf["date"] = tdf["hour"].dt.date
        tdf["day_name"] = tdf["hour"].dt.strftime("%A")
        tdf["hour_of_day"] = tdf["hour"].dt.hour

        tadf, _ = load_actual_safe(start_utc, end_utc)
        if tadf is not None and not tadf.empty:
            tdf = tdf.merge(tadf[["hour", "actual_online"]], on="hour", how="left")
        else:
            tdf["actual_online"] = pd.NA

        # Same precedence as the daily tab: actual online (Slack) drives the
        # recommendation when we have it for an hour, roster otherwise.
        tdf["effective_agents"] = tdf["actual_online"].where(tdf["actual_online"].notna(), tdf["scheduled_agents"])
        tdf, tmeta = add_recommendations(tdf, sla_target=sla_target_rate)

        daily = tdf.groupby("date").agg(
            tickets=("tickets", "sum"),
            sla_met=("sla_met", "sum"),
            human_handled=("human_handled", "sum"),
            understaffed_hours=("recommendation", lambda s: (s == "Increase").sum()),
            avg_scheduled=("scheduled_agents", "mean"),
            avg_actual=("actual_online", "mean"),
        ).reset_index()
        daily["sla_hit_rate"] = daily.apply(lambda r: r.sla_met / r.human_handled if r.human_handled else None, axis=1)

        fig_vol = go.Figure()
        fig_vol.add_bar(x=daily["date"], y=daily["tickets"], marker_color=style.SERIES_TICKETS)
        fig_vol.update_layout(title="Daily ticket volume", height=280, margin=dict(t=40, b=10), plot_bgcolor="white")
        st.plotly_chart(fig_vol, width="stretch")

        fig_sla = go.Figure()
        fig_sla.add_trace(go.Scatter(x=daily["date"], y=daily["sla_hit_rate"], mode="lines+markers",
                                      line=dict(color=style.SERIES_TICKETS)))
        fig_sla.add_hline(y=sla_target_rate, line_dash="dot", line_color=style.TEXT_MUTED,
                           annotation_text="target")
        fig_sla.update_layout(title=f"Daily {int(sla_minutes)}-min SLA hit-rate", height=280,
                               margin=dict(t=40, b=10), plot_bgcolor="white", yaxis_tickformat=".0%")
        st.plotly_chart(fig_sla, width="stretch")

        fig_under = go.Figure()
        fig_under.add_bar(x=daily["date"], y=daily["understaffed_hours"], marker_color=style.STATUS_CRITICAL)
        fig_under.update_layout(title="Understaffed hours per day", height=240, margin=dict(t=40, b=10),
                                 plot_bgcolor="white")
        st.plotly_chart(fig_under, width="stretch")
        if tdf["actual_online"].notna().any():
            st.caption(
                "Counts hours judged against actual online agents (Slack) where available, "
                "the scheduled roster otherwise - same basis as the Daily staffing tab."
            )

        if daily["avg_actual"].notna().any():
            fig_staffing = go.Figure()
            fig_staffing.add_bar(x=daily["date"], y=daily["avg_scheduled"], marker_color=style.SERIES_AGENTS,
                                  name="Avg. scheduled")
            fig_staffing.add_trace(go.Scatter(x=daily["date"], y=daily["avg_actual"], mode="lines+markers",
                                               line=dict(color=style.SERIES_ACTUAL), name="Avg. actual online"))
            fig_staffing.update_layout(
                title=dict(text="Daily avg. scheduled vs. actual online agents", y=0.97, yanchor="top"),
                height=280, margin=dict(t=90, b=10), plot_bgcolor="white",
                legend=dict(orientation="h", y=1.1, yanchor="bottom", x=0.5, xanchor="center"))
            st.plotly_chart(fig_staffing, width="stretch")

        st.subheader("Average ticket volume by day-of-week / hour")
        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        pivot = tdf.pivot_table(index="day_name", columns="hour_of_day", values="tickets", aggfunc="mean")
        pivot = pivot.reindex(day_order)
        fig_heat = go.Figure(data=go.Heatmap(
            z=pivot.values, x=pivot.columns, y=pivot.index,
            colorscale=[[i / 6, c] for i, c in enumerate(style.SEQUENTIAL_BLUE)],
            colorbar=dict(title="avg tickets"),
        ))
        fig_heat.update_layout(height=320, margin=dict(t=20, b=10))
        st.plotly_chart(fig_heat, width="stretch")

        st.caption(
            f"Implied sustainable capacity: ~{tmeta['capacity_tickets_per_agent']:.1f} tickets/agent/hour "
            + ("(estimated from hours that met the SLA target in this window)."
               if tmeta["capacity_estimated_from_data"] else
               "(default placeholder - not enough SLA-passing hours in this window to estimate yet).")
        )
