# ZEVO Support Staffing Dashboard

Shows ticket volume per hour against scheduled agents per hour, flags hours
that are understaffed or overstaffed against a 15-minute first-response goal,
and gives a weekly/monthly trend view.

## What it actually measures (read this before trusting the numbers)

- **Ticket volume & response times** come live from Intercom via its REST API.
- **Scheduled agents** come from `data/roster_schedule.csv`, generated from
  ZEVO's "ZEVO Workforce" Google Sheet, **CST tab** (confirmed with Weng - not
  the PROPOSED tab). That tab lists, for every hour of every day of the week
  (a standard midnight-to-midnight calendar day, since the team runs 24/7),
  which named agents are scheduled. "Scheduled agents" for an hour is the
  count of distinct names listed there. It's a **recurring weekly template**,
  not a record of who actually clocked in on a given date. If the real
  schedule changes, or ZEVO starts tracking actual clock-ins somewhere, this
  file needs to be regenerated or swapped for a live source - see "Updating
  the roster" below.
- The schedule's hours are assumed to be **US Central time (America/Chicago)**,
  read from the "CST" column header. If that's wrong, everything is off by a
  timezone - worth double-checking with whoever maintains that sheet.
- **"Sufficient / Increase / Decrease"** is a heuristic, not a fact: it
  estimates a sustainable "tickets per agent per hour" capacity from hours in
  the loaded window that already met the SLA target, then compares each hour's
  scheduled agents to how many that estimate says are needed. With little
  history it falls back to a flat default (4 tickets/agent/hour) and says so
  in the UI. Treat the recommendation as a starting point for a conversation,
  not an automatic staffing decision.
- Conversations Fin (the AI agent) fully resolves without ever looping in a
  human are counted in total ticket volume but excluded from the SLA
  hit-rate, since they never needed agent capacity.

## Files

```
app.py              Streamlit UI (Daily staffing + Trends tabs)
intercom_client.py  Talks to Intercom's REST API directly (needs its own token)
pipeline.py         Turns raw conversations into an hourly ticket/SLA table
roster.py           Loads the scheduled-agents lookup, handles the shift-day quirk
recommend.py        Capacity estimate + Increase/Sufficient/Decrease logic
style.py            Shared chart colors
data/roster_schedule.csv   Parsed roster (see "Updating the roster")
requirements.txt
.streamlit/secrets.toml.example
smoke_test.py, apptest_check.py   Optional offline sanity checks (no real Intercom calls)
```

## Running it locally first (optional but recommended)

```bash
cd streamlit_app
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml and paste in a real Intercom access token
streamlit run app.py
```

## Getting an Intercom access token

This needs to be a token separate from any Claude/Intercom connection - the
deployed app calls Intercom's API on its own.

In Intercom: **Settings -> Integrations -> Developer Hub**, create a new
internal app (or use an existing one), then under that app's
**Authentication** section generate an **Access Token**. Make sure it has
**read** access to Conversations. (Intercom's UI shifts around between plans
and versions - if this path doesn't match what you see, search Intercom's own
help docs for "access token" or ask whoever administers your Intercom
workspace.)

## Deploying to Streamlit Community Cloud

1. **Push this folder to a GitHub repo.** Create a new repo on github.com
   (e.g. `zevo-support-staffing`), then from inside this `streamlit_app`
   folder:
   ```bash
   git init
   git add .
   git commit -m "Initial staffing dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-username>/zevo-support-staffing.git
   git push -u origin main
   ```
   (`.streamlit/secrets.toml` is not included here - only the `.example` file
   is, so you won't accidentally commit a real token. Never add a real
   `secrets.toml` to git.)

2. **Go to** [share.streamlit.io](https://share.streamlit.io) **and sign in**
   with the account connected to your GitHub.

3. Click **"New app"**, pick the repo and branch you just pushed, and set
   **Main file path** to `app.py`.

4. Before clicking Deploy, open **"Advanced settings" -> Secrets** and paste:
   ```toml
   INTERCOM_ACCESS_TOKEN = "paste-your-real-token-here"
   ```

5. Click **Deploy**. First load will take a minute while it installs
   dependencies and pulls the first batch of Intercom data.

## Updating the roster later

`data/roster_schedule.csv` is a static snapshot parsed from the Google Sheet
at the time this app was built. If the schedule changes:
- Re-export the sheet's PROPOSED tab and re-run the same parsing logic used to
  generate this CSV (ask whoever maintains this app to regenerate it), or
- Hand-edit `data/roster_schedule.csv` directly - it's a plain CSV with
  columns `day_of_week, hour_cst, hour_ph, scheduled_agents, counted_agents, agents`.
  Only `day_of_week`, `hour_cst`, and `scheduled_agents` are actually used by
  the app; the rest is informational.

## Performance note on the Trends tab

This Intercom workspace has a large conversation history. Pulling 30-90 days
of full conversation data can mean thousands of API calls and take a couple
of minutes on first load (results are cached for 30 minutes after that,
per date range). Start with "Last 7 days" if you just want a quick check.

## Known limitations / things worth revisiting

- "Scheduled agents" is a planned roster, not proof anyone was actually
  online - it can't account for call-outs, no-shows, or overtime.
- The capacity estimate is a statistical heuristic over whatever window is
  currently loaded; it will shift as more data comes in, and can look strange
  with very little history (that's what the "(default, low data)" label
  means).
- If ZEVO later wants "agents per hour" to mean actual admin activity
  (who really replied that hour) rather than the schedule, that's also
  derivable from Intercom (via each conversation's assignee/teammates) and
  can be added as a second series alongside the roster-based one.
