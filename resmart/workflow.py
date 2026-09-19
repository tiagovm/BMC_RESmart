"""Programmatic one-shot pipeline over the three analysis layers.

Drives the same code paths as the individual CLIs but as library calls, so a
web interface can run a whole night in a single ``run_full_pipeline`` call
(framework-agnostic: no printing, no argparse, no matplotlib at import time).
:class:`PipelineResult` carries every structured result and the artifact paths
written under the report directory.

Calling ``run_full_pipeline`` mirrors what ``python -m resmart pipeline``
wraps: for each detected session it runs Layer 1 (quality control, reporting
the ``quality`` JSON), then Layer 2 (summary statistics, trend CSV) and
Layer 3 (events, timelines and summary CSV) over the same QC-valid signal.
"""

import dataclasses
import os
from typing import Dict, List, Optional

import pandas as pd

from resmart.config import DEFAULT_CHANNEL, REPORT_DIR
from resmart.events import (
    APNEA_DROP_PCT,
    AhiEstimate,
    DROP_PCT,
    MASK_OFF_MINUTES,
    MIN_DURATION_S,
    Event,
    MaskRemoval,
    detect_flow_limitation,
    detect_mask_removal,
    estimate_ahi,
    event_timeline,
    events_report,
)
from resmart.io import qc_report_path
from resmart.quality import (
    QualityReport,
    SessionData,
    detect_signal_quality,
    load_session,
    reconcile_units,
    resample_signal,
    write_quality_report,
)
from resmart.stats import (
    DistributionStats,
    SessionStats,
    nightly_trend_summary,
    respiratory_rate,
    session_summary,
    volume_distribution,
)


@dataclasses.dataclass
class SessionResult:
    """Everything the three layers produced for one session night."""

    session: SessionData
    quality: QualityReport
    stats: SessionStats
    volume_distribution: DistributionStats
    respiratory_rate: Optional[pd.DataFrame]
    events: List[Event]
    mask_removals: List[MaskRemoval]
    ahi: AhiEstimate
    timeline: pd.DataFrame
    artifact_paths: Dict[str, str]


@dataclasses.dataclass
class PipelineResult:
    """Outcome of a full pipeline run, ready to hand to a UI layer."""

    sessions: List[SessionData]
    by_session: Dict[int, SessionResult]
    nightly_trend: Optional[pd.DataFrame]
    event_summary: Optional[pd.DataFrame]
    artifacts: List[str]

    @property
    def session_ids(self):
        return [s.session_id for s in self.sessions]


def run_full_pipeline(input_path, channel=DEFAULT_CHANNEL,
                      report_dir=REPORT_DIR, limit_hours=4.0,
                      block_minutes=5.0, drop_pct=DROP_PCT,
                      min_duration_s=MIN_DURATION_S,
                      apnea_drop_pct=APNEA_DROP_PCT,
                      mask_off_minutes=MASK_OFF_MINUTES,
                      min_mask_off_minutes=MASK_OFF_MINUTES,
                      run_quality=True, run_stats=True, run_events=True):
    """Run the full analysis for every session of one CSV export.

    ``run_full_pipeline`` discovers the sessions the same way the CLIs do,
    then runs the requested layers in dependency order (quality -> stats ->
    events). The default artifacts of each layer are written under
    ``report_dir`` exactly as the individual commands would write them, and
    every structured result is returned (no printing).
    """
    ids = _discover_session_ids(input_path)
    sessions = [
        load_session(input_path, sid, channel=channel,
                     limit_hours=limit_hours)
        for sid in ids
    ]

    os.makedirs(report_dir, exist_ok=True)
    artifacts = []
    by_session: Dict[int, SessionResult] = {}
    trend = None
    event_summary = None

    for session in sessions:
        quality = None
        stats = None
        dist = None
        rr = None
        events = []
        removals = []
        ahi = None
        timeline = None
        paths: Dict[str, str] = {}

        if run_quality:
            findings = []
            frame = pd.DataFrame({
                "timestamp": session.timestamp,
                session.channel: session.values,
            })
            reconcile_units(frame, findings)
            resampled = resample_signal(
                frame, target_hz=None, method="median",
                column=session.channel, findings=findings)
            quality = detect_signal_quality(
                resampled, findings=findings,
                session_id=session.session_id, channel=session.channel)
            qp = qc_report_path(report_dir, session.session_id, session.start)
            write_quality_report(quality, qp)
            artifacts.append(qp)
            paths["quality"] = qp

        if run_stats and quality is not None:
            rr = respiratory_rate(session, quality, block_minutes=block_minutes)
            stats = session_summary(session, quality,
                                    block_minutes=block_minutes, rr=rr)
            dist = volume_distribution(
                session, quality, warnings=list(stats.warnings))

        if run_events and quality is not None:
            reports = {session.session_id: quality}
            removals, thr, _ = detect_mask_removal(
                session, quality, threshold=None,
                min_minutes=mask_off_minutes)
            events = detect_flow_limitation(
                session, quality, drop_pct=drop_pct,
                min_duration_s=min_duration_s, apnea_drop_pct=apnea_drop_pct,
                exclude_intervals=[(r.start, r.end) for r in removals])
            ahi = estimate_ahi(session, events, quality)
            timeline = event_timeline(session, events)
            ep = os.path.join(
                report_dir,
                "events_session_{0}_{1:%Y-%m-%d}.csv".format(
                    session.session_id, session.start))
            timeline.to_csv(ep, index=False)
            artifacts.append(ep)
            paths["events"] = ep

        by_session[session.session_id] = SessionResult(
            session=session, quality=quality, stats=stats,
            volume_distribution=dist, respiratory_rate=rr,
            events=events, mask_removals=removals, ahi=ahi, timeline=timeline,
            artifact_paths=paths)

    if run_stats:
        sessions_with_quality = [
            s for s in sessions if by_session[s.session_id].quality is not None
        ]
        reports = {
            s.session_id: by_session[s.session_id].quality
            for s in sessions_with_quality
        }
        if reports:
            trend = nightly_trend_summary(sessions_with_quality, reports)
            tp = os.path.join(report_dir, "nightly_trend.csv")
            trend.to_csv(tp, index=False)
            artifacts.append(tp)

    if run_events:
        sessions_with_quality = [
            s for s in sessions if by_session[s.session_id].quality is not None
        ]
        reports = {
            s.session_id: by_session[s.session_id].quality
            for s in sessions_with_quality
        }
        if reports:
            event_summary = events_report(
                sessions_with_quality, reports, drop_pct=drop_pct,
                min_duration_s=min_duration_s, apnea_drop_pct=apnea_drop_pct,
                min_mask_off_minutes=min_mask_off_minutes)
            sp = os.path.join(report_dir, "events_summary.csv")
            event_summary.to_csv(sp, index=False)
            artifacts.append(sp)

    return PipelineResult(
        sessions=sessions, by_session=by_session, nightly_trend=trend,
        event_summary=event_summary, artifacts=artifacts)


def _discover_session_ids(input_path: str) -> List[int]:
    from resmart.io import all_session_ids
    return all_session_ids(input_path)