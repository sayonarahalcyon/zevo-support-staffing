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
  the loaded window that already met the SLA target, then compares each
  hour's *effective* agent count (see below) to how many that estimate says
  are needed. With little history it falls back to a flat default (4
  tickets/agent/hour) and says so in the UI. Treat the recommendation as a
  starting point for a conversation, not an automatic staffing decision.
- Conversations Fin (the AI agent) fully resolves without ever looping in a
  human are counted in total ticket volume but excluded from the SLA
  hit-rate, since they never needed agent capacity.
- **Actual online agents (optional)** come live from Slack's `#secret-cc-cafe`
  channel, where agents post to log in, go on break, come back, and log out.
  See `attendance.py`'s docstring for exactly how messages are read and
  matched to a roster agent, and "Getting a Slack bot token" below for setup.
  Entirely optional: without a Slack token configured, the app runs exactly
  as before, using the roster for every staffing decision.
- **Created / Worked on / Closed (Daily tab's hour-by-hour table)**: "Created"
  is the same ticket-volume number as the chart above it, just renamed in
  this one table. "Worked on" counts tickets, created that same day, that
  got a conversation part authored by a human admin (not Fin AI) landing in
  that hour; "Closed" counts ones with a part marked as a close landing in
  that hour (an agent's own close action or Intercom's auto-close both
  count). An admin's close action counts toward *both* columns for that
  hour, since closing a ticket is itself a human action. **Scoping limit:**
  both columns only look at tickets *created within the selected day* - if
  someone works on or closes, this hour, a ticket that was created on an
  earlier day, that action won't show up anywhere in this table. This keeps
  the added Intercom cost bounded (one extra API call per ticket created
  that day, since - unlike ticket volume - "worked on"/"closed" needs each
  conversation's full reply history, which only Intercom's "retrieve a
  conversation" endpoint returns, not the search endpoint the rest of this
  app uses). Loaded and cached separately from the rest of the Daily tab, so
  a hiccup fetching it doesn't take down the other numbers. Not available on
  the Trends tab - doing this per-ticket fetch over a 30/90-day window would
  mean thousands of extra API calls.
- **Which one drives staffing decisions:** confirmed with Weng - the roster
  sheet isn't updated daily and misses leave, no-shows, and someone covering
  an off day, so it's the fallback, not the primary source, whenever Slack
  has better information. Concretely: for every hour, `effective_agents` is
  the actual-online headcount from Slack if that hour has Slack data, and
  the scheduled roster only for hours Slack has no data for (bot not
  configured, or nobody posted). The recommendation, "Hours understaffed",
  and the capacity estimate are all based on `effective_agents` - never the
  roster alone once Slack is configured. The roster is still shown on its
  own (the green bars, the "Scheduled" column) since the plan-vs-reality gap
  is itself useful, but it's a comparison now, not the basis for the
  recommendation.
  **Leave:** when the leave log is connected, the roster fallback is the
  roster *minus agents on approved leave that date* (the "On leave" column), so
  a logged vacation no longer inflates the headcount for hours Slack has no
  data for. With no leave log configured, this is identical to before. The hour-by-hour table's **"Effective (basis)"** column
  shows exactly which number was used for each hour.

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
leave.py            Pure leave logic: who is off on which dates, roster minus leave,
                     overlap and coverage checks
leave_store.py      Reads/writes the leave log in Supabase (adds, edits, cancels)
forecast.py         Weekday-hour demand baseline + leave-adjusted staffing forecast
planning_ui.py      The Forecast and Leave log screens (and the sign-in form)
notion_backup.py    One-way copy of the leave log into a Notion database
sync_notion.py      Nightly job that re-copies everything to Notion
supabase/schema.sql Tables, overlap rule and change-history triggers (run once)
.github/workflows/notion_backup.yml   Runs sync_notion.py every night
data/roster_schedule.csv   Parsed roster (see "Updating the roster")
requirements.txt
.streamlit/secrets.toml.example
smoke_test.py, apptest_check.py, attendance_smoke_test.py, recommend_test.py,
apptest_custom_range_check.py, ticket_activity_test.py, leave_test.py,
apptest_leave_check.py
                     Optional offline sanity checks (no real Intercom/Slack/Supabase/Notion calls)
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

## Vacation leave log and forecast

Two tabs sit next to Daily staffing and Trends.

**Leave log** is where approved vacation leaves are entered. Erwin, Weng and
Kristine all sign in with their own password and have the same access (add,
edit, cancel). Only approved leaves are logged, so every entry lowers the
headcount. Pick an agent and the first and last day off; the screen warns, live,
if the leave would leave fewer than N agents rostered in any hour (N is
adjustable, default 2), and refuses an overlapping leave for the same agent.
Cancelling keeps the record in the change history. The change history lists
every add, edit and cancel with who and when; it is written by database
triggers, so it cannot miss one. Signed-out viewers of the dashboard see only a
sign-in form on this tab, and on the Forecast tab they see counts of agents on
leave but not names.

**Forecast** looks 7, 14 or 28 days ahead. Click *Build forecast* (it loads 28
days of history, a couple of minutes the first time, cached for 30 minutes).
For each future hour:

- Expected tickets = the average (or 75th percentile at the "Busy" level) of
  that weekday-hour over the last 28 days, times an optional volume adjustment.
- Available agents = the roster for that hour minus rostered agents on approved
  leave that date. Optionally scaled by typical attendance from Slack.
- Needed agents = expected tickets / capacity per agent per hour (the same
  estimate the rest of the app uses), rounded up. Gap = needed minus available.
- The tab shows a day-by-day table, a date-by-hour heatmap of gaps, a one-day
  chart, and a count of hours that are short *only because of leave* (the same
  hour would have been fine on the roster alone).

It is a planning aid. It does not model growth, holidays or incidents unless you
set the volume adjustment, and the capacity estimate carries the same caveats as
the rest of the app.

**How leave is applied.** Dates are calendar dates in US Central time, matching
the roster. An agent on leave for a date is removed from every hour they are
rostered that date. A shift that runs past midnight appears in the roster as
hours on two calendar dates (for example Sunday 6 PM to Monday 1 AM), so log
both dates for an overnight shift.

### One-time setup

1. **Supabase.** In your Supabase project open *SQL Editor -> New query*, paste
   `supabase/schema.sql`, and run it (safe to run again). Then *Project
   Settings -> API*: copy the project URL and the secret / `service_role` key.
   The key is used only on the server. Never commit it, and don't use the
   browser-safe "anon" key (the tables are locked so it can't read them).
2. **Streamlit secrets** (app -> Settings -> Secrets). Add, keeping the
   `[LEAVE_USERS]` table at the very bottom:
   ```toml
   SUPABASE_URL = "https://your-project-ref.supabase.co"
   SUPABASE_SERVICE_KEY = "your-secret-key"
   NOTION_TOKEN = "your-notion-integration-token"
   NOTION_LEAVE_DB_ID = "id-of-the-notion-backup-database"

   [LEAVE_USERS]
   erwin = "a-password"
   weng = "a-password"
   kristine = "a-password"
   ```
3. **Notion backup.** Create a Notion integration at notion.so/profile/integrations
   (internal, with read, update and insert content), copy its secret into
   `NOTION_TOKEN`, then open the backup database -> the ... menu ->
   *Connections* and add that integration. `NOTION_LEAVE_DB_ID` is the 32
   character ID in the database's URL (the part before `?v=`).
4. **Nightly backup (optional but recommended).** In the GitHub repo go to
   *Settings -> Secrets and variables -> Actions* and add the same four values
   as repository secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
   `NOTION_TOKEN`, `NOTION_LEAVE_DB_ID`. The workflow in
   `.github/workflows/notion_backup.yml` then re-copies everything every night
   and can be run by hand from the Actions tab. GitHub pauses scheduled
   workflows on a repo with no activity for 60 days; running it by hand once
   restarts the clock.

Supabase is the source of truth. Each save is copied to Notion right away
(best effort: if Notion is down the leave is still saved and the nightly job
catches up), and Notion is one-way: editing a row there does not change the
forecast.

Everything leave-related is optional. Without the Supabase secrets the app runs
exactly as before and the Leave log tab explains what to set up.

### Note on the `streamlit_app/` folder

The repo root is the live app (Streamlit's main file is `app.py`). The
`streamlit_app/` folder is an older copy from the first build and is not used;
it can be deleted.

## Performance note on the Trends tab

This Intercom workspace has a large conversation history. Pulling 30-90 days
of full conversation data can mean thousands of API calls and take a couple
of minutes on first load (results are cached for 30 minutes after that,
per date range). Start with "Last 7 days" if you just want a quick check.
If Slack attendance is configured, that window also pulls the matching
history from `#secret-cc-cafe`, which adds its own (smaller) round of API
calls on first load - also cached for 30 minutes.

Besides the "Last 7/30/90 days" presets, the "Window" control also has a
**Custom range** option: pick any start and end date (inclusive), including
the same date twice for a single specific day. It's built from the same
`fetch_hourly_table`/Slack calls as the presets, so the same caching and
"wider range = slower first load" notes above apply - a multi-month custom
range will be slow on first load just like a 90-day preset would be.

## Known limitations / things worth revisiting

- "Scheduled agents" is a planned roster, not proof anyone was actually
  online - it can't account for call-outs, no-shows, or overtime. Once Slack
  is configured, that's exactly why staffing decisions are based on actual
  online agents rather than the roster - see "Which one drives staffing
  decisions" above.
- The capacity estimate is a statistical heuristic over whatever window is
  currently loaded; it will shift as more data comes in, and can look strange
  with very little history (that's what the "(default, low data)" label
  means). It's based on `effective_agents` (actual online where Slack has it,
  roster otherwise), so a day with little or no Slack history for a stretch
  of hours falls back to the roster for that stretch, same as before Slack
  was added.
- Slack's history for `#secret-cc-cafe` only goes back as far as the channel
  itself (and further back than that, the bot wasn't invited yet). An hour
  from before the channel was in regular use will have no Slack messages at
  all, which reads as "no Slack data for that hour" and falls back to the
  roster - not as "zero agents online". If a specific hour looks wrong,
  check whether the channel actually had traffic then.
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
- "Worked on" and "Closed" (Daily tab) only count activity on tickets created
  that same day - a ticket created yesterday and closed today won't be
  reflected in either column today (or, for that matter, in yesterday's
  table once that day's window has passed). If a day looks like it has
  "leftover" tickets that never show as worked on/closed, this is why - see
  "Created / Worked on / Closed" above.
- Two posters (Weng and Kristine) no longer take tickets, so their
  login/break/back/logout pings in `#secret-cc-cafe` are deliberately
  excluded from "actual online" - see `EXCLUDED_EMAILS` in `attendance.py`.
  They're excluded rather than treated as "unmatched" since there's no
  naming mismatch to flag. If anyone else stops taking tickets but keeps
  posting in the channel, add them there the same way.
