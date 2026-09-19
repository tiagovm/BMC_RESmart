"""Shared defaults and constants for the resmart package.

Single source of truth for values that were previously repeated across the
standalone scripts: the signal channels a session can carry, the default
report directory, the bed window used for night segments, and the session
segmentation gap. Kept free of third-party imports so any module (and a
future web layer) can import it cheaply.
"""

# Signal channels known to the device (raw words, scaling unconfirmed).
CHANNELS = ("resA", "resB", "resC", "pulse")
DEFAULT_CHANNEL = "resA"

# Nightly window used by Layer-1 night segmentation and the summary layers.
NIGHT_START = "22:00"
NIGHT_END = "07:00"

# Default directory for derived per-patient artifacts (gitignored).
REPORT_DIR = "reports"

# Items per session that get written when the CLI omits an explicit path:
#   reports/qc_session_<id>_<date>.json        (quality)
#   reports/nightly_trend.csv                  (stats trend)
#   reports/stats_session_<id>_<date>.png      (stats --plot)
#   reports/events_session_<id>_<date>.csv     (events timeline)
#   reports/events_session_<id>_<date>.png     (events --plot)
#   reports/events_summary.csv                 (events-report)

# Segmentation gap: rows further apart than this (hours) start a new session.
DEFAULT_LIMIT_HOURS = 4