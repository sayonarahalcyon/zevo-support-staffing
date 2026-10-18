"""One-way backup of the vacation-leave log from Supabase into a Notion database.

Supabase is the source of truth. This module only ever writes TO Notion; it
never reads leave data back from it, so editing a row in Notion does not
change what the forecast uses (the next sync overwrites it).

Two ways it runs:
  - sync_row(): called by the app right after a leave is added, edited or
    cancelled. Best effort: if Notion is slow or down, the leave is still
    saved in Supabase and the nightly sync catches it up.
  - sync_all(): run by sync_notion.py (nightly via GitHub Actions) to bring
    the whole database in line, including anything sync_row missed.

Notion database properties (create_database_schema() lists them):
  Name (title), Agent (select), Dates (date range), Status (select),
  Note, Logged by, Last updated by, Supabase ID (all text).

Uses Notion API version 2022-06-28. Notion allows about 3 requests a second
on average, so the bulk sync paces itself.
"""
from __future__ import annotations

import time

import requests

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"
TIMEOUT = 30
PACE_SECONDS = 0.4


class NotionBackupError(Exception):
    pass


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}


def _request(method: str, token: str, path: str, body: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            resp = requests.request(method, f"{API}/{path}", headers=_headers(token), json=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise NotionBackupError(f"Couldn't reach Notion: {e}") from e
            time.sleep(1 + attempt)
            continue
        if resp.status_code == 429 and attempt < retries - 1:
            time.sleep(float(resp.headers.get("Retry-After", "1")) + 0.5)
            continue
        if resp.status_code in (401, 403):
            raise NotionBackupError(
                "Notion rejected the token, or the integration hasn't been shared with the backup database "
                "(database page -> ... menu -> Connections -> add the integration)."
            )
        if resp.status_code == 404:
            raise NotionBackupError("Notion can't find that database. Check NOTION_LEAVE_DB_ID and that the integration is connected to it.")
        if not resp.ok:
            raise NotionBackupError(f"Notion error {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    raise NotionBackupError("Notion kept rate limiting the request.")


def _text(value: str | None) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": (value or "")[:1900]}}]}


def leave_to_properties(leave: dict) -> dict:
    """Notion page properties for one leave record."""
    start, end = str(leave["start_date"])[:10], str(leave["end_date"])[:10]
    status = "Active" if (leave.get("status") or "active") == "active" else "Cancelled"
    dates = {"start": start} if start == end else {"start": start, "end": end}
    return {
        "Name": {"title": [{"type": "text", "text": {"content": f"{leave['agent_code']}: {start} to {end}"}}]},
        "Agent": {"select": {"name": str(leave["agent_code"])}},
        "Dates": {"date": dates},
        "Status": {"select": {"name": status}},
        "Note": _text(leave.get("note")),
        "Logged by": _text(leave.get("created_by")),
        "Last updated by": _text(leave.get("updated_by")),
        "Supabase ID": _text(str(leave["id"])),
    }


def _fingerprint_from_leave(leave: dict) -> tuple:
    status = "Active" if (leave.get("status") or "active") == "active" else "Cancelled"
    return (str(leave["id"]), str(leave["agent_code"]), str(leave["start_date"])[:10], str(leave["end_date"])[:10],
            status, (leave.get("note") or "").strip(), leave.get("created_by") or "", leave.get("updated_by") or "")


def _plain(prop: dict) -> str:
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", [])).strip()


def _fingerprint_from_page(page: dict) -> tuple:
    p = page["properties"]
    dates = (p.get("Dates") or {}).get("date") or {}
    start = (dates.get("start") or "")[:10]
    end = (dates.get("end") or start)[:10]
    return (_plain(p.get("Supabase ID", {})), ((p.get("Agent") or {}).get("select") or {}).get("name", ""),
            start, end, ((p.get("Status") or {}).get("select") or {}).get("name", ""),
            _plain(p.get("Note", {})), _plain(p.get("Logged by", {})), _plain(p.get("Last updated by", {})))


def _find_page(token: str, db_id: str, supabase_id: str) -> dict | None:
    res = _request("POST", token, f"databases/{db_id}/query", {
        "filter": {"property": "Supabase ID", "rich_text": {"equals": str(supabase_id)}}, "page_size": 1,
    })
    results = res.get("results", [])
    return results[0] if results else None


def sync_row(token: str, db_id: str, leave: dict) -> str:
    """Creates or updates the Notion page for one leave. Returns 'created' or 'updated'."""
    page = _find_page(token, db_id, leave["id"])
    props = leave_to_properties(leave)
    if page:
        _request("PATCH", token, f"pages/{page['id']}", {"properties": props})
        return "updated"
    _request("POST", token, "pages", {"parent": {"database_id": db_id}, "properties": props})
    return "created"


def _all_pages(token: str, db_id: str) -> list[dict]:
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _request("POST", token, f"databases/{db_id}/query", body)
        pages.extend(res.get("results", []))
        if not res.get("has_more"):
            return pages
        cursor = res.get("next_cursor")


def sync_all(token: str, db_id: str, leaves: list[dict], pace: float = PACE_SECONDS) -> dict:
    """Brings the Notion database in line with `leaves` (all records, including
    cancelled). Skips rows that already match. Never deletes Notion pages.
    Returns counts: created, updated, unchanged, failed (plus 'errors')."""
    existing = {}
    for page in _all_pages(token, db_id):
        sid = _plain(page["properties"].get("Supabase ID", {}))
        if sid:
            existing[sid] = page
    counts = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0, "errors": []}
    for leave in leaves:
        sid = str(leave["id"])
        page = existing.get(sid)
        try:
            if page and _fingerprint_from_page(page) == _fingerprint_from_leave(leave):
                counts["unchanged"] += 1
                continue
            if page:
                _request("PATCH", token, f"pages/{page['id']}", {"properties": leave_to_properties(leave)})
                counts["updated"] += 1
            else:
                _request("POST", token, "pages",
                         {"parent": {"database_id": db_id}, "properties": leave_to_properties(leave)})
                counts["created"] += 1
            time.sleep(pace)
        except NotionBackupError as e:
            counts["failed"] += 1
            counts["errors"].append(f"{leave.get('agent_code')} {leave.get('start_date')}: {e}")
    return counts


def database_schema_properties() -> dict:
    """The properties the backup database needs, in Notion API form. Used to
    create the database (and to check an existing one has them)."""
    return {
        "Name": {"title": {}},
        "Agent": {"select": {}},
        "Dates": {"date": {}},
        "Status": {"select": {"options": [{"name": "Active", "color": "green"}, {"name": "Cancelled", "color": "gray"}]}},
        "Note": {"rich_text": {}},
        "Logged by": {"rich_text": {}},
        "Last updated by": {"rich_text": {}},
        "Supabase ID": {"rich_text": {}},
    }
