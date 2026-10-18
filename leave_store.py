"""Reads and writes the vacation-leave log in Supabase (PostgREST over HTTPS).

Uses plain `requests` (already a dependency) rather than the supabase client,
so the deployed app needs nothing new. Run supabase/schema.sql once first.

Auth: SUPABASE_URL plus SUPABASE_SERVICE_KEY, both in Streamlit secrets. The
key is used only on the server (Streamlit runs your code server-side), never
sent to the browser. Do not put it in the repo.

Audit history is written by database triggers (see schema.sql), so every add,
edit and cancel is recorded atomically with the change. The app only has to
say who is acting (created_by / updated_by).
"""
from __future__ import annotations

from datetime import date

import requests

TIMEOUT = 20


class LeaveStoreError(Exception):
    """Anything that stops a leave read or write; the message is user-facing."""


class LeaveOverlapError(LeaveStoreError):
    """The agent already has an active leave overlapping those dates."""


class SupabaseLeaveStore:
    def __init__(self, url: str, key: str, session: requests.Session | None = None):
        self.base = url.rstrip("/") + "/rest/v1"
        self.key = key
        self.http = session or requests.Session()

    # ------------------------------------------------------------- plumbing --
    def _headers(self, write: bool = False) -> dict:
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        # Legacy service_role keys are JWTs and are also sent as a bearer token.
        # Newer "secret" keys (sb_secret_...) are not JWTs and go in apikey only.
        if self.key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.key}"
        if write:
            headers["Prefer"] = "return=representation"
        return headers

    def _call(self, method: str, path: str, *, params=None, json=None, write: bool = False):
        try:
            resp = self.http.request(
                method, f"{self.base}/{path}", headers=self._headers(write),
                params=params, json=json, timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise LeaveStoreError(f"Couldn't reach Supabase: {e}") from e
        if resp.status_code in (401, 403):
            raise LeaveStoreError(
                "Supabase rejected the key. Check SUPABASE_URL and SUPABASE_SERVICE_KEY in Streamlit secrets."
            )
        if resp.status_code == 404:
            raise LeaveStoreError(
                "Supabase says the table doesn't exist. Run supabase/schema.sql in the Supabase SQL editor first."
            )
        if resp.status_code == 409 or (resp.status_code == 400 and "23P01" in resp.text):
            raise LeaveOverlapError("That agent already has a leave overlapping those dates.")
        if not resp.ok:
            raise LeaveStoreError(f"Supabase error {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return []
        return resp.json()

    # ----------------------------------------------------------------- reads --
    def list_leaves(self, include_cancelled: bool = False) -> list[dict]:
        params = {"select": "*", "order": "start_date.asc,agent_code.asc"}
        if not include_cancelled:
            params["status"] = "eq.active"
        return self._call("GET", "leaves", params=params)

    def list_audit(self, limit: int = 200) -> list[dict]:
        return self._call("GET", "leave_audit", params={"select": "*", "order": "at.desc", "limit": str(limit)})

    # ---------------------------------------------------------------- writes --
    @staticmethod
    def _iso(d) -> str:
        return d.isoformat() if isinstance(d, date) else str(d)[:10]

    def add(self, agent_code: str, start, end, note: str, actor: str) -> dict:
        body = {
            "agent_code": agent_code.upper(), "start_date": self._iso(start), "end_date": self._iso(end),
            "note": (note or "").strip() or None, "created_by": actor, "updated_by": actor,
        }
        rows = self._call("POST", "leaves", json=body, write=True)
        return rows[0]

    def update(self, leave_id: str, agent_code: str, start, end, note: str, actor: str) -> dict:
        body = {
            "agent_code": agent_code.upper(), "start_date": self._iso(start), "end_date": self._iso(end),
            "note": (note or "").strip() or None, "updated_by": actor,
        }
        rows = self._call("PATCH", "leaves", params={"id": f"eq.{leave_id}", "status": "eq.active"},
                          json=body, write=True)
        if not rows:
            raise LeaveStoreError("That leave no longer exists or was already cancelled. Refresh and try again.")
        return rows[0]

    def cancel(self, leave_id: str, actor: str) -> dict:
        rows = self._call("PATCH", "leaves", params={"id": f"eq.{leave_id}", "status": "eq.active"},
                          json={"status": "cancelled", "updated_by": actor}, write=True)
        if not rows:
            raise LeaveStoreError("That leave was already cancelled. Refresh and try again.")
        return rows[0]
