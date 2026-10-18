"""End-to-end check of the Leave log and Forecast tabs, driving the real app.py
with Streamlit's AppTest. Intercom, Slack, Supabase and Notion are all faked, so
nothing leaves this machine.

Run:  python apptest_leave_check.py
"""
import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ["INTERCOM_ACCESS_TOKEN"] = "fake-token-for-test"
os.environ["SUPABASE_URL"] = "https://fake.supabase.co"
os.environ["SUPABASE_SERVICE_KEY"] = "eyJfake.service.key"
os.environ["NOTION_TOKEN"] = "fake-notion"
os.environ["NOTION_LEAVE_DB_ID"] = "fakedb"
os.environ.pop("SLACK_BOT_TOKEN", None)

import intercom_client
import leave_store
import notion_backup
from leave_test import FakeNotion, FakePostgrest

CT = ZoneInfo("America/Chicago")

# ---- fakes -------------------------------------------------------------
FAKE_DB = FakePostgrest()
leave_store.requests.Session = lambda: FAKE_DB
FAKE_NOTION = FakeNotion()
notion_backup._request = FAKE_NOTION


def fake_search_conversations(token, created_after, created_before):
    random.seed(3)
    t = created_after
    n_id = 0
    while t < created_before:
        for _ in range(random.randint(0, 8)):
            n_id += 1
            created = t + random.randint(0, 3599)
            yield {"id": str(n_id), "created_at": created,
                   "statistics": {"first_admin_reply_at": created + random.choice([120, 600, 1800])}}
        t += 3600


intercom_client.search_conversations = fake_search_conversations
intercom_client.get_conversation = lambda token, cid: {"conversation_parts": {"conversation_parts": []}}

from streamlit.testing.v1 import AppTest

today = datetime.now(CT).date()
users = {"erwin": "pw-erwin", "weng": "pw-weng", "kristine": "pw-kristine"}


def new_app() -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=180)
    at.secrets["LEAVE_USERS"] = users
    return at


def by_label(elements, label):
    return next(e for e in elements if e.label == label)


def flash(at):
    return [m.value for m in list(at.success) + list(at.warning) + list(at.error)]


at = new_app()
at.run()
assert not at.exception, at.exception
assert any("Leave log connected" in s.value for s in at.sidebar.success), "sidebar should show the leave log is connected"
print("1. app loads with the leave log connected")

# ---- sign in ------------------------------------------------------------
by_label(at.selectbox, "Name").select("erwin")
pw = by_label(at.text_input, "Password")
pw.input("wrong")
next(b for b in at.button if b.label == "Sign in").click().run()
assert any("Wrong password" in e.value for e in at.error), "wrong password should be rejected"
assert "leave_user" not in at.session_state
by_label(at.text_input, "Password").input("pw-erwin")
next(b for b in at.button if b.label == "Sign in").click().run()
assert at.session_state["leave_user"] == "Erwin", at.session_state
print("2. wrong password rejected, correct password signs in as Erwin")

# ---- add a leave (Erwin works Mon-Sat 4-11); cover the next three days ---
start, end = today + timedelta(days=1), today + timedelta(days=3)
at.selectbox(key="lv_add_agent").select("ERWIN")
at.date_input(key="lv_add_dates").set_value((start, end))
at.text_input(key="lv_add_note").input("Family trip")
at.run()
assert not at.exception, at.exception
assert any("Coverage check" in (m.value if hasattr(m, "value") else "") for m in list(at.success) + list(at.warning)), \
    "live coverage preview should show before saving"
at.button(key="lv_save_new").click().run()
assert not at.exception, at.exception
assert len(FAKE_DB.leaves) == 1 and FAKE_DB.leaves[0]["created_by"] == "Erwin" and FAKE_DB.leaves[0]["note"] == "Family trip"
assert len(FAKE_NOTION.pages) == 1, "leave should be copied to Notion"
assert any("Logged leave for ERWIN" in m for m in flash(at)), flash(at)
print("3. Erwin logged a leave: saved in Supabase, copied to Notion, confirmation shown")

