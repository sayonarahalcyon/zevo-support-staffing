"""Turns the hourly table into an Increase / Sufficient / Decrease recommendation.

The user's stated goal is concrete: tickets should get a first response within
15 minutes. We use that directly as the headline metric (sla_hit_rate), then
turn it into a staffing recommendation like this:

1. Estimate a "sustainable tickets-per-agent" capacity by looking at hours in
   the loaded window that already met the SLA target comfortably - the 75th
   percentile of tickets_per_agent among those hours is what "enough staffing"
   looks like in practice for this team.
2. For every hour, required_agents = ceil(tickets / capacity).
3. Compare required_agents to effective_agents (see below):
     effective well below required, or hit-rate below target -> Increase
     effective comfortably above required AND hit-rate already high -> Decrease
     otherwise -> Sufficient

"effective_agents" - who actually counts as staffing for an hour - is Slack's
actual-online headcount when it's available for that hour, falling back to
the scheduled roster otherwise (confirmed with Weng: the roster sheet isn't
updated daily and misses leave, no-shows, and agents covering an off day, so
Slack - when configured - is the more trustworthy signal). Callers that merge
Slack data (see app.py) compute this column themselves before calling
add_recommendations; if it's missing entirely (no Slack configured, or a
caller like smoke_test.py that only has the roster), it's filled in here as a
straight copy of scheduled_agents, so this module works identically to before
Slack existed.

This needs a reasonable amount of data to be meaningful (a handful of hours
that already met the SLA target). With very little history the capacity
estimate falls back to a conservative default and the app says so.
"""
from __future__ import annotations

import math

import pandas as pd

DEFAULT_CAPACITY_FALLBACK = 4.0  # tickets/agent/hour, used only if we can't estimate one
OVERSTAFF_BUFFER = 1.3  # effective must exceed required by this much to call it "Decrease"


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

    tickets-per-agent is computed against effective_agents (actual online when
    Slack has it, the roster otherwise - see module docstring), not the roster
    alone, so the estimate reflects who was really covering the queue.
    """
    effective = df["effective_agents"]
    tickets_per_effective = df["tickets"] / effective.where(effective > 0)

    good_hours_mask = (
        (df["sla_hit_rate"].notna())
        & (df["sla_hit_rate"] >= sla_target)
        & (tickets_per_effective.notna())
        & (df["tickets"] >= MIN_TICKETS_FOR_SIGNAL)
    )
    if good_hours_mask.sum() >= MIN_QUALIFYING_HOURS:
        capacity = float(tickets_per_effective[good_hours_mask].quantile(0.75))
        return max(capacity, 1.0), True
    return DEFAULT_CAPACITY_FALLBACK, False


def add_recommendations(df: pd.DataFrame, sla_target: float = 0.9) -> tuple[pd.DataFrame, dict]:
    df = df.copy()
    if "effective_agents" not in df.columns:
        # No Slack-derived headcount was merged in - fall back to the roster,
        # exactly like this app behaved before Slack attendance existed.
        df["effective_agents"] = df["scheduled_agents"]

    capacity, estimated = estimate_capacity(df, sla_target)

    def required_agents(tickets: int) -> int:
        if tickets <= 0:
            return 0
        return max(1, math.ceil(tickets / capacity))

    df["required_agents"] = df["tickets"].apply(required_agents)

    def recommend(row) -> str:
        effective = row["effective_agents"]
        req = row["required_agents"]
        hit_rate = row["sla_hit_rate"]

        understaffed_signal = effective < req or (hit_rate is not None and hit_rate < sla_target)
        overstaffed_signal = effective > req * OVERSTAFF_BUFFER and (hit_rate is None or hit_rate >= sla_target)

        if row["tickets"] == 0:
            return "Sufficient"
        if understaffed_signal:
            return "Increase"
        if overstaffed_signal:
            return "Decrease"
        return "Sufficient"

    df["recommendation"] = df.apply(recommend, axis=1)
    df["agent_gap"] = df["required_agents"] - df["effective_agents"]

    meta = {
        "capacity_tickets_per_agent": round(capacity, 2),
        "capacity_estimated_from_data": estimated,
        "sla_target": sla_target,
    }
    return df, meta
