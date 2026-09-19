"""Layer 3 of the CPAP analysis roadmap: respiratory event detection and
the per-night timeline.

Consumes Layer 1 (a :class:`SessionData` from ``quality.load_session`` and the
persisted JSON :class:`QualityReport`) exactly like Layer 2 does — the signal
is never reloaded and QC is never re-run. For one session it detects
sustained reductions of the flow amplitude (probable apnea/hypopnea), the
long zero-activity stretches that are mask removal rather than physiology,
and aggregates both into a per-night timeline, an estimated_AHI per
QC-valid hour of use, and a one-row-per-night summary.

    SessionData + QualityReport
        -> detect_mask_removal (long near-zero-activity stretches)
        -> detect_flow_limitation (sustained amplitude drops) -> list[Event]
        -> estimate_ahi (events per QC-valid usage hour)
        -> event_timeline (DataFrame + hour_of_night) -> reports/events_session_*.csv
        -> events_report (one row per night) -> reports/events_summary.csv

Every event carries a ``quality_flag``: events that overlap a QC-suspect
interval (flatline/clipping/spike/drift) or sit on mostly-invalid samples are
flagged ``"suspect"`` and are never presented as physiology — the confident
``estimated_ahi`` counts only ``"ok"`` events. All values are in raw units
(``resA`` scaling unconfirmed). ``estimated_ahi`` is a flow-derived estimate,
**not** a clinical AHI: there is no oximetry and no thoracic effort signal.

Detector limits (honest): the amplitude envelope is a per-second statistic,
so events shorter than the ``min_duration_s`` floor (default 10 s, the
simplified AASM hypopnea convention) are not reported even though the 25 Hz
grid itself resolves them; and the single flow channel cannot separate central
from obstructive causes.

CLI:
    python events.py events <session_id> --input <csv> [--json out.json]
                          [--csv events.csv] [--plot] [-o events.png] [--show]
                          [--channel resA] [--report-path qc.json]
                          [--report-dir reports]
                          [--drop-pct 0.30] [--min-duration-s 10]
                          [--apnea-drop-pct 0.80] [--mask-off-minutes 2]
    python events.py events-report --input <csv> [--all]
                          [--output reports/events_summary.csv] [--report-dir reports]
Not for medical use.
"""

import argparse
import dataclasses
import json
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

from quality import (
    QualityReport,
    SessionData,
    load_session,
    read_quality_report,
    resample_signal,
)
from stats import _all_session_ids, valid_mask

# Defaults (simplified AASM, documented in README/DESIGN):
DROP_PCT = 0.30            # sustained amplitude reduction that starts a hypopnea
APNEA_DROP_PCT = 0.80      # sustained amplitude reduction that starts an apnea
MIN_DURATION_S = 10.0      # minimum sustained reduction (s) to count an event
MASK_OFF_MINUTES = 2.0     # near-zero activity this long = mask removal
SMOOTH_WIN_S = 5.0         # median smoothing of the per-second amplitude envelope
BASELINE_WIN_S = 300.0     # local baseline: rolling median of the envelope (5 min)
MIN_BASELINE_S = 90.0      # minimum envelope history for the baseline window
MIN_SAMPLES_PER_S = 10     # seconds with fewer samples are treated as unknown
MIN_EVENT_VALID_FRAC = 0.9  # below this, an event sits mostly on invalid samples
MAX_PLOT_POINTS = 200_000  # waveform decimation target for the annotated plot

INTERVAL_TYPES = ("flatline", "clipping", "spike", "drift")


