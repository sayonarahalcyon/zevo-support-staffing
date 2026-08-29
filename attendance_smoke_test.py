"""Offline smoke test for attendance.py's session reconstruction: monkeypatches
the two network calls (_fetch_channel_messages, _fetch_agent_map) with
synthetic data shaped like real #secret-cc-cafe messages, and checks the
resulting hourly headcounts by hand."""
from datetime import datetime, timedelta, timezone

import attendance

LOCAL_TZ = attendance.LOCAL_TZ


def ts(local_dt):
    return local_dt.timestamp()


start_local = datetime(2026, 8, 24, 0, 0, tzinfo=LOCAL_TZ)  # Monday midnight
end_local = start_local + timedelta(hours=6)
start_utc = start_local.astimezone(timezone.utc)
end_utc = end_local.astimezone(timezone.utc)

# JULY: login 0:05, break 1:00, back 1:15, logout 2:00 -> "on" during
#       [0:05,1:00) and [1:15,2:00) -> counted online in hours 0 and 1, not 2
# MARK: only a stray "back" with no login first -> never counted (ignored, not guessed)
# STELLA: login 3:00, never logs out within the window -> stays online through
#         the rest of the window (capped, doesn't disappear)
synthetic_messages = [
    {"user": "U_JULY", "ts": str(ts(start_local + timedelta(minutes=5))), "text": "Good morning",
     "type": "message"},
    {"user": "U_JULY", "ts": str(ts(start_local + timedelta(hours=1))), "text": "Break", "type": "message"},
    {"user": "U_JULY", "ts": str(ts(start_local + timedelta(hours=1, minutes=15))), "text": "back",
     "type": "message"},
    {"user": "U_JULY", "ts": str(ts(start_local + timedelta(hours=2))), "text": "EOS. Thank you",
     "type": "message"},
    {"user": "U_MARK", "ts": str(ts(start_local + timedelta(hours=1))), "text": "back", "type": "message"},
    {"user": "U_STELLA", "ts": str(ts(start_local + timedelta(hours=3))), "text": "Hello", "type": "message"},
    {"user": "U_NOTANAGENT", "ts": str(ts(start_local + timedelta(hours=1))), "text": "Good morning team!",
     "type": "message"},
]

attendance._fetch_channel_messages = lambda token, channel_id, oldest_ts, latest_ts: synthetic_messages
attendance._fetch_agent_map = lambda token, user_ids: {
    "U_JULY": "JULY", "U_MARK": "MARK", "U_STELLA": "STELLA", "U_NOTANAGENT": None,
}

df, meta = attendance.fetch_actual_online_table("fake-token", "C_FAKE", start_utc, end_utc)
print(df.to_string())
print("meta:", meta)

by_hour = {row.hour.hour: row.actual_agents for row in df.itertuples()}
assert by_hour[0] == ["JULY"], by_hour[0]
assert by_hour[1] == ["JULY"], by_hour[1]  # back at :15, still on for part of the hour
assert by_hour[2] == [], by_hour[2]        # logged out exactly at 2:00
assert by_hour[3] == ["STELLA"], by_hour[3]
assert by_hour[4] == ["STELLA"], by_hour[4]  # no logout seen -> stays online
assert meta["unmatched_slack_user_ids"] == ["U_NOTANAGENT"]

print("\nOK - attendance smoke test passed")
