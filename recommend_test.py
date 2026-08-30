"""Offline unit test for recommend.py's headcount precedence: actual online
(from Slack) overrides the scheduled roster for any hour it's available for,
falling back to the roster only when there's no Slack data for that hour -
confirmed with Weng: the roster sheet isn't updated daily and misses leave,
no-shows, and someone covering an off day."""
import pandas as pd

from recommend import add_recommendations

# The capacity model needs a run of "good" hours to estimate a rate from, so
# build a comfortable baseline (8 tickets, 4 effective agents, SLA met) before
# the two hours actually under test.
rows = []
for h in range(20):
    rows.append({"hour": h, "tickets": 8, "scheduled_agents": 4, "actual_online": 4.0,
                 "human_handled": 8, "sla_met": 8, "sla_hit_rate": 1.0})

# Hour A: roster says fully staffed (5), but Slack shows nobody was actually
# online - this must read as understaffed once actual overrides the roster.
rows.append({"hour": 20, "tickets": 8, "scheduled_agents": 5, "actual_online": 0.0,
             "human_handled": 2, "sla_met": 0, "sla_hit_rate": 0.0})
# Hour B: no Slack data at all for this hour (bot not configured, or no
# messages posted) - must fall back to the roster, exactly like before Slack
# attendance existed.
rows.append({"hour": 21, "tickets": 8, "scheduled_agents": 4, "actual_online": pd.NA,
             "human_handled": 8, "sla_met": 8, "sla_hit_rate": 1.0})

df = pd.DataFrame(rows)
df["effective_agents"] = df["actual_online"].where(df["actual_online"].notna(), df["scheduled_agents"])

out, meta = add_recommendations(df, sla_target=0.9)
print(out[["hour", "tickets", "scheduled_agents", "actual_online", "effective_agents",
           "required_agents", "recommendation"]].tail(5).to_string())
print("meta:", meta)

hour_a = out[out["hour"] == 20].iloc[0]
assert hour_a["effective_agents"] == 0.0, hour_a["effective_agents"]
assert hour_a["recommendation"] == "Increase", hour_a["recommendation"]  # roster alone would've said "Sufficient" (5 scheduled)

hour_b = out[out["hour"] == 21].iloc[0]
assert hour_b["effective_agents"] == 4, hour_b["effective_agents"]  # fell back to the roster
assert hour_b["recommendation"] == "Sufficient", hour_b["recommendation"]

# No Slack column at all (e.g. a caller like smoke_test.py with no Slack data
# merged in) - must behave exactly as it did before Slack attendance existed.
df_no_slack = df.drop(columns=["actual_online", "effective_agents"])
out2, _ = add_recommendations(df_no_slack, sla_target=0.9)
assert (out2["effective_agents"] == out2["scheduled_agents"]).all()

print("\nOK - recommend precedence test passed")