@dataclasses.dataclass
class Event:
    """One detected respiratory event of a night, with full timestamps.

    ``start``/``end`` carry the full date+time (sessions cross midnight).
    ``reduction`` is 1 - (median envelope during the event / local baseline),
    so 0.30 means the flow amplitude lost 30 % vs. the surrounding night.
    ``event_type`` is ``"apnea"``/``"hypopnea"`` (provisional, flow-derived).
    ``quality_flag`` is ``"ok"`` or ``"suspect"`` (overlaps a QC interval or
    sits mostly on invalid samples); ``confidence`` ``"high"``/``"low"``.
    """

    session_id: int
    channel: str
    start: pd.Timestamp
    end: pd.Timestamp
    duration_s: float
    reduction: float
    event_type: str
    quality_flag: str
    confidence: str

    @property
    def date(self):
        return self.start.date().isoformat()

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "channel": self.channel,
            "date": self.date,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_s": round(self.duration_s, 3),
            "reduction": round(self.reduction, 4),
            "event_type": self.event_type,
            "quality_flag": self.quality_flag,
            "confidence": self.confidence,
        }


@dataclasses.dataclass
class MaskRemoval:
    """A sustained near-zero-flow stretch: the mask was off, not an apnea."""

    session_id: int
    channel: str
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def duration_s(self):
        return (self.end - self.start).total_seconds()

    def to_dict(self):
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_s": round(self.duration_s, 3),
        }


@dataclasses.dataclass
class AhiEstimate:
    """Events per QC-valid usage hour (``estimated_ahi``, never bare AHI)."""

    session_id: int
    usage_hours: float
    n_events: int
    n_events_confident: int
    estimated_ahi: Optional[float]
    estimated_ahi_confident: Optional[float]

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "usage_hours": round(self.usage_hours, 4),
            "n_events": self.n_events,
            "n_events_confident": self.n_events_confident,
            "estimated_ahi": (round(self.estimated_ahi, 4)
                              if self.estimated_ahi is not None else None),
            "estimated_ahi_confident": (
                round(self.estimated_ahi_confident, 4)
                if self.estimated_ahi_confident is not None else None),
        }


def _period_s(session: SessionData) -> float:
    period = getattr(session, "measured_period_s", 0.0) or 0.0
    if period > 0:
        return period
    rate = getattr(session, "sample_rate_hz", 0.0) or 0.0
    return 1.0 / rate if rate > 0 else 0.04


def _analysis_frame(session: SessionData):
    """The session signal on the same regular grid the QC pass used."""
    frame = pd.DataFrame({
        "timestamp": session.timestamp,
        session.channel: session.values,
    })
    return resample_signal(
        frame, method="median", column=session.channel)


def _per_second_activity(frame, column: str) -> pd.Series:
    """One amplitude statistic per wall-clock second of the session.

    The per-second standard deviation of the raw signal measures breathing
    activity in raw units (a quiet breath ~ A/sqrt(2)). Seconds with fewer
    than MIN_SAMPLES_PER_S valid-scalar samples are left NaN (grid edges,
    resample holes). The index is the floored second (full timestamps).
    """
    vals = frame[column].to_numpy(dtype=float)
    secs = frame["timestamp"].dt.floor("s")
    per_sec = pd.Series(vals, index=secs).groupby(level=0).agg(
        lambda x: float(np.std(x)) if len(x) >= MIN_SAMPLES_PER_S else np.nan)
    return per_sec


