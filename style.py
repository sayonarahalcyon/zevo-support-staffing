"""Shared color palette (validated for colorblind-safety) used across all charts."""

SERIES_TICKETS = "#2a78d6"       # categorical slot 1 (blue) - tickets created
SERIES_AGENTS = "#1baf7a"        # categorical slot 3 (aqua) - scheduled agents
SERIES_ACTUAL = "#eb6834"        # categorical slot 2 (orange) - actual online (from Slack)
SERIES_WORKED_ON = "#eda100"     # categorical slot 4 (yellow) - tickets worked on

SEQUENTIAL_BLUE = [
    "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b",
]

STATUS_GOOD = "#0ca30c"       # sufficient staffing
STATUS_WARNING = "#fab219"    # likely overstaffed
STATUS_CRITICAL = "#d03b3b"   # understaffed / SLA at risk

TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#898781"
GRIDLINE = "#e1e0d9"

RECOMMENDATION_COLORS = {
    "Increase": STATUS_CRITICAL,
    "Sufficient": STATUS_GOOD,
    "Decrease": STATUS_WARNING,
}
