"""Forward-looking staffing forecast that accounts for logged vacation leave.

Method, in plain terms:

1. Expected tickets for each (weekday, hour) come from recent history: the
   average (or, for a busier planning level, the 75th percentile) of that
   weekday-hour over the loaded history window. With 28 days of history that
   is four samples per weekday-hour, so a Tuesday 2 PM forecast is built from
   the last four Tuesday 2 PMs. An optional volume adjustment (for example
   +10% for a promo or holiday week) scales the whole forecast.
2. Agents available for each future hour start from the roster template (the
   CST tab), then subtract agents rostered that hour who are on logged leave
   that date. Optionally scale by typical attendance (how many of the
   scheduled agents were actually online in Slack, on average).
3. Agents needed = ceil(expected tickets / capacity), where capacity is the
   same tickets-per-agent-per-hour estimate recommend.py makes from history.
4. Gap = needed minus available. Each hour gets the same Increase / Sufficient
   / Decrease call the rest of the app uses. The same call is also made
   without leave, so the app can show which understaffed hours exist only
   because of leave.

This is a planning aid, not a prediction of exact ticket counts. It does not
model growth trends, holidays, or incidents beyond the manual adjustment.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd

import leave as leave_mod
import roster
from recommend import OVERSTAFF_BUFFER

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MIN_FORECAST_TICKETS = 0.5  # below this an hour is treated as "no demand"
PLANNING_LEVELS = {"Average": None, "Busy (75th percentile)": 0.75}


def baseline_by_weekday_hour(history: pd.DataFrame, level: str = "Average") -> pd.DataFrame:
    """Expected tickets per (weekday, hour) from an hourly history table.

    history needs a tz-aware 'hour' column and a 'tickets' column (the output
    of pipeline.fetch_hourly_table). Returns columns day_name, hour_of_day,
    expected_tickets, samples - one row for every weekday-hour that appears in
    the history.
    """
    if history.empty:
        return pd.DataFrame(columns=["day_name", "hour_of_day", "expected_tickets", "samples"])
    h = history[["hour", "tickets"]].copy()
    h["day_name"] = h["hour"].dt.strftime("%A")
    h["hour_of_day"] = h["hour"].dt.hour
    grouped = h.groupby(["day_name", "hour_of_day"])["tickets"]
    quantile = PLANNING_LEVELS[level]
    expected = grouped.mean() if quantile is None else grouped.quantile(quantile)
    out = pd.DataFrame({"expected_tickets": expected, "samples": grouped.size()}).reset_index()
    return out


def attendance_factor(history: pd.DataFrame) -> float | None:
    """Typical share of scheduled agents that were actually online (Slack),
    over hours that have both numbers. Clamped to 0.5-1.0 so a few odd hours
    can't swing the forecast. None if there is no Slack data in the history."""
    if "actual_online" not in history.columns:
        return None
    both = history[history["actual_online"].notna() & (history["scheduled_agents"] > 0)]
    if both.empty:
        return None
    ratio = float(both["actual_online"].astype(float).sum() / both["scheduled_agents"].sum())
    return min(1.0, max(0.5, ratio))


def _required(expected: float, capacity: float) -> int:
    if expected < MIN_FORECAST_TICKETS:
        return 0
    return max(1, math.ceil(expected / capacity))


def _call(available: float, required: int) -> str:
    if required == 0:
        return "Sufficient"
    if available < required:
        return "Increase"
    if available > required * OVERSTAFF_BUFFER:
        return "Decrease"
    return "Sufficient"


def build_forecast(baseline: pd.DataFrame, start: date, days: int, lmap: dict[date, set[str]],
                   capacity: float, volume_adjust_pct: float = 0.0,
                   attendance: float = 1.0) -> pd.DataFrame:
    """One row per hour for `days` days starting at `start` (inclusive)."""
    expected_lookup = {(r.day_name, int(r.hour_of_day)): float(r.expected_tickets) for r in baseline.itertuples()}
    scale = 1.0 + volume_adjust_pct / 100.0
    rows = []
    for offset in range(days):
        d = start + timedelta(days=offset)
        for hour in range(24):
            dt = datetime(d.year, d.month, d.day, hour, tzinfo=roster.ROSTER_TZ)
            day_name = DAY_ORDER[dt.weekday()]
            expected = expected_lookup.get((day_name, hour), 0.0) * scale
            roster_agents = roster.scheduled_agents(dt)
            off = leave_mod.rostered_on_leave(dt, lmap)
            after_leave = roster_agents - len(off)
            required = _required(expected, capacity)
            projected = after_leave * attendance
            baseline_avail = roster_agents * attendance
            rows.append({
                "hour": dt, "hour_label": dt.strftime("%-I %p"), "date": d, "day_name": day_name,
                "hour_of_day": hour,
                "forecast_tickets": expected,
                "roster_agents": roster_agents,
                "on_leave": len(off),
                "on_leave_names": ", ".join(sorted(off)),
                "available_agents": projected,
                "required_agents": required,
                "gap": required - projected,
                "recommendation": _call(projected, required),
                "recommendation_no_leave": _call(baseline_avail, required),
            })
    df = pd.DataFrame(rows)
    df["leave_caused"] = (df["recommendation"] == "Increase") & (df["recommendation_no_leave"] != "Increase")
    return df


def daily_summary(fc: pd.DataFrame) -> pd.DataFrame:
    """One row per date: demand, leave, and how many hours are short."""
    if fc.empty:
        return pd.DataFrame()
    g = fc.groupby("date")
    out = pd.DataFrame({
        "day_name": g["day_name"].first(),
        "forecast_tickets": g["forecast_tickets"].sum(),
        "agent_hours_on_leave": g["on_leave"].sum(),
        "understaffed_hours": g["recommendation"].apply(lambda s: int((s == "Increase").sum())),
        "understaffed_due_to_leave": g["leave_caused"].sum().astype(int),
        "worst_gap": g["gap"].max().clip(lower=0),
    }).reset_index()
    names = fc[fc["on_leave_names"] != ""].groupby("date")["on_leave_names"].apply(
        lambda s: ", ".join(sorted({n for chunk in s for n in chunk.split(", ") if n}))
    )
    out["agents_on_leave"] = out["date"].map(names).fillna("")
    return out