def _consecutive_runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    runs = []
    start = None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def detect_mask_removal(session: SessionData, quality: QualityReport,
                        threshold: Optional[float] = None,
                        min_minutes: float = MASK_OFF_MINUTES) \
        -> Tuple[List[MaskRemoval], float, float]:
    """Long near-zero-activity stretches = mask removed, not physiology.

    Uses the amplitude envelope of the *raw* signal (mask removal happens
    while the sensor reads ~0, a stretch QC usually tabs as flatline/dead).
    Runs of seconds whose per-second activity stays below ``threshold`` for at
    least ``min_minutes`` are mask removal. When ``threshold`` is None it is
    derived from the data and returned with the run count; the derivation and
    the criterion used are surfaced so the report states exactly what was
    decided:

      threshold = 20 % of the night's median per-second activity

    (the envelope is bimodal between breathing-amplitude seconds and ~0 dead
    seconds, so a fraction of its median separates the two modes regardless
    of the raw-unit scale).

    ``min_minutes`` separates mask removal (minutes, excluded from event
    detection) from respiratory pauses (seconds, kept as apnea candidates).
    Returns (mask removals, threshold used, total mask-off hours).
    """
    frame = _analysis_frame(session)
    per_sec = _per_second_activity(frame, session.channel)
    env = per_sec.dropna()
    if threshold is None:
        # 20 % of the night's median activity: the per-second envelope is
        # bimodal (breathing ~ seconds-of-normal-amplitude vs. ~0 during
        # mask removal / dead stretches — the same ~0 peak seen in the
        # volume distribution), so a fraction of its median sits cleanly
        # between the two modes regardless of the raw-unit scale.
        med = float(np.median(env)) if len(env) else 0.0
        threshold = 0.20 * med
    below = per_sec < threshold
    below = below.fillna(False)
    n_run = max(1, int(round(min_minutes * 60.0)))
    idx = per_sec.index
    removals = []
    for i0, i1 in _consecutive_runs(below.to_numpy()):
        if (i1 - i0 + 1) >= n_run:
            removals.append(MaskRemoval(
                session_id=session.session_id, channel=session.channel,
                start=idx[i0],
                end=idx[i1] + pd.Timedelta(seconds=1)))
    total_h = sum(r.duration_s for r in removals) / 3600.0
    return removals, threshold, total_h


def _overlaps_qc(start, end, intervals) -> bool:
    for iv in intervals:
        if iv.kind in INTERVAL_TYPES and iv.start <= end and start <= iv.end:
            return True
    return False


