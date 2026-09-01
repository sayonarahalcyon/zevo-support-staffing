import os
import random

os.environ["INTERCOM_ACCESS_TOKEN"] = "fake-token-for-test"
os.environ["SLACK_BOT_TOKEN"] = "fake-slack-token-for-test"

# Patch the network calls before Streamlit imports/runs app.py
import attendance
import intercom_client


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

print("Exceptions after initial run:", at.exception)
for e in at.exception:
    print(" -", e)

# Click into the Trends tab equivalent by directly checking no exception raised;
# AppTest's tabs aren't independently clickable in older versions, so we just
# assert the run didn't blow up and sidebar/metrics rendered.
print("Number of markdown/metric elements:", len(at.main))
assert not at.exception, "App raised an exception during execution"
print("APPTEST OK")
