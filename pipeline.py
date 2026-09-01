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

fetch_hourly_ticket_activity (below) is a separate, heavier function for the
Daily tab's "Worked on"/"Closed" columns - see its own docstring.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import roster
from intercom_client import get_conversation, search_conversations

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


def fetch_hourly_ticket_activity(token: str, start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
    """Returns one row per hour in [start_utc, end_utc) with:
      - created: tickets created in that hour (same definition as
        fetch_hourly_table's "tickets" - recomputed here so this function
        doesn't depend on that one having already run)
      - worked_on: distinct tickets, of the ones *created somewhere in this
        whole window*, that got a conversation part authored by a human
        admin (author.type == "admin", i.e. a real agent - not Fin AI, whose
        parts come through as author.type == "bot") whose own created_at
        falls in this hour
      - closed: same, but for a conversation part with part_type == "close"
        (whoever/whatever closed it - an agent action or Intercom's own
        auto-close) landing in this hour

    Deliberate v1 scoping: only tickets *created within [start_utc, end_utc)*
    are considered at all, so work done in this hour on a ticket created
    before the window (e.g. carried over from a prior day) won't be counted
    in "worked on" or "closed" here - see README for the full caveat. This
    keeps the added Intercom API cost bounded to "one GET per ticket created
    in the window" rather than "one GET per ticket touched at all," which for
    a busy day-long window can otherwise mean revisiting weeks of history.

    Costs one GET /conversations/{id} call per ticket created in the window,
    on top of the search call itself - Intercom's search endpoint doesn't
    return the conversation_parts thread, only "retrieve a conversation"
    does (see intercom_client.get_conversation). That's meaningfully more API
    calls than fetch_hourly_table, so this is loaded (and cached) separately
    rather than folded into it.
    """
    buckets: dict[datetime, dict] = defaultdict(lambda: {"created": 0, "worked_on": set(), "closed": set()})

    cursor = start_utc.astimezone(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
    end_local = end_utc.astimezone(LOCAL_TZ)
    while cursor < end_local:
        buckets[cursor]  # touch to materialize the zero-filled bucket
        cursor += pd.Timedelta(hours=1)

    conv_ids: list[str] = []
    for conv in search_conversations(token, int(start_utc.timestamp()), int(end_utc.timestamp())):
        created_at = conv.get("created_at")
        conv_id = conv.get("id")
        if not created_at or not conv_id:
            continue
        hour_key = _to_local(created_at).replace(minute=0, second=0, microsecond=0)
        if hour_key in buckets:
            buckets[hour_key]["created"] += 1
        conv_ids.append(conv_id)

    def _events_for(conv_id: str) -> list[tuple[datetime, str, str]]:
        detail = get_conversation(token, conv_id)
        parts = ((detail.get("conversation_parts") or {}).get("conversation_parts")) or []
        events: list[tuple[datetime, str, str]] = []
        for part in parts:
            part_created = part.get("created_at")
            if not part_created:
                continue
            hour_key = _to_local(part_created).replace(minute=0, second=0, microsecond=0)
            if hour_key not in buckets:
                continue
            author_type = (part.get("author") or {}).get("type")
            if author_type == "admin":
                events.append((hour_key, "worked_on", conv_id))
            if part.get("part_type") == "close":
                events.append((hour_key, "closed", conv_id))
        return events

    if conv_ids:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for events in pool.map(_events_for, conv_ids):
                for hour_key, kind, conv_id in events:
                    buckets[hour_key][kind].add(conv_id)

    rows = []
    for hour_key, b in sorted(buckets.items()):
        rows.append({
            "hour": hour_key,
            "created": b["created"],
            "worked_on": len(b["worked_on"]),
            "closed": len(b["closed"]),
        })

    if not rows:
        return pd.DataFrame(columns=["hour", "created", "worked_on", "closed"])
    return pd.DataFrame(rows)