def detect_flow_limitation(session: SessionData, quality: QualityReport,
                           drop_pct: float = DROP_PCT,
                           min_duration_s: float = MIN_DURATION_S,
                           apnea_drop_pct: float = APNEA_DROP_PCT,
                           exclude_intervals: Optional[List[Tuple]] = None) \
        -> List[Event]:
    """Sustained reductions of the flow amplitude (probable events).

    The per-second amplitude envelope is compared with a *local* baseline
    (rolling median over BASELINE_WIN_S, ~5 min) so variations across the
    night do not distort the reference. A run of seconds whose envelope stays
    below ``(1 - drop_pct) x baseline`` for at least ``min_duration_s`` is one
    event; its reduction is 1 - (median envelope / median baseline) over the
    run.

    Classification (simplified AASM reference): ``reduction >= apnea_drop_pct``
    -> ``"apnea"``; ``drop_pct..apnea_drop_pct`` -> ``"hypopnea"``. Events
    overlapping a QC-suspect interval or with < MIN_EVENT_VALID_FRAC valid
    samples carry ``quality_flag="suspect"`` (never presented as physiology);
    ``confidence`` is ``"high"`` only for fully-valid, QC-clean events.
    ``exclude_intervals`` (mask removal) suppresses events inside them.
    """
    frame = _analysis_frame(session)
    col = session.channel
    ts = frame["timestamp"]
    values = frame[col].to_numpy(dtype=float)
    mask = valid_mask(ts, quality) & np.isfinite(values)

    per_sec = _per_second_activity(frame, col)
    env = per_sec.rolling(int(round(SMOOTH_WIN_S)), center=True,
                          min_periods=max(1, int(SMOOTH_WIN_S // 2))
                          ).median()
    baseline = per_sec.rolling(int(round(BASELINE_WIN_S)), center=True,
                               min_periods=int(MIN_BASELINE_S)).median()

    idx = per_sec.index
    if exclude_intervals:
        excluded = np.zeros(len(per_sec), dtype=bool)
        for s, e in exclude_intervals:
            excluded |= (idx >= np.datetime64(s)) & (idx <= np.datetime64(e))
    else:
        excluded = np.zeros(len(per_sec), dtype=bool)

    thr = baseline * (1.0 - drop_pct)
    below = env < thr
    below = below.fillna(False) & ~excluded
    n_min = max(1, int(round(min_duration_s)))

    events = []
    for i0, i1 in _consecutive_runs(below.to_numpy()):
        dur = i1 - i0 + 1
        if dur < n_min:
            continue
        start_ts = idx[i0]
        end_ts = idx[i1] + pd.Timedelta(seconds=1)
        bl = baseline.iloc[i0:i1 + 1]
        en = env.iloc[i0:i1 + 1]
        if not np.isfinite(bl.median()) or bl.median() <= 0:
            continue
        reduction = float(1.0 - en.median() / bl.median())
        if reduction < drop_pct:
            continue

        lo = np.searchsorted(ts.to_numpy(), np.datetime64(start_ts))
        hi = np.searchsorted(ts.to_numpy(), np.datetime64(end_ts), side="right")
        sl = slice(lo, hi)
        valid_frac = 1.0
        if hi > lo:
            valid_frac = float(mask[sl].sum()) / float(hi - lo)

        suspect = _overlaps_qc(start_ts, end_ts, quality.intervals) \
            or valid_frac < MIN_EVENT_VALID_FRAC
        events.append(Event(
            session_id=session.session_id, channel=session.channel,
            start=start_ts, end=end_ts, duration_s=float(dur),
            reduction=reduction,
            event_type=("apnea" if reduction >= apnea_drop_pct else "hypopnea"),
            quality_flag=("suspect" if suspect else "ok"),
            confidence=("high" if (not suspect and valid_frac >= 1.0)
                        else "low")))
    return events


def estimate_ahi(session: SessionData, events: List[Event],
                 quality: QualityReport) -> AhiEstimate:
    """Events per QC-valid usage hour (the ``estimated_AHI`` of the night).

    The denominator is the *effective* use: valid_samples x measured period
    (the exact figure Layer 2 calls ``night_usage``), not wall-clock hours of
    the file. ``estimated_ahi`` counts all events, ``estimated_ahi_confident``
    excludes ``quality_flag="suspect"`` ones — a confidence range for the
    same hour base. Both are None when no valid use exists.
    """
    frame = _analysis_frame(session)
    ts = frame["timestamp"]
    values = frame[session.channel].to_numpy(dtype=float)
    mask = valid_mask(ts, quality) & np.isfinite(values)
    usage_h = mask.sum() * _period_s(session) / 3600.0
    n_conf = sum(1 for e in events if e.quality_flag == "ok")
    n_total = len(events)
    if usage_h <= 0:
        return AhiEstimate(session.session_id, 0.0, n_total, n_conf, None, None)
    return AhiEstimate(
        session_id=session.session_id, usage_hours=usage_h,
        n_events=n_total, n_events_confident=n_conf,
        estimated_ahi=n_total / usage_h,
        estimated_ahi_confident=n_conf / usage_h)


def event_timeline(session: SessionData, events: List[Event]) -> pd.DataFrame:
    """One row per event, sorted chronologically, with derived hour band.

    ``hour_of_night`` is the wall-clock hour (0-23) of the event start, i.e.
    the clock band (23 h, 00 h, 01 h) used to answer "at which hour of the
    night do events concentrate?" — sessions cross midnight, so the date in
    ``start``/``end`` is what keeps the timeline correct.
    """
    rows = [{
        "start": e.start, "end": e.end,
        "duration_s": round(e.duration_s, 3),
        "reduction": round(e.reduction, 4),
        "event_type": e.event_type,
        "quality_flag": e.quality_flag,
        "confidence": e.confidence,
        "hour_of_night": int(e.start.hour),
    } for e in events]
    df = pd.DataFrame(rows, columns=[
        "start", "end", "duration_s", "reduction", "event_type",
        "quality_flag", "confidence", "hour_of_night"])
    if not df.empty:
        df = df.sort_values("start").reset_index(drop=True)
    return df


def events_by_hour(timeline: pd.DataFrame) -> Dict[str, int]:
    """Counts of events per wall-clock hour of the night (string "HH")."""
    if timeline.empty:
        return {}
    counts = timeline.groupby(timeline["hour_of_night"].apply(
        lambda h: "{:02d}".format(h)))["start"].count()
    return {str(k): int(v) for k, v in counts.items()}


def events_report(sessions: Iterable[SessionData],
                  reports: Mapping[int, QualityReport],
                  drop_pct: float = DROP_PCT,
                  min_duration_s: float = MIN_DURATION_S,
                  apnea_drop_pct: float = APNEA_DROP_PCT,
                  min_mask_off_minutes: float = MASK_OFF_MINUTES) \
        -> pd.DataFrame:
    """Consolidated view: one row per session night.

    Combines event count, ``estimated_ahi`` (+ confident variant), suspect
    share, the per-hour breakdown and the mask-removal stretches. Same
    handling as :func:`nightly_trend_summary`: a session without a report
    raises immediately naming what to run. Persisted by the CLI as
    ``reports/events_summary.csv``.
    """
    rows = []
    for session in sessions:
        if session.session_id not in reports:
            raise ValueError(
                "no QualityReport for session {}".format(session.session_id)
                + ": run quality.py preprocess first")
        quality = reports[session.session_id]
        removals, thr, _ = detect_mask_removal(session, quality)
        events = detect_flow_limitation(
            session, quality, drop_pct=drop_pct,
            min_duration_s=min_duration_s, apnea_drop_pct=apnea_drop_pct,
            exclude_intervals=[(r.start, r.end) for r in removals])
        ahi = estimate_ahi(session, events, quality)
        timeline = event_timeline(session, events)
        n_suspect = sum(1 for e in events if e.quality_flag == "suspect")
        rows.append({
            "session_id": session.session_id,
            "date": session.start.date().isoformat(),
            "start": session.start.isoformat(),
            "end": session.end.isoformat(),
            "usage_hours": round(ahi.usage_hours, 4),
            "n_events": ahi.n_events,
            "n_suspect": n_suspect,
            "suspect_pct": round(100.0 * n_suspect / ahi.n_events, 1)
            if ahi.n_events else 0.0,
            "estimated_ahi": ahi.estimated_ahi,
            "estimated_ahi_confident": ahi.estimated_ahi_confident,
            "events_by_hour": json.dumps(events_by_hour(timeline),
                                         sort_keys=True),
            "n_mask_removals": len(removals),
            "mask_removal_hours": round(sum(r.duration_s for r in removals)
                                        / 3600.0, 4),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("session_id").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# Plotting
# --------------------------------------------------------------------------

_PLT = None


def _pyplot():
    global _PLT
    if _PLT is None:
        from matplotlib import pyplot
        _PLT = pyplot
    return _PLT


def plot_night_events(session: SessionData, events: List[Event],
                      mask_removals: List[MaskRemoval],
                      quality: QualityReport):
    """The night's waveform with detected events and mask removal shaded.

    Shading: mask removal = gray; confident events = green (hypopnea) / red
    (apnea); suspect events hatch-patterned and translucent — degraded
    stretches are never drawn as clean physiology. Long nights are decimated
    like ``plot_waveform``. Returns the ``(figure, axes)`` pair.
    """
    frame = _analysis_frame(session)
    col = session.channel
    ts = frame["timestamp"]
    values = frame[col].to_numpy(dtype=float)
    n = len(values)
    step = max(1, round(n / MAX_PLOT_POINTS))

    fig, ax = _pyplot().subplots(figsize=(14, 5))
    ax.plot(ts.iloc[::step], values[::step], color="tab:blue", linewidth=0.6)

    for r in mask_removals:
        ax.axvspan(r.start, r.end, color="0.6", alpha=0.5, label="mask off")
    for e in events:
        if e.quality_flag == "suspect":
            color = "0.9"
            kwargs = {"hatch": "//", "alpha": 0.5}
        else:
            color = ("tab:red" if e.event_type == "apnea" else "tab:green")
            kwargs = {"alpha": 0.45}
        ax.axvspan(e.start, e.end, color=color, **kwargs,
                   label="{} ({})".format(e.event_type, e.quality_flag))

    from matplotlib.lines import Line2D
    handles = []
    seen = set()
    for h in ax.get_legend_handles_labels()[0]:
        label = h.get_label() if hasattr(h, "get_label") else str(h)
        key = label
        if key not in seen:
            seen.add(key)
            handles.append(h)
    ax.legend(handles=handles, loc="upper right", fontsize="small")

    ahi = estimate_ahi(session, events, quality)
    ahi_txt = ("estimated_AHI {:.2f} (conf. {:.2f})".format(
        ahi.estimated_ahi, ahi.estimated_ahi_confident)
        if ahi.estimated_ahi is not None else "estimated_AHI n/a")
    title = "Night events - session {} | {} | valid {:.1f}% | {} events".format(
        session.session_id, session.channel, quality.valid_pct, len(events))
    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel("{} (raw units)".format(col))
    ax.text(0.01, 0.99, ahi_txt, transform=ax.transAxes, va="top", fontsize=9,
            color="0.4")
    if step > 1:
        ax.set_title(ax.get_title() + " (downsampled {:d}x)".format(step))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig, ax


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _q_report_path(report_dir: str, session_id: int, start: pd.Timestamp) -> str:
    return os.path.join(
        report_dir, "qc_session_{0}_{1:%Y-%m-%d}.json".format(
            session_id, start))


def _load_quality_or_error(report_path: str) -> QualityReport:
    if not os.path.exists(report_path):
        raise ValueError(
            "no QualityReport at {}: run quality.py preprocess for this "
            "session first".format(report_path))
    return read_quality_report(report_path)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Layer 3: respiratory events and per-night timeline consuming "
            "Layer 1 (SessionData + persisted QualityReport) and Layer 2 "
            "(effective usage). Subcommands: events (one session, full "
            "timeline) and events-report (one row per night)."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("events", help="detect events + timeline of one session")
    p.add_argument("--input", required=True,
                   help="CSV export from resmart_parse.py (with -2 for resA) "
                        "or a segmented CSV (with session_id)")
    p.add_argument("session_id", type=int, help="session id to analyze")
    p.add_argument("--channel", default="resA",
                   choices=["resA", "resB", "resC", "pulse"],
                   help="signal channel to analyze (default: resA)")
    p.add_argument("--report-path", default=None,
                   help="path to the persisted QualityReport JSON (default: "
                        "<report-dir>/qc_session_<id>_<date>.json)")
    p.add_argument("--report-dir", default="reports",
                   help="directory holding the default QualityReport JSONs "
                        "(default: reports/)")
    p.add_argument("--drop-pct", type=float, default=DROP_PCT,
                   help="sustained amplitude drop starting an event "
                        "(default: {})".format(DROP_PCT))
    p.add_argument("--min-duration-s", type=float, default=MIN_DURATION_S,
                   help="minimum sustained length of an event in seconds "
                        "(default: {})".format(MIN_DURATION_S))
    p.add_argument("--apnea-drop-pct", type=float, default=APNEA_DROP_PCT,
                   help="drop starting a probable apnea "
                        "(default: {})".format(APNEA_DROP_PCT))
    p.add_argument("--mask-off-minutes", type=float, default=MASK_OFF_MINUTES,
                   help="near-zero activity this long = mask removal "
                        "(default: {})".format(MASK_OFF_MINUTES))
    p.add_argument("--mask-off-threshold", type=float, default=None,
                   help="absolute per-second activity below which a stretch "
                        "counts as near-zero (default: derived from the data)")
    p.add_argument("--csv", default=None,
                   help="write the per-session event timeline to this CSV "
                        "(default: reports/events_session_<id>_<date>.csv)")
    p.add_argument("--json", metavar="PATH", default=None,
                   help="write the full structured result as JSON")
    p.add_argument("--plot", action="store_true",
                   help="also render the annotated night-events plot")
    p.add_argument("-o", "--output", default=None,
                   help="PNG file to write with --plot (default: "
                        "reports/events_session_<id>_<date>.png)")
    p.add_argument("--show", action="store_true",
                   help="with --plot, display the plot on screen")

    r = sub.add_parser("events-report", help="one summary row per session night")
    r.add_argument("--input", required=True,
                   help="CSV export from resmart_parse.py or a segmented CSV")
    r.add_argument("--all", action="store_true",
                   help="explicitly select every session (default behaviour)")
    r.add_argument("--channel", default="resA",
                   choices=["resA", "resB", "resC", "pulse"],
                   help="signal channel to analyze (default: resA)")
    r.add_argument("--report-dir", default="reports",
                   help="directory holding the default QualityReport JSONs "
                        "(default: reports/)")
    r.add_argument("--output", default=None,
                   help="CSV file to write (default: reports/events_summary.csv)")
    r.add_argument("--drop-pct", type=float, default=DROP_PCT,
                   help="sustained amplitude drop starting an event "
                        "(default: {})".format(DROP_PCT))
    r.add_argument("--min-duration-s", type=float, default=MIN_DURATION_S,
                   help="minimum sustained length of an event in seconds "
                        "(default: {})".format(MIN_DURATION_S))
    r.add_argument("--apnea-drop-pct", type=float, default=APNEA_DROP_PCT,
                   help="drop starting a probable apnea "
                        "(default: {})".format(APNEA_DROP_PCT))
    r.add_argument("--mask-off-minutes", type=float, default=MASK_OFF_MINUTES,
                   help="near-zero activity this long = mask removal "
                        "(default: {})".format(MASK_OFF_MINUTES))
    return parser


def _print_events(session, quality, events, removals, ahi, timeline, params):
    print("session {} | channel {} | {:%Y-%m-%d %H:%M} -> {:%Y-%m-%d %H:%M}".format(
        session.session_id, session.channel, session.start, session.end))
    print("valid {:.1f}% | effective use {:.2f} h | params {}".format(
        quality.valid_pct, ahi.usage_hours, params))
    if removals:
        print("mask removal ({}):".format(len(removals)))
        for r in sorted(removals, key=lambda r: r.start):
            print("  {:%Y-%m-%d %H:%M:%S} -> {:%Y-%m-%d %H:%M:%S} "
                  "({:.1f} min)".format(r.start, r.end, r.duration_s / 60.0))
    else:
        print("mask removal: none")
    if not events:
        print("events: none")
    else:
        print("events ({}): estimated_AHI {:.2f} /h | confident {:.2f} /h "
              "({} suspect)".format(
                  len(events),
                  ahi.estimated_ahi or float("nan"),
                  ahi.estimated_ahi_confident or float("nan"),
                  sum(1 for e in events if e.quality_flag == "suspect")))
        print("  {:<20} {:<20} {:>6} {:>7} {:<9} {:<6}".format(
            "start", "end", "dur", "drop%", "type", "flag"))
        for e in sorted(events, key=lambda e: e.start):
            print("  {:%Y-%m-%d %H:%M:%S} {:%Y-%m-%d %H:%M:%S} {:>6.1f} "
                  "{:>6.0f}% {:<9} {:<6}".format(
                      e.start, e.end, e.duration_s, 100.0 * e.reduction,
                      e.event_type, e.quality_flag))
        bh = events_by_hour(timeline)
        if bh:
            print("per hour: {}".format(
                " | ".join("{}h: {}".format(k, v)
                           for k, v in sorted(bh.items()))))


def _cmd_events(args) -> int:
    session = load_session(args.input, args.session_id, channel=args.channel)
    report_path = args.report_path or _q_report_path(
        args.report_dir, session.session_id, session.start)
    try:
        quality = _load_quality_or_error(report_path)
    except ValueError as exc:
        print(str(exc))
        return 2

    removals, thr, _ = detect_mask_removal(
        session, quality, threshold=args.mask_off_threshold,
        min_minutes=args.mask_off_minutes)
    events = detect_flow_limitation(
        session, quality, drop_pct=args.drop_pct,
        min_duration_s=args.min_duration_s, apnea_drop_pct=args.apnea_drop_pct,
        exclude_intervals=[(r.start, r.end) for r in removals])
    ahi = estimate_ahi(session, events, quality)
    timeline = event_timeline(session, events)
    params = {
        "drop_pct": args.drop_pct, "min_duration_s": args.min_duration_s,
        "apnea_drop_pct": args.apnea_drop_pct,
        "mask_off_minutes": args.mask_off_minutes,
        "mask_removal_threshold": round(thr, 4) if thr > 0 else None,
    }

    print()
    _print_events(session, quality, events, removals, ahi, timeline, params)

    out = args.csv or os.path.join(
        args.report_dir,
        "events_session_{0}_{1:%Y-%m-%d}.csv".format(
            session.session_id, session.start))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    timeline.to_csv(out, index=False)
    print()
    print("wrote {}".format(out))

    if args.json:
        payload = {
            "session": {
                "session_id": session.session_id, "channel": session.channel,
                "start": session.start.isoformat(),
                "end": session.end.isoformat(),
            },
            "params": params,
            "mask_removal_threshold": params["mask_removal_threshold"],
            "mask_removals": [r.to_dict() for r in removals],
            "events": [e.to_dict() for e in sorted(events, key=lambda e: e.start)],
            "events_by_hour": events_by_hour(timeline),
            "ahi": ahi.to_dict(),
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print()
        print("wrote {}".format(args.json))

    if args.plot:
        if not args.show:
            import matplotlib
            matplotlib.use("Agg")
        fig, _ = plot_night_events(session, events, removals, quality)
        if args.show:
            _pyplot().show()
            return 0
        out = args.output or os.path.join(
            args.report_dir,
            "events_session_{0}_{1:%Y-%m-%d}.png".format(
                session.session_id, session.start))
        fig.savefig(out, dpi=110)
        print("wrote {}".format(out))
    return 0


def _cmd_events_report(args) -> int:
    ids = _all_session_ids(args.input)
    sessions = []
    reports = {}
    missing = []
    for sid in ids:
        try:
            session = load_session(args.input, sid, channel=args.channel)
        except ValueError as exc:
            print(str(exc))
            return 2
        path = _q_report_path(args.report_dir, session.session_id, session.start)
        if not os.path.exists(path):
            missing.append((sid, path))
            continue
        sessions.append(session)
        reports[sid] = read_quality_report(path)
    if missing:
        for sid, path in missing:
            print("no QualityReport at {}: run quality.py preprocess for "
                  "session {} first".format(path, sid))
        return 2

    df = events_report(
        sessions, reports, drop_pct=args.drop_pct,
        min_duration_s=args.min_duration_s, apnea_drop_pct=args.apnea_drop_pct,
        min_mask_off_minutes=args.mask_off_minutes)
    print()
    _print_events_report(df)

    out = args.output or os.path.join(args.report_dir, "events_summary.csv")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    df.to_csv(out, index=False)
    print()
    print("wrote {}".format(out))
    return 0


def _print_events_report(df):
    if df.empty:
        print("events report: no sessions")
        return
    print("events report ({} nights):".format(len(df)))
    cols = ["session_id", "date", "usage_hours", "n_events", "n_suspect",
            "estimated_ahi", "estimated_ahi_confident", "n_mask_removals"]
    print("  " + "  ".join("{:<14}".format(c) for c in cols))
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if pd.isna(v):
                cells.append("")
            elif isinstance(v, np.floating) or isinstance(v, float):
                cells.append("{:.3f}".format(v))
            else:
                cells.append(str(v))
        print("  " + "  ".join("{:<14}".format(x) for x in cells))


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "events":
        return _cmd_events(args)
    if args.command == "events-report":
        return _cmd_events_report(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())