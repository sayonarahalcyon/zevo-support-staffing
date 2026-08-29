"""Parses ZEVO's #secret-cc-cafe Slack channel into actual per-hour agent
availability, to compare against the planned roster (see roster.py).

That channel's whole purpose is login/break/back/logout pings (its topic is
literally "Log in and Log out / Breaks"), so - checked against real channel
history - every message is expected to match one of four signals:

    login:  "good morning", "morning", "hello", "hi"
    break:  "break"
    back:   "back"
    logout: "eos", "eod", "heading out", or a "bye" sign-off

checked in that order (logout first) so a combined message like "good night.
eos" resolves to logout, not login. A message matching none of these is left
unclassified - skipped, not guessed. Likewise, a poster who can't be matched
to exactly one roster agent code (see roster.match_agent) is skipped for
every message they post, and surfaced in this module's returned meta dict as
"unmatched" rather than silently dropped - that covers people who post in the
same channel but aren't a scheduled agent (e.g. a supervisor saying good
morning), as well as any real mismatch worth someone's attention.

This runs on Streamlit Cloud, separate from any Claude/MCP session, so it
authenticates with its own Slack bot token (read from Streamlit secrets or an
environment variable) and talks to Slack's Web API directly. The bot needs:
  - groups:history (the channel is private) - and must be invited into it
  - users:read (to resolve a poster's display/real name)
  - users:read.email (optional; improves matching but not required)
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd
import requests

import roster

LOCAL_TZ = roster.ROSTER_TZ
API_BASE = "https://slack.com/api"
MAX_RETRIES = 4
MAX_SHIFT_HOURS = 16  # safety cap for a session with no observed logout - see module docstring


class SlackAuthError(RuntimeError):
    pass


_LOGOUT_RE = re.compile(r"\b(eos|eod)\b|heading out|bye", re.IGNORECASE)
_BREAK_RE = re.compile(r"\bbreak\b", re.IGNORECASE)
_BACK_RE = re.compile(r"\bback\b", re.IGNORECASE)
_LOGIN_RE = re.compile(r"good morning|\bmorning\b|\bhello\b|\bhi\b", re.IGNORECASE)


def classify_message(text: str) -> str | None:
    """Returns "login" / "break" / "back" / "logout", or None if the message
    doesn't match any of them (see module docstring for the phrase list)."""
    if not text:
        return None
    if _LOGOUT_RE.search(text):
        return "logout"
    if _BREAK_RE.search(text):
        return "break"
    if _BACK_RE.search(text):
        return "back"
    if _LOGIN_RE.search(text):
        return "login"
    return None


def _to_local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ)


def _call(token: str, endpoint: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    last_error = "unknown_error"
    for attempt in range(MAX_RETRIES):
        resp = requests.get(f"{API_BASE}/{endpoint}", headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "2")))
            continue
        data = resp.json()
        if data.get("ok"):
            return data
        err = data.get("error", "unknown_error")
        if err in ("invalid_auth", "not_authed", "account_inactive", "token_revoked",
                    "missing_scope", "not_in_channel", "channel_not_found"):
            raise SlackAuthError(f"Slack rejected the request ({endpoint}): {err}. "
                                  f"Check SLACK_BOT_TOKEN / SLACK_CHANNEL_ID and that the bot is in the channel.")
        last_error = err
        time.sleep(1 + attempt)
    raise RuntimeError(f"Slack API error ({endpoint}): {last_error}")


def _fetch_channel_messages(token: str, channel_id: str, oldest_ts: float, latest_ts: float) -> list[dict]:
    messages: list[dict] = []
    cursor = None
    while True:
        params = {"channel": channel_id, "oldest": f"{oldest_ts}", "latest": f"{latest_ts}",
                   "limit": 200, "inclusive": "true"}
        if cursor:
            params["cursor"] = cursor
        data = _call(token, "conversations.history", params)
        messages.extend(m for m in data.get("messages", []) if m.get("type") == "message" and not m.get("subtype"))
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return messages


# The roster lists both MICHELLE and MITCH as distinct agent codes, and one
# Slack account (display name "Mitch", email michelle@zevo.com) genuinely
# matches both by the generic name/email matching in roster.match_agent - so
# it comes back ambiguous (None) without help. Confirmed directly by Weng:
# Slack's "Mitch" is roster's MICHELLE, and Slack's "Chelle" is roster's
# MICHELL (that one already matched fine on its own, via the email
# michell@zevo.com). This override is for that one specific, confirmed case -
# it is not a general license to guess at other ambiguous matches.
MANUAL_EMAIL_OVERRIDES = {
    "michelle@zevo.com": "MICHELLE",
}


