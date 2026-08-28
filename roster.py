"""Loads the agent work schedule (from ZEVO's 'ZEVO Workforce' Google Sheet,
'CST' tab) and answers "how many agents were scheduled this hour?".

Source: the CST tab's named-agent grid - one row per hour (12:00 AM -> 11:00 PM),
one column per day of week, listing every agent scheduled in that hour. This is
a standard midnight-to-midnight calendar day (confirmed with Weng: the team runs
24/7, each day's hours run 12:00 AM through 11:00 PM), so no shift-day offset is
needed here (unlike the sheet's separate PROPOSED tab, which uses an 11am-11am
shift-day convention - that tab is not used by this app).

"scheduled_agents" for a given (day, hour) is the count of distinct agent names
listed in that grid cell. This is a recurring WEEKLY template, not a record of
who actually clocked in on a specific date - if ZEVO starts tracking actual
clock-ins somewhere, swap this module's data source and keep the interface.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_PATH = Path(__file__).parent / "data" / "roster_schedule.csv"
ROSTER_TZ = ZoneInfo("America/Chicago")  # "CST/CDT" in the source sheet

_DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class RosterSlot:
    scheduled_agents: int
    agents: list[str]


def _load() -> dict[tuple[str, int], RosterSlot]:
    table: dict[tuple[str, int], RosterSlot] = {}
    with open(DATA_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            day = row["day_of_week"]
            hour = int(row["hour_cst"])
            agents = [a for a in row["agents"].split(";") if a]
            table[(day, hour)] = RosterSlot(scheduled_agents=int(row["scheduled_agents"]), agents=agents)
    return table


_ROSTER = _load()


def scheduled_agents(local_dt) -> int:
    """local_dt: a timezone-aware datetime in America/Chicago (standard calendar day/hour)."""
    slot = _ROSTER.get((_DAY_ORDER[local_dt.weekday()], local_dt.hour))
    return slot.scheduled_agents if slot else 0


def agents_on_duty(local_dt) -> list[str]:
    slot = _ROSTER.get((_DAY_ORDER[local_dt.weekday()], local_dt.hour))
    return slot.agents if slot else []


def weekly_template() -> dict[tuple[str, int], int]:
    """Full (day, hour) -> scheduled_agents map, for the trends heatmap."""
    return {k: v.scheduled_agents for k, v in _ROSTER.items()}
