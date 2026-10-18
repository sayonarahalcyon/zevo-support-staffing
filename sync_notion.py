"""Nightly job: copy every vacation leave from Supabase into the Notion backup.

Run by .github/workflows/notion_backup.yml, or by hand:

    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... NOTION_TOKEN=... NOTION_LEAVE_DB_ID=... \
        python sync_notion.py

Exits non-zero if anything failed, so a failed night shows up as a red run in
the repo's Actions tab.
"""
from __future__ import annotations

import os
import sys

import leave_store
import notion_backup


def main() -> int:
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "NOTION_TOKEN", "NOTION_LEAVE_DB_ID")
               if not os.environ.get(k)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return 2
    store = leave_store.SupabaseLeaveStore(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    leaves = store.list_leaves(include_cancelled=True)
    print(f"{len(leaves)} leave record(s) in Supabase")
    counts = notion_backup.sync_all(os.environ["NOTION_TOKEN"], os.environ["NOTION_LEAVE_DB_ID"], leaves)
    print({k: v for k, v in counts.items() if k != "errors"})
    for err in counts["errors"]:
        print("FAILED:", err)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
