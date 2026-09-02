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
from pipeline import fetch_hourly_table, fetch_hourly_ticket_activity
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


@st.cache_data(ttl=1800, show_spinner="Pulling per-ticket work history from Intercom...")
def load_ticket_activity(token: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
    return fetch_hourly_ticket_activity(token, start_utc, end_utc)


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


def load_ticket_activity_safe(start_utc: datetime, end_utc: datetime):
    """Returns a per-hour created/worked_on/closed DataFrame, or None if it
    couldn't be loaded. This does one Intercom GET per ticket created in the
    window - much heavier than everything else on this tab - so a hiccup
    here shouldn't take down the cheaper, more important data above it."""
    try:
        return load_ticket_activity(token, start_utc, end_utc)
    except IntercomAuthError as e:
        st.sidebar.warning(f"Ticket work-history error: {e}")
        return None
    except Exception as e:
        st.warning(f"Couldn't load per-ticket work history (Created/Worked on/Closed columns): {e}")
        return None


def render_daily_view(picked_date, key_prefix: str = "daily") -> None:
    """Renders the full daily-staffing view (metrics, explanatory expanders,
    the per-hour charts, the recommendation strip, and the hour-by-hour
    table) for one specific local date. Shared by the Daily staffing tab
    (whichever date is picked there) and the Trends tab's 'Yesterday' option
    - 'Yesterday' shows this same hour-by-hour detail rather than an
    aggregate multi-day view, since there's only one day to show.

    key_prefix disambiguates the widget/chart keys between the two callers -
    both run in the same script pass, so if the Daily tab's picked date and
    Trends' "Yesterday" happen to be the same date, matching keys alone
    would collide."""
    start_utc, end_utc = local_day_bounds_utc(picked_date)
    try:
        df = load_hourly(token, start_utc, end_utc, int(sla_minutes * 60))
    except IntercomAuthError as e:
        st.error(str(e))
        return

    if df.empty:
        st.info("No data for this date.")
        return

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

    # Loaded here (rather than down by the Hour-by-hour table) so the
    # worked_on/closed columns are also available to the "Which hours are
    # flagged" table above - worked_on in particular helps tell a genuine
    # coverage gap apart from an hour where agents were tied up on
    # carryover tickets from earlier hours.
    activity_df = load_ticket_activity_safe(start_utc, end_utc)
    if activity_df is not None and not activity_df.empty:
        df = df.merge(activity_df[["hour", "worked_on", "closed"]], on="hour", how="left")
    else:
        df["worked_on"] = pd.NA
        df["closed"] = pd.NA

    if unmatched:
        with st.expander(f"{len(unmatched)} Slack poster(s) in #secret-cc-cafe couldn't be matched to a roster agent"):
            st.write(
                "These Slack user IDs posted in the channel but don't match exactly one name in "
                "the roster, so they're excluded from 'actual online' rather than guessed at: "
                + ", ".join(unmatched)
            )

    tickets_total = int(df["tickets"].sum())
    human_handled_total = int(df["human_handled"].sum())
    fin_only_total = tickets_total - human_handled_total

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tickets", tickets_total)
    overall_hit = df["sla_met"].sum() / df["human_handled"].sum() if df["human_handled"].sum() else None
    c2.metric("SLA hit-rate", f"{overall_hit:.0%}" if overall_hit is not None else "n/a")
    c3.metric("Hours understaffed", int((df["recommendation"] == "Increase").sum()),
              help="Based on actual online agents (Slack) for hours that have that data, "
                   "the scheduled roster otherwise.")
    c4.metric("Est. capacity/agent/hr", f"{meta['capacity_tickets_per_agent']:.1f}"
               + ("" if meta["capacity_estimated_from_data"] else " (default, low data)"))

    with st.expander(f"What's inside the {tickets_total} tickets?"):
        st.write(
            f"**{tickets_total} tickets** total today. Of those, **{fin_only_total}** were "
            f"resolved entirely by Fin AI - no human ever replied, so there was no 15-minute "
            f"clock to start or miss for them. The remaining **{human_handled_total}** needed "
            f"a human reply at some point - that's the group the SLA hit-rate expander below is "
            f"calculated from."
        )
        st.caption(
            "This is why the count in the SLA hit-rate explanation below won't match the "
            "'Tickets' number up top: Fin-only resolutions count toward ticket volume (someone "
            "still contacted support and got helped) but are excluded from the hit-rate, since "
            "no agent capacity was ever needed for them."
        )

    if overall_hit is not None:
        with st.expander(f"What does {overall_hit:.0%} SLA hit-rate mean?"):
            sla_met_total = int(df["sla_met"].sum())
            st.write(
                f"Of the **{human_handled_total} tickets today that needed a human reply** "
                f"(Fin AI resolutions that never needed one aren't counted), "
                f"**{sla_met_total}** got that first reply within your "
                f"**{int(sla_minutes)}-minute** target - that's the {overall_hit:.0%} above."
            )
            verdict = "at or above" if overall_hit >= sla_target_rate else "below"
            st.write(
                f"Your **Minimum acceptable SLA hit-rate** slider (sidebar) is set to "
                f"**{sla_target_rate:.0%}** - today's {overall_hit:.0%} is **{verdict}** that bar."
            )
            st.caption(
                "Three different numbers on this page say 'SLA' and answer three different "
                "questions. The first-response SLA target (minutes, sidebar) is the clock: did "
                "one ticket get a reply in time, yes or no. This SLA hit-rate is the scoreboard: "
                "of the tickets that needed a human, what percent passed that clock test. The "
                "'Minimum acceptable SLA hit-rate' slider is your own bar for that scoreboard "
                "number - it doesn't change the clock at all, it only decides how strict the "
                "recommendations below are about flagging an hour red. Example: 10 tickets "
                "needing a reply, 6 answered in time, is a 60% hit-rate for that stretch - that "
                "clears a 50% bar but fails a 90% one."
            )

    increase_df = df[df["recommendation"] == "Increase"]
    if not increase_df.empty:
        with st.expander(f"Which {len(increase_df)} hour(s) are flagged 'Increase'"):
            def _why_flagged(row) -> str:
                headcount_short = row["agent_gap"] > 0
                hit_rate_miss = pd.notna(row["sla_hit_rate"]) and row["sla_hit_rate"] < sla_target_rate
                if headcount_short and hit_rate_miss:
                    return "Short-staffed + hit-rate miss"
                if headcount_short:
                    return "Short-staffed"
                if hit_rate_miss:
                    return "Hit-rate miss (agents were sufficient)"
                return "–"

            increase_df = increase_df.copy()
            increase_df["why_flagged"] = increase_df.apply(_why_flagged, axis=1)
            increase_detail = increase_df[
                ["hour_label", "tickets", "worked_on", "effective_agents", "required_agents",
                 "agent_gap", "sla_hit_rate", "why_flagged"]
            ].rename(columns={
                "hour_label": "Hour", "tickets": "Created", "worked_on": "Worked on",
                "effective_agents": "Effective agents", "required_agents": "Needed",
                "agent_gap": "Gap", "sla_hit_rate": "SLA hit-rate", "why_flagged": "Why flagged",
            })
            increase_detail["Worked on"] = increase_detail["Worked on"].map(
                lambda x: int(x) if pd.notna(x) else "–"
            )
            increase_detail["SLA hit-rate"] = increase_detail["SLA hit-rate"].map(
                lambda x: f"{x:.0%}" if pd.notna(x) else "–"
            )
            st.dataframe(increase_detail, width="stretch", hide_index=True, key=f"{key_prefix}_increase_table_{picked_date}")
            st.caption(
                "'Why flagged' spells out which of the two triggers put the hour here: "
                "'Short-staffed' means effective agents came in below what the capacity "
                "estimate says that hour needed; 'Hit-rate miss (agents were sufficient)' "
                "means headcount was fine but the hour's own SLA hit-rate still missed your "
                "target above - having enough, or more than enough, agents online doesn't "
                "clear an hour on its own. A hit-rate-miss hour is usually not a coverage "
                "problem - it points to something else (replies bunched on a few agents, a "
                "complexity spike, a lag before new tickets got picked up). 'Created' vs "
                "'Worked on' can help tell that apart: 'Worked on' counts tickets (created "
                "today) that got a human reply during that hour, including carryover from "
                "earlier hours - a 'Worked on' count much higher than 'Created' suggests "
                "agents were tied up clearing backlog rather than sitting idle."
            )

    has_worked_on = bool(df["worked_on"].notna().any())

    fig_tickets = go.Figure()
    fig_tickets.add_bar(x=df["hour_label"], y=df["tickets"], marker_color=style.SERIES_TICKETS, name="Created")
    if has_worked_on:
        # Same axis, same unit (ticket count) as "Created" - a solid line
        # with visible markers rather than a second bar, so the two are
        # easy to tell apart at a glance without needing a second scale.
        fig_tickets.add_trace(go.Scatter(
            x=df["hour_label"], y=df["worked_on"], mode="lines+markers",
            line=dict(color=style.SERIES_WORKED_ON, width=2), marker=dict(size=7),
            name="Worked on",
        ))
    fig_tickets.update_layout(
        title="Tickets per hour: created vs. worked on", height=280,
        margin=dict(t=40, b=10), plot_bgcolor="white", showlegend=has_worked_on,
        legend=dict(orientation="h", y=1.15, yanchor="bottom", x=0.5, xanchor="center"),
        yaxis=dict(title="Tickets"),
    )
    st.plotly_chart(fig_tickets, width="stretch", key=f"{key_prefix}_tickets_chart_{picked_date}")
    if has_worked_on:
        st.caption(
            "'Worked on' counts tickets (created today) that got a human reply during that "
            "hour, including carryover from earlier hours. When it runs well above 'Created' "
            "for an hour, agents were busy clearing backlog rather than sitting idle - worth "
            "checking before assuming a low hit-rate that hour means too few agents."
        )

    fig_agents = go.Figure()
    # Tickets drawn first (so they sit behind the agent series), on their own
    # right-hand axis since agent headcount and ticket volume are different
    # units - lets you spot "the ticket spike is why more agents were needed"
    # right on this chart, without cross-referencing the ticket chart above.
    fig_agents.add_trace(go.Scatter(
        x=df["hour_label"], y=df["tickets"], mode="lines", fill="tozeroy",
        line=dict(color=style.SERIES_TICKETS, width=1),
        fillcolor="rgba(42, 120, 214, 0.15)", name="Created (right axis)", yaxis="y2",
    ))
    if has_worked_on:
        fig_agents.add_trace(go.Scatter(
            x=df["hour_label"], y=df["worked_on"], mode="lines+markers",
            line=dict(color=style.SERIES_WORKED_ON, dash="dash", width=2),
            marker=dict(size=4), name="Worked on (right axis)", yaxis="y2",
        ))
    fig_agents.add_bar(x=df["hour_label"], y=df["scheduled_agents"], marker_color=style.SERIES_AGENTS, name="Scheduled agents")
    if df["actual_online"].notna().any():
        fig_agents.add_trace(go.Scatter(x=df["hour_label"], y=df["actual_online"], mode="lines+markers",
                                         line=dict(color=style.SERIES_ACTUAL, width=2), marker=dict(size=7),
                                         name="Actual online (Slack)"))
    fig_agents.add_trace(go.Scatter(x=df["hour_label"], y=df["required_agents"], mode="lines+markers",
                                     line=dict(color=style.TEXT_SECONDARY, dash="dot", width=2),
                                     marker=dict(size=7), name="Agents needed"))
    fig_agents.update_layout(
        title=dict(text="Scheduled vs. actual vs. needed agents per hour", y=0.97, yanchor="top"),
        height=300, margin=dict(t=90, b=10), plot_bgcolor="white",
        legend=dict(orientation="h", y=1.1, yanchor="bottom", x=0.5, xanchor="center"),
        yaxis=dict(title="Agents"),
        yaxis2=dict(title="Tickets", overlaying="y", side="right", showgrid=False, rangemode="tozero"),
    )
    st.plotly_chart(fig_agents, width="stretch", key=f"{key_prefix}_agents_chart_{picked_date}")
    st.caption(
        "The light blue area is tickets created that hour (right-hand scale); the dashed yellow "
        "line, same axis, is tickets worked on that hour (including carryover from earlier "
        "hours) - line these up against the agent lines to see which hours' agent gaps track a "
        "volume or backlog spike versus a coverage gap with steady or even light ticket flow."
        + (" The recommendation strip below judges each hour against actual online agents "
           "(Slack) where available, and the scheduled roster for any hour Slack has no data for."
           if df["actual_online"].notna().any() else "")
    )

    rec_colors = df["recommendation"].map(style.RECOMMENDATION_COLORS)
    fig_rec = go.Figure()
    fig_rec.add_bar(x=df["hour_label"], y=[1] * len(df), marker_color=rec_colors,
                     hovertext=df["recommendation"], hoverinfo="text")
    fig_rec.update_layout(title="Staffing recommendation by hour", height=120, margin=dict(t=40, b=10),
                           plot_bgcolor="white", showlegend=False, yaxis=dict(visible=False))
    st.plotly_chart(fig_rec, width="stretch", key=f"{key_prefix}_rec_chart_{picked_date}")

    st.subheader("Hour-by-hour detail")
    show = df[["hour_label", "tickets", "worked_on", "closed", "scheduled_agents", "actual_online",
                "effective_agents", "required_agents", "agent_gap", "sla_hit_rate", "recommendation"]].rename(columns={
        "hour_label": "Hour", "tickets": "Created", "worked_on": "Worked on", "closed": "Closed",
        "scheduled_agents": "Scheduled",
        "actual_online": "Actual (Slack)", "effective_agents": "Effective (basis)",
        "required_agents": "Needed", "agent_gap": "Gap",
        "sla_hit_rate": "SLA hit-rate", "recommendation": "Recommendation",
    })
    show["Worked on"] = show["Worked on"].map(lambda x: int(x) if pd.notna(x) else "–")
    show["Closed"] = show["Closed"].map(lambda x: int(x) if pd.notna(x) else "–")
    show["SLA hit-rate"] = show["SLA hit-rate"].map(lambda x: f"{x:.0%}" if pd.notna(x) else "–")
    st.dataframe(show, width="stretch", hide_index=True, key=f"{key_prefix}_hourly_table_{picked_date}")
    st.caption(
        "'Created' is tickets that came in during that hour. 'Worked on' counts tickets "
        "created on this same day that got a reply from a human agent (not Fin AI) during "
        "that hour; 'Closed' counts ones marked closed during that hour, by an agent or "
        "automatically. Both only cover tickets created within the selected day - work done "
        "that hour on a ticket created on an earlier day won't show up here. "
        "'Effective (basis)' is whichever number the recommendation actually used for that "
        "hour: actual online when Slack has it, the scheduled roster when it doesn't."
    )


st.title("Support Staffing Dashboard")
tab_daily, tab_trends = st.tabs(["Daily staffing", "Trends"])

# ------------------------------------------------------------- daily tab --
with tab_daily:
    picked_date = st.date_input("Date", value=datetime.now(LOCAL_TZ).date())
    if not token:
        st.stop()
    render_daily_view(picked_date)

# ------------------------------------------------------------ trends tab --
with tab_trends:
    if not token:
        st.stop()
    window = st.radio("Window", ["Yesterday", "Last 7 days", "Last 30 days", "Last 90 days", "Custom range"],
                       horizontal=True, index=2)

    today_local = datetime.now(LOCAL_TZ).date()
    if window == "Yesterday":
        # Yesterday is a single day, so the aggregate multi-day charts below
        # (daily totals, a heatmap by day-of-week) don't have anything
        # meaningful to show - one bar, one dot, one heatmap row. Instead,
        # show the exact same hour-by-hour view as the Daily staffing tab,
        # as if yesterday's date had been picked there.
        yesterday_local = today_local - timedelta(days=1)
        st.caption(f"1 day: {yesterday_local.strftime('%b %-d, %Y')} (America/Chicago).")
        render_daily_view(yesterday_local, key_prefix="trends_yesterday")
    else:
        if window == "Custom range":
            picked_range = st.date_input(
                "Pick a start and end date (inclusive) - use the same date twice for a single day",
                value=(today_local - timedelta(days=29), today_local),
                max_value=today_local,
            )
            # Streamlit returns a single date while the user has only picked one
            # end of the range yet - fall back to that same date for both ends
            # until the second click lands, rather than erroring.
            if isinstance(picked_range, tuple) and len(picked_range) == 2:
                range_start, range_end = picked_range
            elif isinstance(picked_range, tuple) and len(picked_range) == 1:
                range_start = range_end = picked_range[0]
            else:
                range_start = range_end = picked_range
            if range_start > range_end:
                range_start, range_end = range_end, range_start

            start_local = datetime.combine(range_start, dtime.min, tzinfo=LOCAL_TZ)
            end_local = datetime.combine(range_end, dtime.min, tzinfo=LOCAL_TZ) + timedelta(days=1)
            span_days = (range_end - range_start).days + 1
            st.caption(
                f"{span_days} day{'s' if span_days != 1 else ''}: {range_start.strftime('%b %-d, %Y')} "
                f"– {range_end.strftime('%b %-d, %Y')} (America/Chicago, inclusive)."
            )
        else:
            days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}[window]
            end_local = datetime.now(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
            start_local = end_local - timedelta(days=days)

        start_utc, end_utc = start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))
        window_days = (end_local - start_local).days

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

            # "Worked on" requires one Intercom GET per ticket created in the
            # window (see load_ticket_activity_safe) - fine for a single day,
            # but potentially minutes-long over 30-90 days. Cap it so a wide
            # window doesn't hang; Created-only volume still renders either way.
            WORKED_ON_FETCH_MAX_DAYS = 14
            worked_on_skipped = window_days > WORKED_ON_FETCH_MAX_DAYS
            if not worked_on_skipped:
                activity_df = load_ticket_activity_safe(start_utc, end_utc)
                if activity_df is not None and not activity_df.empty:
                    tdf = tdf.merge(activity_df[["hour", "worked_on"]], on="hour", how="left")
                else:
                    tdf["worked_on"] = pd.NA
            else:
                tdf["worked_on"] = pd.NA

            daily = tdf.groupby("date").agg(
                tickets=("tickets", "sum"),
                worked_on=("worked_on", "sum"),
                sla_met=("sla_met", "sum"),
                human_handled=("human_handled", "sum"),
                understaffed_hours=("recommendation", lambda s: (s == "Increase").sum()),
                avg_scheduled=("scheduled_agents", "mean"),
                avg_actual=("actual_online", "mean"),
                avg_required=("required_agents", "mean"),
            ).reset_index()
            if worked_on_skipped:
                # A sum() over an all-NA column still yields 0 (pandas default
                # min_count=0), which would draw a misleading flat line at zero
                # - force it back to NA so the overlay is simply omitted below.
                daily["worked_on"] = pd.NA
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
                has_worked_on_daily = bool(daily["worked_on"].notna().any())
                fig_staffing = go.Figure()
                # Tickets drawn first (so they sit behind the agent series), on
                # their own right-hand axis - same treatment as the hourly
                # "Scheduled vs. actual vs. needed agents" chart, so the two
                # read the same way regardless of which view you're on.
                fig_staffing.add_trace(go.Scatter(
                    x=daily["date"], y=daily["tickets"], mode="lines", fill="tozeroy",
                    line=dict(color=style.SERIES_TICKETS, width=1),
                    fillcolor="rgba(42, 120, 214, 0.15)", name="Created (right axis)", yaxis="y2",
                ))
                if has_worked_on_daily:
                    fig_staffing.add_trace(go.Scatter(
                        x=daily["date"], y=daily["worked_on"], mode="lines+markers",
                        line=dict(color=style.SERIES_WORKED_ON, dash="dash", width=2),
                        marker=dict(size=4), name="Worked on (right axis)", yaxis="y2",
                    ))
                fig_staffing.add_bar(x=daily["date"], y=daily["avg_scheduled"], marker_color=style.SERIES_AGENTS,
                                      name="Avg. scheduled")
                fig_staffing.add_trace(go.Scatter(x=daily["date"], y=daily["avg_actual"], mode="lines+markers",
                                                   line=dict(color=style.SERIES_ACTUAL), name="Avg. actual online"))
                fig_staffing.add_trace(go.Scatter(x=daily["date"], y=daily["avg_required"], mode="lines+markers",
                                                   line=dict(color=style.TEXT_SECONDARY, dash="dot", width=2),
                                                   marker=dict(size=7), name="Avg. agents needed"))
                fig_staffing.update_layout(
                    title=dict(text="Daily avg. scheduled vs. actual vs. needed agents", y=0.97, yanchor="top"),
                    height=300, margin=dict(t=90, b=10), plot_bgcolor="white",
                    legend=dict(orientation="h", y=1.1, yanchor="bottom", x=0.5, xanchor="center"),
                    yaxis=dict(title="Agents"),
                    yaxis2=dict(title="Tickets", overlaying="y", side="right", showgrid=False, rangemode="tozero"),
                )
                st.plotly_chart(fig_staffing, width="stretch", key="trends_staffing_chart")
                staffing_caption = (
                    "The light blue area is total tickets created that day (right-hand scale)"
                    + ("; the dashed yellow line, same axis, is tickets worked on that day "
                       "(including carryover from earlier days)."
                       if has_worked_on_daily else ".")
                )
                if worked_on_skipped:
                    staffing_caption += (
                        f" Tickets worked on isn't shown for windows over {WORKED_ON_FETCH_MAX_DAYS} days - it "
                        "requires one Intercom lookup per ticket created in the window, which would be too "
                        f"slow to load here. Pick a shorter window (or a Custom range of {WORKED_ON_FETCH_MAX_DAYS} "
                        "days or less) to see it."
                    )
                st.caption(staffing_caption)

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
