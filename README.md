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
- **Actual online agents (optional)** come live from Slack's `#secret-cc-cafe`
  channel, where agents post to log in, go on break, come back, and log out.
  This is shown *alongside* the planned roster, not instead of it - the gap
  between "scheduled" and "actual" is itself useful (a no-show, a late start,
  a missed logout). See `attendance.py`'s docstring for exactly how messages
  are read and matched to a roster agent, and "Getting a Slack bot token"
  below for setup. Entirely optional: without a Slack token configured, the
  app runs exactly as before, just without this comparison.

## Files

```
app.py              Streamlit UI (Daily staffing + Trends tabs)
intercom_client.py  Talks to Intercom's REST API directly (needs its own token)
pipeline.py         Turns raw conversations into an hourly ticket/SLA table
roster.py           Loads the scheduled-agents lookup; also matches a Slack
                     poster to a roster agent code (see attendance.py)
attendance.py        Reads #secret-cc-cafe on Slack and reconstructs actual
                     online/break time per agent per hour (optional data source)
recommend.py        Capacity estimate + Increase/Sufficient/Decrease logic
style.py            Shared chart colors
data/roster_schedule.csv   Parsed roster (see "Updating the roster")
requirements.txt
.streamlit/secrets.toml.example
smoke_test.py, apptest_check.py, attendance_smoke_test.py
                     Optional offline sanity checks (no real Intercom/Slack calls)
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

## Getting a Slack bot token (optional - only for "actual online" agents)

Skip this if you don't want the actual-vs-scheduled comparison; the app works
fine without it. To enable it:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app
   ("From scratch") in ZEVO's workspace.
2. Under **OAuth & Permissions -> Scopes -> Bot Token Scopes**, add:
   - `groups:history` (read message history in private channels - `#secret-cc-cafe`
     is private)
   - `users:read` (resolve a poster's display/real name)
   - `users:read.email` (optional - helps match a couple of ambiguous names, see
     `attendance.py`'s docstring; not required)
3. Click **Install to Workspace**, then copy the **Bot User OAuth Token**
   (starts with `xoxb-`).
4. In Slack, go to `#secret-cc-cafe` and **invite the bot** you just created
   (`/invite @your-bot-name`) - Slack won't let a bot read a private channel's
   history until it's a member.
5. Add `SLACK_BOT_TOKEN` (and, only if the channel ID ever changes,
   `SLACK_CHANNEL_ID`) to Streamlit secrets - see `.streamlit/secrets.toml.example`.

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
   # optional - only if you set up the Slack bot token above:
   # SLACK_BOT_TOKEN = "paste-your-real-slack-bot-token-here"
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
If Slack attendance is configured, that window also pulls the matching
history from `#secret-cc-cafe`, which adds its own (smaller) round of API
calls on first load - also cached for 30 minutes.

## Known limitations / things worth revisiting

- "Scheduled agents" is a planned roster, not proof anyone was actually
  online - it can't account for call-outs, no-shows, or overtime. That's
  exactly what the optional Slack-based "actual online" series is for; see
  above.
- The capacity estimate is a statistical heuristic over whatever window is
  currently loaded; it will shift as more data comes in, and can look strange
  with very little history (that's what the "(default, low data)" label
  means). It's still based on the *planned* roster, not actual attendance -
  worth revisiting once there's enough Slack history to consider basing it
  on actual instead.
- The Slack-based attendance parser is keyword-based (see `attendance.py`'s
  docstring for the exact phrases) - it was checked against real channel
  history, but a genuinely new phrasing, a missed "back" after a "break", or
  someone forgetting to log out will show up as a gap rather than a guess.
  A handful of Slack posters may also come back "unmatched" if their Slack
  name doesn't clearly map to exactly one roster agent (the app surfaces
  these rather than hiding them - see the expander under the daily chart).
  One resolved case, kept here as an example: the roster lists both
  `MICHELLE` and `MITCH` as separate agent codes, and the Slack account
  matching both by name/email alone was genuinely ambiguous. Confirmed with
  Weng - Slack's "Mitch" is roster's `MICHELLE`, and Slack's "Chelle" (which
  already matched fine on its own) is roster's `MICHELL` - so `attendance.py`
  has a small `MANUAL_EMAIL_OVERRIDES` table for exactly that one confirmed
  case. If another poster ever comes back "unmatched" for the same kind of
  reason, add them there the same way rather than loosening the general
  matching rule.
