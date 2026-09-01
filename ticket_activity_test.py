"""Offline unit test for pipeline.fetch_hourly_ticket_activity's bucketing
logic: created/worked_on/closed counts per hour, scoped to tickets created
within the queried window - see that function's docstring for the exact
rules being tested here."""
from datetime import datetime, timezone

import intercom_client
import pipeline

START = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)  # 3 one-hour buckets: 00, 01, 02 UTC

# Local (America/Chicago) hour keys line up with UTC-5 here (Aug = CDT), so
# 00:00 UTC -> Aug 29 19:00 local, 01:00 UTC -> 20:00 local, 02:00 UTC -> 21:00 local.
# We don't need to know the exact local labels for this test - just that three
# distinct hour buckets exist and behave independently.

FAKE_CONVERSATIONS = [
    # Ticket A: created hour 0, gets an admin reply in hour 0, never closed.
    {"id": "A", "created_at": int(datetime(2026, 8, 30, 0, 15, tzinfo=timezone.utc).timestamp())},
    # Ticket B: created hour 0, admin reply lands in hour 1 (next bucket), then closed in hour 2.
    {"id": "B", "created_at": int(datetime(2026, 8, 30, 0, 45, tzinfo=timezone.utc).timestamp())},
    # Ticket C: created hour 1, no admin reply at all (e.g. Fin-only resolution), never closed.
    {"id": "C", "created_at": int(datetime(2026, 8, 30, 1, 30, tzinfo=timezone.utc).timestamp())},
    # Ticket D: created BEFORE the window (should not appear in "created" at all,
    # and is never even passed to get_conversation since search_conversations
    # itself wouldn't return it for this window - simulated by simply omitting it).
]

FAKE_PARTS = {
    "A": [
        {"created_at": int(datetime(2026, 8, 30, 0, 20, tzinfo=timezone.utc).timestamp()),
         "author": {"type": "admin"}, "part_type": "comment"},
        # A bot (Fin) part should NOT count as "worked_on".
        {"created_at": int(datetime(2026, 8, 30, 0, 25, tzinfo=timezone.utc).timestamp()),
         "author": {"type": "bot"}, "part_type": "comment"},
    ],
    "B": [
        {"created_at": int(datetime(2026, 8, 30, 1, 10, tzinfo=timezone.utc).timestamp()),
         "author": {"type": "admin"}, "part_type": "comment"},
        {"created_at": int(datetime(2026, 8, 30, 2, 5, tzinfo=timezone.utc).timestamp()),
         "author": {"type": "admin"}, "part_type": "close"},
    ],
    "C": [
        # A customer reply only - should not count as worked_on or closed.
        {"created_at": int(datetime(2026, 8, 30, 1, 40, tzinfo=timezone.utc).timestamp()),
         "author": {"type": "user"}, "part_type": "comment"},
    ],
}


def fake_search_conversations(token, created_after, created_before):
    for conv in FAKE_CONVERSATIONS:
        if created_after < conv["created_at"] < created_before:
            yield conv


def fake_get_conversation(token, conversation_id):
    return {"conversation_parts": {"conversation_parts": FAKE_PARTS.get(conversation_id, [])}}


intercom_client.search_conversations = fake_search_conversations
intercom_client.get_conversation = fake_get_conversation
pipeline.search_conversations = fake_search_conversations
pipeline.get_conversation = fake_get_conversation

df = pipeline.fetch_hourly_ticket_activity("fake-token", START, END)
print(df.to_string())

assert len(df) == 3, f"expected 3 hourly buckets, got {len(df)}"
assert df["created"].sum() == 3, f"expected 3 tickets created total, got {df['created'].sum()}"

by_hour = {row["hour"]: row for _, row in df.iterrows()}
hours = sorted(by_hour.keys())
h0, h1, h2 = hours

# Hour 0 (local): ticket A created + worked on here; ticket B created here
# (its admin reply lands in hour 1, not counted here).
assert by_hour[h0]["created"] == 2, by_hour[h0]["created"]
assert by_hour[h0]["worked_on"] == 1, by_hour[h0]["worked_on"]  # only A
assert by_hour[h0]["closed"] == 0, by_hour[h0]["closed"]

# Hour 1 (local): ticket C created (no admin reply -> 0 worked_on for it);
# ticket B's admin reply lands here.
assert by_hour[h1]["created"] == 1, by_hour[h1]["created"]
assert by_hour[h1]["worked_on"] == 1, by_hour[h1]["worked_on"]  # B's reply
assert by_hour[h1]["closed"] == 0, by_hour[h1]["closed"]

# Hour 2 (local): nothing created; ticket B gets closed here by an admin -
# an admin-authored close part counts toward both "worked_on" and "closed"
# for that hour (closing a ticket is itself a human agent action), not just
# "closed" alone.
assert by_hour[h2]["created"] == 0, by_hour[h2]["created"]
assert by_hour[h2]["worked_on"] == 1, by_hour[h2]["worked_on"]  # B, via its close part
assert by_hour[h2]["closed"] == 1, by_hour[h2]["closed"]  # B

print("\nOK - ticket activity (created/worked_on/closed) bucketing test passed")
