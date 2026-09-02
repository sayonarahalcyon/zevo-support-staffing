"""Exercises the new 'Custom range' path on the Trends tab specifically -
apptest_check.py only ever runs the default ('Last 30 days') radio state, so
it never touches the date_input branch added for date-range selection."""
import os
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ["INTERCOM_ACCESS_TOKEN"] = "fake-token-for-test"
os.environ["SLACK_BOT_TOKEN"] = "fake-slack-token-for-test"

import attendance
import intercom_client

# Match the app's own notion of "today" (America/Chicago), not the test
# runner's system-local date - they can differ by a day depending on UTC
# offset at run time.
TODAY_LOCAL = datetime.now(ZoneInfo("America/Chicago")).date()


def fake_fetch_channel_messages(token, channel_id, oldest_ts, latest_ts):
    random.seed(5)
    messages = []
    t = oldest_ts
    users = ["U_JULY", "U_STELLA", "U_NOTANAGENT"]
    while t < latest_ts:
        if random.random() < 0.3:
            user = random.choice(users)
            text = random.choice(["Good morning", "Break", "back", "EOS. Thank you"])
            messages.append({"user": user, "ts": str(t), "text": text, "type": "message"})
        t += 900
    return messages


attendance._fetch_channel_messages = fake_fetch_channel_messages
attendance._fetch_agent_map = lambda token, user_ids: (
    {"U_JULY": "JULY", "U_STELLA": "STELLA", "U_NOTANAGENT": None},
    set(),
)


CONV_CREATED_AT: dict[str, int] = {}


def fake_search_conversations(token, created_after, created_before):
    random.seed(3)
    t = created_after
    conv_id = 0
    while t < created_before:
        n = random.randint(0, 8)
        for _ in range(n):
            conv_id += 1
            created = t + random.randint(0, 3599)
            got_reply = random.random() < 0.85
            resp_time = random.choice([120, 300, 600, 900, 1800, 3600]) if got_reply else None
            CONV_CREATED_AT[str(conv_id)] = created
            yield {
                "id": str(conv_id),
                "created_at": created,
                "statistics": {"first_admin_reply_at": (created + resp_time) if resp_time else None},
            }
        t += 3600


def fake_get_conversation(token, conversation_id):
    created = CONV_CREATED_AT.get(conversation_id)
    if created is None:
        return {"conversation_parts": {"conversation_parts": []}}
    random.seed(int(conversation_id) * 7 + 1)
    parts = []
    if random.random() < 0.8:
        parts.append({"created_at": created + 300, "author": {"type": "admin"}, "part_type": "comment"})
    if random.random() < 0.5:
        parts.append({"created_at": created + 1800, "author": {"type": "admin"}, "part_type": "close"})
    return {"conversation_parts": {"conversation_parts": parts}}


intercom_client.search_conversations = fake_search_conversations
intercom_client.get_conversation = fake_get_conversation

from streamlit.testing.v1 import AppTest

at = AppTest.from_file("app.py", default_timeout=60)
at.run()
assert not at.exception, f"Initial run raised: {list(at.exception)}"

# Switch the Trends-tab window radio to "Yesterday".
radios = [r for r in at.radio if r.label == "Window"]
assert radios, "Could not find the Trends 'Window' radio"
window_radio = radios[0]
window_radio.set_value("Yesterday").run()
assert not at.exception, f"Selecting Yesterday raised: {list(at.exception)}"
assert any("1 day:" in c.value for c in at.caption), "Yesterday's single-day caption did not appear"

# Switch the Trends-tab window radio to "Custom range".
window_radio.set_value("Custom range").run()
assert not at.exception, f"Selecting Custom range raised: {list(at.exception)}"

# It should have surfaced a date_input for picking the range, defaulted to
# the last 30 days (same shape as the old preset default).
range_inputs = [d for d in at.date_input if "start and end date" in d.label]
assert range_inputs, "Custom-range date_input did not appear"
di = range_inputs[0]
assert di.value == (TODAY_LOCAL - timedelta(days=29), TODAY_LOCAL), di.value

# Now narrow it to a single specific day ("just a specific day" case).
one_day = TODAY_LOCAL - timedelta(days=2)
di.set_value((one_day, one_day)).run()
assert not at.exception, f"Single-day custom range raised: {list(at.exception)}"

# Edge case: the Daily tab's own date picker happens to be set to the exact
# same date as Trends' "Yesterday" - both render_daily_view() calls run in
# the same script pass regardless of which tab is visually active, so this
# checks their chart/table keys don't collide when the dates match.
yesterday = TODAY_LOCAL - timedelta(days=1)
date_inputs = [d for d in at.date_input if d.label == "Date"]
assert date_inputs, "Could not find the Daily tab's 'Date' input"
date_inputs[0].set_value(yesterday).run()
assert not at.exception, f"Setting Daily tab to yesterday raised: {list(at.exception)}"
window_radio.set_value("Yesterday").run()
assert not at.exception, (
    f"Daily tab date == Trends 'Yesterday' date raised: {list(at.exception)}"
)

print("APPTEST OK - custom range (multi-day and single-day) both render without error")
