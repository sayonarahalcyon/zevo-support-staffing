"""Turns raw Intercom conversations into an hourly staffing table.

For every hour bucket (America/Chicago, matching the roster) we compute:
  - tickets: all conversations created in that hour
  - human_handled: of those, how many ever got a first admin/human reply
      (conversations Fin AI fully resolves without escalating never get one -
      they didn't need agent capacity, so they're excluded from the SLA metric
      but still shown in the raw ticket-volume count)
  - sla_met: of the human_handled ones, how many got that first reply within
      the target response time (default 15 minutes)
  - scheduled_agents: from the roster template for that hour
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import roster
from intercom_client import search_conversations

LOCAL_TZ = ZoneInfo("America/Chicago")


def _to_local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ)


def _empty_bucket() -> dict:
    return {"tickets": 0, "human_handled": 0, "sla_met": 0, "response_times": []}


def fetch_hourly_table(token: str, start_utc: datetime, end_utc: datetime, sla_seconds: int = 900) -> pd.DataFrame:
    """Returns one row per hour in [start_utc, end_utc) with ticket/SLA/staffing columns.

    Every hour in the range is included (even ones with zero tickets), so a quiet
    overnight hour still shows up in the chart instead of silently disappearing.
    """
    buckets: dict[datetime, dict] = defaultdict(_empty_bucket)

    cursor = start_utc.astimezone(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
    end_local = end_utc.astimezone(LOCAL_TZ)
    while cursor < end_local:
        buckets[cursor]  # touch to materialize the zero-filled bucket
        cursor += pd.Timedelta(hours=1)

    for conv in search_conversations(token, int(start_utc.timestamp()), int(end_utc.timestamp())):
        created_at = conv.get("created_at")
        if not created_at:
            continue
        local_dt = _to_local(created_at)
        hour_key = local_dt.replace(minute=0, second=0, microsecond=0)
        b = buckets[hour_key]
        b["tickets"] += 1

        stats = conv.get("statistics") or {}
        first_reply = stats.get("first_admin_reply_at")
        if first_reply:
            response_time = first_reply - created_at
            b["human_handled"] += 1
            b["response_times"].append(response_time)
            if response_time <= sla_seconds:
                b["sla_met"] += 1

    rows = []
    for hour_key, b in sorted(buckets.items()):
        sched = roster.scheduled_agents(hour_key)
        human_handled = b["human_handled"]
        rt = b["response_times"]
        rows.append({
            "hour": hour_key,
            "tickets": b["tickets"],
            "human_handled": human_handled,
            "sla_met": b["sla_met"],
            "sla_hit_rate": (b["sla_met"] / human_handled) if human_handled else None,
            "median_response_sec": (sorted(rt)[len(rt) // 2] if rt else None),
            "scheduled_agents": sched,
            "tickets_per_agent": (b["tickets"] / sched) if sched else None,
        })

    if not rows:
        return pd.DataFrame(columns=[
            "hour", "tickets", "human_handled", "sla_met", "sla_hit_rate",
            "median_response_sec", "scheduled_agents", "tickets_per_agent",
        ])
    return pd.DataFrame(rows)