# ---- overlapping leave is blocked in the UI -----------------------------
at.selectbox(key="lv_add_agent").select("ERWIN")
at.date_input(key="lv_add_dates").set_value((start, start))
at.run()
assert any("already has a leave" in e.value for e in at.error)
assert at.button(key="lv_save_new").disabled
print("4. overlapping leave for the same agent is blocked with a clear message")

# ---- Weng edits it, Kristine cancels it (same access as Erwin) ----------
at.session_state["leave_user"] = "Weng"
at.selectbox(key="lv_add_agent").select("MARK")  # unrelated pick to prove the edit flow is independent
at.run()
lid = FAKE_DB.leaves[0]["id"]
at.selectbox(key="lv_edit_pick").select(lid).run()
at.text_input(key=f"lv_e_note_{lid}").input("Family trip (extended)")
at.run()
at.button(key="lv_save_edit").click().run()
assert FAKE_DB.leaves[0]["note"] == "Family trip (extended)" and FAKE_DB.leaves[0]["updated_by"] == "Weng"
assert any(a["action"] == "edited" and a["actor"] == "Weng" for a in FAKE_DB.audit)
print("5. Weng edited the leave; history records Weng")

at.session_state["leave_user"] = "Kristine"
at.selectbox(key="lv_edit_pick").select(lid).run()
at.checkbox(key=f"lv_confirm_{lid}").check().run()
at.button(key="lv_cancel_btn").click().run()
assert FAKE_DB.leaves[0]["status"] == "cancelled"
assert [a["action"] for a in FAKE_DB.audit] == ["added", "edited", "cancelled"]
assert next(iter(FAKE_NOTION.pages.values()))["properties"]["Status"]["select"]["name"] == "Cancelled"
print("6. Kristine cancelled it; history and Notion both show Cancelled")

# ---- leave drives the forecast and the daily table ---------------------
# Re-add Erwin's leave, then build the forecast and check leave shows up in it.
at.session_state["leave_user"] = "Erwin"
at.selectbox(key="lv_add_agent").select("ERWIN")
at.date_input(key="lv_add_dates").set_value((start, end))
at.run()
at.button(key="lv_save_new").click().run()
assert sum(1 for r in FAKE_DB.leaves if r["status"] == "active") == 1

at.button(key="fc_build").click().run()
assert not at.exception, at.exception
metrics = {m.label: m.value for m in at.metric}
assert metrics["Agent-hours on leave"] != "0", metrics
assert "Expected tickets" in metrics
print("7. forecast built; agent-hours on leave:", metrics["Agent-hours on leave"],
      "| understaffed hours:", metrics["Understaffed hours"],
      "| short only because of leave:", metrics["Short only because of leave"])

# Daily tab: put Erwin on leave today (if he is rostered) and confirm the table column exists and is counted.
at.session_state["leave_user"] = "Erwin"
at.selectbox(key="lv_add_agent").select("JOJO")  # works Mon-Fri all day, Sun 18-23, Sat 0-1
at.date_input(key="lv_add_dates").set_value((today, today))
at.run()
at.button(key="lv_save_new").click().run()
assert not at.exception, at.exception
daily_tables = [d for d in at.dataframe if "On leave" in list(d.value.columns)]
assert daily_tables, "Daily hour-by-hour table should have an 'On leave' column"
print("8. daily table shows the 'On leave' column; no exceptions")

# ---- signed-out viewers do not see the log ------------------------------
fresh = new_app()
fresh.run()
assert not fresh.exception
assert any("Sign in" in b.label for b in fresh.button)
assert not any("Family trip" in str(d.value) for d in fresh.dataframe)
print("9. a signed-out viewer sees only the sign-in form")
print("APPTEST OK")
