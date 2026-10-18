"""Offline checks for the leave log, forecast and Notion backup. No network.

Run:  python leave_test.py     (or: pytest leave_test.py)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import forecast
import leave
import leave_store
import notion_backup
import roster

CT = ZoneInfo("America/Chicago")
MON = date(2026, 11, 2)   # a Monday


def L(agent, start, end, status="active", id_=None, **extra):
    return {"id": id_ or f"{agent}-{start}", "agent_code": agent, "start_date": start, "end_date": end,
            "status": status, **extra}


# ------------------------------------------------------------------ leave --
def test_leave_by_date_spans_range_and_ignores_cancelled():
    lm = leave.leave_by_date([L("ERWIN", "2026-11-02", "2026-11-04"),
                              L("MARK", "2026-11-03", "2026-11-03", status="cancelled")])
    assert lm[date(2026, 11, 2)] == {"ERWIN"} and lm[date(2026, 11, 4)] == {"ERWIN"}
    assert date(2026, 11, 5) not in lm
    assert "MARK" not in lm[date(2026, 11, 3)]


def test_agent_removed_only_in_hours_they_are_rostered():
    lm = leave.leave_by_date([L("ERWIN", "2026-11-02", "2026-11-02")])  # Erwin works Mon 4-11
    inside = datetime(2026, 11, 2, 5, tzinfo=CT)
    outside = datetime(2026, 11, 2, 15, tzinfo=CT)
    assert "ERWIN" in roster.agents_on_duty(inside)
    assert leave.rostered_on_leave(inside, lm) == ["ERWIN"]
    assert leave.agents_after_leave(inside, lm) == roster.scheduled_agents(inside) - 1
    assert leave.rostered_on_leave(outside, lm) == []
    assert leave.agents_after_leave(outside, lm) == roster.scheduled_agents(outside)
    # Same agent, next day not on leave
    assert leave.rostered_on_leave(datetime(2026, 11, 3, 5, tzinfo=CT), lm) == []


def test_annotate_hourly_matches_roster_when_no_leave():
    hours = [datetime(2026, 11, 2, h, tzinfo=CT) for h in range(24)]
    df = pd.DataFrame({"hour": hours, "scheduled_agents": [roster.scheduled_agents(h) for h in hours]})
    out = leave.annotate_hourly(df, {})
    assert (out["on_leave"] == 0).all()
    assert (out["scheduled_after_leave"] == out["scheduled_agents"]).all()
    with_leave = leave.annotate_hourly(df, leave.leave_by_date([L("ERWIN", "2026-11-02", "2026-11-02")]))
    assert with_leave["on_leave"].sum() == 8  # Erwin's 8 rostered hours that Monday


def test_overlap_and_validation():
    existing = [L("ERWIN", "2026-11-02", "2026-11-04", id_="a"), L("ERWIN", "2026-12-01", "2026-12-02", status="cancelled")]
    assert leave.find_overlaps(existing, "erwin", "2026-11-04", "2026-11-06")
    assert not leave.find_overlaps(existing, "ERWIN", "2026-11-05", "2026-11-06")
    assert not leave.find_overlaps(existing, "ERWIN", "2026-11-02", "2026-11-04", ignore_id="a")  # editing itself
    assert not leave.find_overlaps(existing, "ERWIN", "2026-12-01", "2026-12-02")  # cancelled does not block
    assert not leave.find_overlaps(existing, "MARK", "2026-11-02", "2026-11-04")
    assert leave.validate("ERWIN", "2026-11-02", "2026-11-03") == []
    assert leave.validate("NOBODY", "2026-11-02", "2026-11-03")
    assert leave.validate("ERWIN", "2026-11-05", "2026-11-03")
    assert leave.validate("ERWIN", "2026-11-01", "2027-03-01")


def test_coverage_warning_counts_other_leaves():
    # Monday 4-9 AM: ERWIN, STELLA, MARK, JULY, NINS (from 6) etc. Take everyone rostered at 4 AM off.
    on_duty = roster.agents_on_duty(datetime(2026, 11, 2, 4, tzinfo=CT))
    others = [L(a, "2026-11-02", "2026-11-02") for a in on_duty if a != "ERWIN"]
    warns = leave.coverage_warnings(others, "ERWIN", "2026-11-02", "2026-11-02", min_agents=1)
    assert any(w["hour"] == 4 and w["remaining"] == 0 for w in warns)
    assert warns[0]["others_off"]
    assert leave.coverage_warnings([], "ERWIN", "2026-11-02", "2026-11-02", min_agents=0) == []


# ------------------------------------------------------------- store (fake) --
class FakePostgrest:
    """Just enough of PostgREST for SupabaseLeaveStore: leaves + audit, with the
    overlap rule and the audit trigger behaviour from schema.sql."""

    def __init__(self):
        self.leaves, self.audit, self.n = [], [], 0
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, dict(headers or {}), params, json))
        table = url.rsplit("/", 1)[-1]
        params = params or {}
        if table == "leave_audit":
            return Resp(200, list(reversed(self.audit)))
        if method == "GET":
            rows = [r for r in self.leaves if params.get("status") != "eq.active" or r["status"] == "active"]
            return Resp(200, rows)
        if method == "POST":
            if any(r["status"] == "active" and r["agent_code"] == json["agent_code"]
                   and r["start_date"] <= json["end_date"] and r["end_date"] >= json["start_date"] for r in self.leaves):
                return Resp(409, {"code": "23P01"})
            self.n += 1
            row = {"id": f"id{self.n}", "status": "active", **json}
            self.leaves.append(row)
            self.audit.append({"leave_id": row["id"], "action": "added", "actor": row["created_by"], "after": dict(row),
                               "at": datetime.now(CT).isoformat()})
            return Resp(201, [row])
        if method == "PATCH":
            rid = params["id"].split(".", 1)[1]
            row = next((r for r in self.leaves if r["id"] == rid and r["status"] == "active"), None)
            if not row:
                return Resp(200, [])
            row.update(json)
            self.audit.append({"leave_id": rid, "action": "cancelled" if row["status"] == "cancelled" else "edited",
                               "actor": json.get("updated_by"), "after": dict(row),
                               "at": datetime.now(CT).isoformat()})
            return Resp(200, [row])
        raise AssertionError(method)


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = status < 400
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._body


def _store(fake=None, key="eyJhbGciOi.legacy.jwt"):
    fake = fake or FakePostgrest()
    return leave_store.SupabaseLeaveStore("https://x.supabase.co/", key, session=fake), fake


def test_store_add_edit_cancel_and_audit():
    store, fake = _store()
    row = store.add("erwin", date(2026, 11, 2), date(2026, 11, 4), " vacation ", "Erwin")
    assert row["agent_code"] == "ERWIN" and row["note"] == "vacation" and row["created_by"] == "Erwin"
    store.update(row["id"], "ERWIN", date(2026, 11, 2), date(2026, 11, 3), "", "Weng")
    assert fake.leaves[0]["note"] is None and fake.leaves[0]["updated_by"] == "Weng"
    store.cancel(row["id"], "Kristine")
    assert store.list_leaves() == []
    assert len(store.list_leaves(include_cancelled=True)) == 1
    assert [a["action"] for a in reversed(store.list_audit())] == ["added", "edited", "cancelled"]
    try:
        store.cancel(row["id"], "Weng")
        raise AssertionError("cancelling twice should fail")
    except leave_store.LeaveStoreError:
        pass


def test_store_overlap_and_auth_headers():
    store, fake = _store()
    store.add("ERWIN", "2026-11-02", "2026-11-04", "", "Erwin")
    try:
        store.add("ERWIN", "2026-11-03", "2026-11-05", "", "Weng")
        raise AssertionError("overlap should fail")
    except leave_store.LeaveOverlapError:
        pass
    assert fake.calls[0][2]["Authorization"].startswith("Bearer eyJ")
    new_style, fake2 = _store(key="sb_secret_abc")
    new_style.list_leaves()
    assert "Authorization" not in fake2.calls[0][2] and fake2.calls[0][2]["apikey"] == "sb_secret_abc"


# ---------------------------------------------------------------- forecast --
def _history(weeks=4, tickets_at=lambda dt: 8 if dt.hour == 10 else 0):
    start = datetime(2026, 10, 5, tzinfo=CT)  # a Monday
    hours = [start + timedelta(hours=i) for i in range(24 * 7 * weeks)]
    df = pd.DataFrame({"hour": hours, "tickets": [tickets_at(h) for h in hours]})
    df["scheduled_agents"] = [roster.scheduled_agents(h) for h in hours]
    return df


def test_baseline_average_and_busy_level():
    hist = _history(tickets_at=lambda dt: [2, 4, 6, 8][(dt.day // 7) % 4] if dt.hour == 10 else 0)
    avg = forecast.baseline_by_weekday_hour(hist, "Average")
    row = avg[(avg.day_name == "Monday") & (avg.hour_of_day == 10)].iloc[0]
    assert row.samples == 4 and 2 <= row.expected_tickets <= 8
    busy = forecast.baseline_by_weekday_hour(hist, "Busy (75th percentile)")
    brow = busy[(busy.day_name == "Monday") & (busy.hour_of_day == 10)].iloc[0]
    assert brow.expected_tickets >= row.expected_tickets


def test_forecast_leave_reduces_availability_and_flags_leave_caused_gap():
    hist = _history()
    base = forecast.baseline_by_weekday_hour(hist, "Average")  # 8 tickets at 10 AM every day
    capacity = 4.0                                              # -> need 2 agents at 10 AM
    day = date(2026, 11, 9)                                     # a Monday
    no_leave = forecast.build_forecast(base, day, 1, {}, capacity)
    row = no_leave[no_leave.hour_of_day == 10].iloc[0]
    assert row.required_agents == 2 and row.on_leave == 0 and row.available_agents == row.roster_agents
    on_duty = roster.agents_on_duty(datetime(2026, 11, 9, 10, tzinfo=CT))
    assert len(on_duty) >= 2
    # Put enough rostered agents on leave that availability drops below 2.
    off = {day: set(on_duty[: len(on_duty) - 1])}
    with_leave = forecast.build_forecast(base, day, 1, off, capacity)
    r2 = with_leave[with_leave.hour_of_day == 10].iloc[0]
    assert r2.available_agents == 1 and r2.gap == 1 and r2.recommendation == "Increase"
    assert r2.recommendation_no_leave != "Increase" and bool(r2.leave_caused)
    summary = forecast.daily_summary(with_leave)
    assert summary.iloc[0].understaffed_due_to_leave >= 1 and summary.iloc[0].agent_hours_on_leave >= 1
    assert on_duty[0] in summary.iloc[0].agents_on_leave


def test_forecast_adjustment_and_quiet_hours():
    hist = _history()
    base = forecast.baseline_by_weekday_hour(hist, "Average")
    plus = forecast.build_forecast(base, date(2026, 11, 9), 1, {}, 4.0, volume_adjust_pct=50)
    assert plus[plus.hour_of_day == 10].iloc[0].forecast_tickets == 12
    assert plus[plus.hour_of_day == 3].iloc[0].required_agents == 0  # no demand, nobody "needed"
    assert plus[plus.hour_of_day == 3].iloc[0].recommendation == "Sufficient"


def test_attendance_factor_clamped():
    hist = _history()
    assert forecast.attendance_factor(hist) is None
    hist["actual_online"] = hist["scheduled_agents"] * 0.1
    assert forecast.attendance_factor(hist) == 0.5
    hist["actual_online"] = hist["scheduled_agents"] * 2
    assert forecast.attendance_factor(hist) == 1.0


# ------------------------------------------------------------------- notion --
class FakeNotion:
    def __init__(self):
        self.pages, self.log = {}, []

    def __call__(self, method, token, path, body=None, retries=3):
        self.log.append((method, path))
        if path.endswith("/query"):
            flt = (body or {}).get("filter")
            pages = list(self.pages.values())
            if flt:
                want = flt["rich_text"]["equals"]
                pages = [p for p in pages if _sid(p) == want]
            return {"results": pages, "has_more": False}
        if method == "POST" and path == "pages":
            pid = f"p{len(self.pages) + 1}"
            self.pages[pid] = {"id": pid, "properties": _as_page_props(body["properties"])}
            return self.pages[pid]
        if method == "PATCH":
            pid = path.split("/")[1]
            self.pages[pid]["properties"] = _as_page_props(body["properties"])
            return self.pages[pid]
        raise AssertionError((method, path))


def _as_page_props(props):
    """Turns request-form properties into the read-form Notion returns."""
    out = {}
    for k, v in props.items():
        if "rich_text" in v:
            out[k] = {"rich_text": [{"plain_text": t["text"]["content"]} for t in v["rich_text"]]}
        else:
            out[k] = v
    return out


def _sid(page):
    return notion_backup._plain(page["properties"]["Supabase ID"])


def test_notion_sync_row_and_sync_all_are_idempotent(monkeypatch=None):
    fake = FakeNotion()
    original = notion_backup._request
    notion_backup._request = fake
    notion_backup.PACE_SECONDS = 0
    try:
        a = L("ERWIN", "2026-11-02", "2026-11-04", id_="u1", note="vac", created_by="Erwin", updated_by="Erwin")
        b = L("MARK", "2026-11-05", "2026-11-05", id_="u2", created_by="Weng", updated_by="Weng")
        assert notion_backup.sync_row("t", "db", a) == "created"
        assert notion_backup.sync_row("t", "db", a) == "updated"
        assert len(fake.pages) == 1
        counts = notion_backup.sync_all("t", "db", [a, b], pace=0)
        assert counts["created"] == 1 and counts["unchanged"] == 1 and counts["failed"] == 0
        b2 = {**b, "status": "cancelled", "updated_by": "Kristine"}
        counts = notion_backup.sync_all("t", "db", [a, b2], pace=0)
        assert counts["updated"] == 1 and counts["unchanged"] == 1
        page_b = next(p for p in fake.pages.values() if _sid(p) == "u2")
        assert page_b["properties"]["Status"]["select"]["name"] == "Cancelled"
        # Single-day leave has no end date in Notion (a one-day range)
        assert page_b["properties"]["Dates"]["date"] == {"start": "2026-11-05"}
        assert len(fake.pages) == 2  # never duplicated
    finally:
        notion_backup._request = original


def test_notion_failure_is_counted_not_raised():
    original = notion_backup._request

    def boom(method, token, path, body=None, retries=3):
        if path.endswith("/query"):
            return {"results": [], "has_more": False}
        raise notion_backup.NotionBackupError("rate limited")

    notion_backup._request = boom
    try:
        counts = notion_backup.sync_all("t", "db", [L("ERWIN", "2026-11-02", "2026-11-02", id_="u1")], pace=0)
        assert counts["failed"] == 1 and "rate limited" in counts["errors"][0]
    finally:
        notion_backup._request = original


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"{len(tests)} checks passed")
