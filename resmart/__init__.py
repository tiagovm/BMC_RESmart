"""BMC RESmart GII CPAP data analysis, as a package.

Top-level API for the three analysis layers plus the one-shot pipeline:

    from resmart import workflow
    result = workflow.run_full_pipeline("out.csv")

Reverse-engineered, unofficial, not for medical use. See ``README.md`` for the
packet-format spec and ``DESIGN.md`` for architecture and design decisions.
"""

from resmart.analysis import segment_sessions
from resmart.events import (
    AhiEstimate,
    Event,
    MaskRemoval,
    detect_flow_limitation,
    detect_mask_removal,
    estimate_ahi,
    event_timeline,
    events_report,
)
from resmart.io import (
    all_session_ids,
    load_quality_or_error,
    qc_report_path,
    read_segmented_csv,
)
from resmart.preprocess import clean_and_preprocess
from resmart.quality import (
    NightSegment,
    QualityReport,
    SessionData,
    SuspiciousInterval,
    detect_signal_quality,
    load_session,
    plot_signal_quality,
    read_quality_report,
    reconcile_units,
    resample_signal,
    segment_night,
    write_quality_report,
)
from resmart.stats import (
    DistributionStats,
    SessionStats,
    nightly_trend_summary,
    plot_volume_distribution,
    respiratory_rate,
    session_summary,
    valid_mask,
    volume_distribution,
)
from resmart.workflow import PipelineResult, SessionResult, run_full_pipeline

__version__ = "0.1.0"

__all__ = [
    "AhiEstimate",
    "DistributionStats",
    "Event",
    "MaskRemoval",
    "NightSegment",
    "PipelineResult",
    "QualityReport",
    "SessionData",
    "SessionResult",
    "SessionStats",
    "SuspiciousInterval",
    "all_session_ids",
    "clean_and_preprocess",
    "detect_flow_limitation",
    "detect_mask_removal",
    "detect_signal_quality",
    "estimate_ahi",
    "event_timeline",
    "events_report",
    "load_quality_or_error",
    "load_session",
    "nightly_trend_summary",
    "plot_signal_quality",
    "plot_volume_distribution",
    "qc_report_path",
    "read_quality_report",
    "read_segmented_csv",
    "reconcile_units",
    "resample_signal",
    "respiratory_rate",
    "run_full_pipeline",
    "segment_night",
    "segment_sessions",
    "session_summary",
    "valid_mask",
    "volume_distribution",
    "write_quality_report",
]