def _fetch_agent_map(token: str, user_ids: Iterable[str]) -> dict[str, str | None]:
    """user_id -> matched roster agent code (or None if it can't be matched to
    exactly one - see roster.match_agent)."""
    out: dict[str, str | None] = {}
    for uid in user_ids:
        data = _call(token, "users.info", {"user": uid})
        user = data.get("user") or {}
        profile = user.get("profile") or {}
        email = profile.get("email") or ""
        display_name = profile.get("display_name") or user.get("real_name") or ""
        real_name = profile.get("real_name") or ""

        if email and email.lower() in MANUAL_EMAIL_OVERRIDES:
            out[uid] = MANUAL_EMAIL_OVERRIDES[email.lower()]
            continue

        candidates = list(display_name.split()) + list(real_name.split())
        if email:
            candidates.append(email.split("@")[0])
        out[uid] = roster.match_agent(candidates)
    return out


def fetch_actual_online_table(
    token: str, channel_id: str, start_utc: datetime, end_utc: datetime, lookback_hours: int = 24
) -> tuple[pd.DataFrame, dict]:
    """Returns (hourly table, meta).

    Hourly table: one row per hour in [start_utc, end_utc) with "actual_online"
    (headcount, from reconstructed login/break/back/logout sessions) and
    "actual_agents" (the list of agent codes counted in that hour).

    meta["unmatched_slack_user_ids"]: Slack user IDs seen posting in the window
    that couldn't be matched to exactly one roster agent - shown in the app so
    it's visible, not silently dropped.

    lookback_hours pulls extra history before start_utc so a shift that began
    before the window (but is still open) is correctly counted as online at
    the start of the window, rather than missed entirely.
    """
    oldest = start_utc - timedelta(hours=lookback_hours)
    raw = _fetch_channel_messages(token, channel_id, oldest.timestamp(), end_utc.timestamp())

    user_ids = sorted({m["user"] for m in raw if m.get("user")})
    agent_map = _fetch_agent_map(token, user_ids)
    unmatched_users = sorted({uid for uid in user_ids if agent_map.get(uid) is None})

    per_agent: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for m in raw:
        code = agent_map.get(m.get("user"))
        if not code:
            continue
        kind = classify_message(m.get("text", ""))
        if kind is None:
            continue
        per_agent[code].append((float(m["ts"]), kind))

    intervals: list[tuple[str, float, float]] = []
    for agent, events in per_agent.items():
        events.sort(key=lambda e: e[0])
        state = "OFF"
        seg_start = None
        for ts, kind in events:
            if kind == "login" and state == "OFF":
                state, seg_start = "ON", ts
            elif kind == "break" and state == "ON":
                intervals.append((agent, seg_start, ts))
                state = "BREAK"
            elif kind == "back" and state == "BREAK":
                state, seg_start = "ON", ts
            elif kind == "logout" and state in ("ON", "BREAK"):
                if state == "ON":
                    intervals.append((agent, seg_start, ts))
                state, seg_start = "OFF", None
            # any other ordering (duplicate login, "back" with no "break", ...)
            # is ignored rather than guessed at
        if state == "ON" and seg_start is not None:
            cutoff = min(end_utc.timestamp(), seg_start + MAX_SHIFT_HOURS * 3600)
            intervals.append((agent, seg_start, cutoff))

    hour_keys: list[datetime] = []
    cursor = start_utc.astimezone(LOCAL_TZ).replace(minute=0, second=0, microsecond=0)
    end_local = end_utc.astimezone(LOCAL_TZ)
    while cursor < end_local:
        hour_keys.append(cursor)
        cursor += timedelta(hours=1)
    valid_hours = set(hour_keys)

    online: dict[datetime, set] = defaultdict(set)
    for agent, s, e in intervals:
        hk = _to_local(s).replace(minute=0, second=0, microsecond=0)
        e_local = _to_local(e)
        while hk < e_local:
            if hk in valid_hours:
                online[hk].add(agent)
            hk += timedelta(hours=1)

    rows = [
        {"hour": hk, "actual_online": len(online.get(hk, ())), "actual_agents": sorted(online.get(hk, ()))}
        for hk in hour_keys
    ]
    meta = {"unmatched_slack_user_ids": unmatched_users}
    if not rows:
        return pd.DataFrame(columns=["hour", "actual_online", "actual_agents"]), meta
    return pd.DataFrame(rows), meta
