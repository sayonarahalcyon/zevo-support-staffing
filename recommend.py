"""Turns the hourly table into an Increase / Sufficient / Decrease recommendation.

The user's stated goal is concrete: tickets should get a first response within
15 minutes. We use that directly as the headline metric (sla_hit_rate), then
turn it into a staffing recommendation like this:

1. Estimate a "sustainable tickets-per-agent" capacity by looking at hours in
   the loaded window that already met the SLA target comfortably - the median
   tickets_per_agent among those hours is what "enough staffing" looks like in
   practice for this team.
2. For every hour, required_agents = ceil(tickets / capacity).
3. Compare required_agents to scheduled_agents:
     scheduled well below required, or hit-rate below target -> Increase
     scheduled comfortably above required AND hit-rate already high -> Decrease
     otherwise -> Sufficient

This needs a reasonable amount of data to be meaningful (a handful of hours
that already met the SLA target). With very little history the capacity
estimate falls back to a conservative default and the app says so.
"""
from __future__ import annotations

import math

import pandas as pd

DEFAULT_CAPACITY_FALLBACK = 4.0  # tickets/agent/hour, used only if we can't estimate one
OVERSTAFF_BUFFER = 1.3  # scheduled must exceed required by this much to call it "Decrease"


MIN_TICKETS_FOR_SIGNAL = 3  # ignore near-empty hours; they say nothing about capacity
MIN_QUALIFYING_HOURS = 8


def estimate_capacity(df: pd.DataFrame, sla_target: float) -> tuple[float, bool]:
    """Returns (tickets_per_agent_capacity, was_estimated_from_data).

    We want "how much load can an agent take on and still hit the SLA", which is
    a busy-hour question - so this only looks at hours with a meaningful ticket
    count that still met the SLA target, and takes the 75th percentile of their
    tickets-per-agent ratio (not the median), since typical/quiet hours understate
    real capacity. Hours with too few tickets to be a real signal are excluded
    entirely, otherwise a quiet hour that happens to hit 100% SLA with 1 ticket
    and a full roster would drag the estimate down to near zero.
    """
    good_hours = df[
        (df["sla_hit_rate"].notna())
        & (df["sla_hit_rate"] >= sla_target)
        & (df["tickets_per_agent"].notna())
        & (df["tickets"] >= MIN_TICKETS_FOR_SIGNAL)
    ]
    if len(good_hours) >= MIN_QUALIFYING_HOURS:
        capacity = float(good_hours["tickets_per_agent"].quantile(0.75))
        return max(capacity, 1.0), True
    return DEFAULT_CAPACITY_FALLBACK, False


def add_recommendations(df: pd.DataFrame, sla_target: float = 0.9) -> tuple[pd.DataFrame, dict]:
    df = df.copy()
    capacity, estimated = estimate_capacity(df, sla_target)

    def required_agents(tickets: int) -> int:
        if tickets <= 0:
            return 0
        return max(1, math.ceil(tickets / capacity))

    df["required_agents"] = df["tickets"].apply(required_agents)

    def recommend(row) -> str:
        sched = row["scheduled_agents"]
        req = row["required_agents"]
        hit_rate = row["sla_hit_rate"]

        understaffed_signal = sched < req or (hit_rate is not None and hit_rate < sla_target)
        overstaffed_signal = sched > req * OVERSTAFF_BUFFER and (hit_rate is None or hit_rate >= sla_target)

        if row["tickets"] == 0:
            return "Sufficient"
        if understaffed_signal:
            return "Increase"
        if overstaffed_signal:
            return "Decrease"
        return "Sufficient"

    df["recommendation"] = df.apply(recommend, axis=1)
    df["agent_gap"] = df["required_agents"] - df["scheduled_agents"]

    meta = {
        "capacity_tickets_per_agent": round(capacity, 2),
        "capacity_estimated_from_data": estimated,
        "sla_target": sla_target,
    }
    return df, meta
