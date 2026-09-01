"""Thin wrapper around Intercom's Conversations Search REST API.

This app runs on Streamlit Cloud, completely separate from any Claude/MCP
session, so it authenticates with its own Intercom access token (read from
Streamlit secrets or an environment variable) and talks to Intercom's public
REST API directly.

Docs: https://developers.intercom.com/docs/references/rest-api/api.intercom.io/conversations/searchconversations
"""
from __future__ import annotations

import time
from typing import Iterator

import requests

API_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "2.11"
PAGE_SIZE = 150
MAX_RETRIES = 4


class IntercomAuthError(RuntimeError):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Intercom-Version": INTERCOM_VERSION,
    }


def search_conversations(token: str, created_after: int, created_before: int) -> Iterator[dict]:
    """Yield every conversation created in [created_after, created_before) (unix seconds).

    Handles pagination and basic rate-limit backoff. Raises IntercomAuthError on 401/403
    so the app can show a clear "check your token" message instead of a stack trace.
    """
    query = {
        "operator": "AND",
        "value": [
            {"field": "created_at", "operator": ">", "value": created_after},
            {"field": "created_at", "operator": "<", "value": created_before},
        ],
    }
    body = {"query": query, "pagination": {"per_page": PAGE_SIZE}}

    starting_after = None
    while True:
        if starting_after:
            body["pagination"] = {"per_page": PAGE_SIZE, "starting_after": starting_after}

        resp = None
        for attempt in range(MAX_RETRIES):
            resp = requests.post(f"{API_BASE}/conversations/search", json=body, headers=_headers(token), timeout=30)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            break

        if resp.status_code in (401, 403):
            raise IntercomAuthError(
                "Intercom rejected the access token (401/403). Check the INTERCOM_ACCESS_TOKEN secret."
            )
        resp.raise_for_status()
        data = resp.json()

        for conv in data.get("conversations", []):
            yield conv

        next_page = (data.get("pages") or {}).get("next")
        if not next_page or not next_page.get("starting_after"):
            break
        starting_after = next_page["starting_after"]


def get_conversation(token: str, conversation_id: str) -> dict:
    """Fetch one conversation's full detail, including its conversation_parts
    thread (every reply, note, and status change, each with its own
    created_at and author).

    This is a different endpoint from search_conversations above ("Retrieve a
    conversation", not "Search conversations") - the search endpoint only
    returns a summary plus aggregate `statistics` fields (like
    first_admin_reply_at), never the parts themselves. Computing per-hour
    "worked on"/"closed" activity needs the parts, so it costs one extra GET
    per conversation on top of the search call.

    Docs: https://developers.intercom.com/docs/references/rest-api/api.intercom.io/conversations/retrieveconversation
    """
    resp = None
    for attempt in range(MAX_RETRIES):
        resp = requests.get(f"{API_BASE}/conversations/{conversation_id}", headers=_headers(token), timeout=30)
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        break

    if resp.status_code in (401, 403):
        raise IntercomAuthError(
            "Intercom rejected the access token (401/403). Check the INTERCOM_ACCESS_TOKEN secret."
        )
    resp.raise_for_status()
    return resp.json()
