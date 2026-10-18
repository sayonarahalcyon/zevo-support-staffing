"""Vacation-leave logic: which rostered agents are off on which dates.

This module is pure logic (no network, no Streamlit) so it can be tested on
its own. The leave records themselves live in Supabase (see leave_store.py);
callers pass them in as a list of dicts with at least:

    agent_code  - a roster agent code, e.g. "ERWIN"
    start_date  - first day off (date or ISO string, inclusive)
    end_date    - last day off (date or ISO string, inclusive)
    status      - "active" or "cancelled" (missing means active)

Only approved leaves are ever logged (Erwin enters them after approval), so
every active record reduces headcount.

How a leave is applied: dates are calendar dates in America/Chicago, the same
calendar day the roster template uses. An agent on leave for a date is removed
from every hour they are rostered on that date. A shift that runs past
midnight appears in the roster as hours on two calendar days (for example
Sunday 6 PM to Monday 1 AM), so a leave for such a shift needs both dates
logged. The Leave log tab says this next to the date picker.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

import roster


def to_date(value) -> date:
    """Accepts a date, datetime, or ISO 'YYYY-MM-DD' string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def is_active(leave: dict) -> bool:
    return (leave.get("status") or "active") == "active"


def leave_by_date(leaves: list[dict]) -> dict[date, set[str]]:
    """Maps each calendar date to the set of agent codes on leave that day.
    Cancelled records are ignored."""
    out: dict[date, set[str]] = defaultdict(set)
    for leave in leaves:
        if not is_active(leave):
            continue
        start, end = to_date(leave["start_date"]), to_date(leave["end_date"])
        code = str(leave["agent_code"]).upper()
        d = start
        while d <= end:
            out[d].add(code)
            d += timedelta(days=1)
    return out


def rostered_on_leave(local_dt, lmap: dict[date, set[str]]) -> list[str]:
    """Agents who are rostered in this hour AND on leave on this calendar date."""
    off = lmap.get(local_dt.date())
    if not off:
        return []
    return [a for a in roster.agents_on_duty(local_dt) if a in off]


def agents_after_leave(local_dt, lmap: dict[date, set[str]]) -> int:
    """Roster headcount for this hour minus agents on leave."""
    return roster.scheduled_agents(local_dt) - len(rostered_on_leave(local_dt, lmap))


def annotate_hourly(df, lmap: dict[date, set[str]]):
    """Adds two columns to an hourly DataFrame (needs a tz-aware 'hour' column
    in America/Chicago and a 'scheduled_agents' column):

      on_leave              - rostered agents off that hour because of leave
      scheduled_after_leave - scheduled_agents minus on_leave

    Returns a copy. With no leaves, on_leave is 0 and the two headcount
    columns are identical, so callers behave exactly as before leave existed.
    """
    df = df.copy()
    if df.empty:
        df["on_leave"] = []
        df["scheduled_after_leave"] = []
        return df
    df["on_leave"] = [len(rostered_on_leave(h, lmap)) for h in df["hour"]]
    df["scheduled_after_leave"] = df["scheduled_agents"] - df["on_leave"]
    return df


def find_overlaps(leaves: list[dict], agent_code: str, start, end, ignore_id=None) -> list[dict]:
    """Active leaves for the same agent whose dates overlap [start, end]."""
    start, end = to_date(start), to_date(end)
    hits = []
    for leave in leaves:
        if not is_active(leave):
            continue
        if ignore_id is not None and str(leave.get("id")) == str(ignore_id):
            continue
        if str(leave["agent_code"]).upper() != agent_code.upper():
            continue
        if to_date(leave["start_date"]) <= end and to_date(leave["end_date"]) >= start:
            hits.append(leave)
    return hits


def validate(agent_code: str, start, end) -> list[str]:
    """Returns a list of problems with a proposed leave (empty means fine)."""
    problems = []
    if not agent_code or agent_code.upper() not in roster.ALL_AGENT_CODES:
        problems.append("Pick an agent from the roster list.")
    try:
        s, e = to_date(start), to_date(end)
    except Exception:
        return problems + ["Start and end dates are required."]
    if e < s:
        problems.append("End date is before the start date.")
    elif (e - s).days > 60:
        problems.append("A single leave can span at most 61 days. Split longer ones into two entries.")
    return problems


def coverage_warnings(leaves: list[dict], agent_code: str, start, end,
                      min_agents: int, ignore_id=None) -> list[dict]:
    """Preview of what a proposed leave would do to roster coverage.

    Looks at every date in [start, end] and every hour the agent is rostered
    that date, counts how many rostered agents would remain once this leave
    AND every other active leave are applied, and reports hours where that is
    below min_agents. This is a roster-only check (no ticket data), so it is
    instant; the Forecast tab does the demand-based version.

    Returns rows: {date, hour, remaining, others_off} sorted by date and hour.
    """
    start, end = to_date(start), to_date(end)
    others = [lv for lv in leaves if str(lv.get("id")) != str(ignore_id)]
    proposed = {"agent_code": agent_code.upper(), "start_date": start, "end_date": end, "status": "active"}
    lmap = leave_by_date(others + [proposed])
    other_map = leave_by_date(others)
    rows = []
    d = start
    while d <= end:
        for hour in range(24):
            dt = datetime(d.year, d.month, d.day, hour, tzinfo=roster.ROSTER_TZ)
            on_duty = roster.agents_on_duty(dt)
            if agent_code.upper() not in on_duty:
                continue
            remaining = len(on_duty) - len([a for a in on_duty if a in lmap.get(d, set())])
            if remaining < min_agents:
                others_off = [a for a in on_duty if a in other_map.get(d, set())]
                rows.append({"date": d, "hour": hour, "remaining": remaining, "others_off": others_off})
        d += timedelta(days=1)
    return rows
