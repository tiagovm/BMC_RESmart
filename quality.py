"""Layer 1 of the CPAP analysis roadmap: ingest, preprocess and quality control.

Operates on the data produced by ``resmart_parse.py`` (a CSV export, ideally
with ``-2`` so the 25 Hz ``resA`` waveform is present), reusing the existing
clean/segment loaders instead of duplicating them.

Pipeline per session:

    raw CSV -> reconcile_units -> load_session -> resample_signal
             -> detect_signal_quality -> segment_night -> report/plot

This module is a pure-CLI step with no API/UI: results are printed as
text, persisted as a JSON ``QualityReport`` (consumed by later event and
statistics layers) and optionally drawn as a shaded PNG plot, following
the repository's matplotlib conventions.

Requires pandas/numpy (approved exceptions); ``plot_signal_quality`` needs
matplotlib and mirrors the lazy-backend pattern of ``plotting.py``. Not for
medical use.

CLI:
    python quality.py preprocess --input out2.csv 89 [--report qc_89.json]
                                  [--plot] [-o qc.png] [--show]
                                  [--channel resA] [--limit-hours 4]
"""

import argparse
import dataclasses
import json
import logging
import os
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from analysis import segment_sessions
from plotting import read_segmented_csv
from preprocess import clean_and_preprocess

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("quality")

DEFAULT_NIGHT_START = "22:00"
DEFAULT_NIGHT_END = "07:00"


@dataclasses.dataclass
class SessionData:
    """A single session's raw channel signal plus load-time metadata."""

    session_id: int
    channel: str
    timestamp: pd.Series
    values: pd.Series
    start: pd.Timestamp
    end: pd.Timestamp
    sample_rate_hz: float
    measured_period_s: float
    n: int

    @property
    def duration(self):
        return self.end - self.start


@dataclasses.dataclass
class SuspiciousInterval:
    """A contiguous region flagged by one of the signal-quality detectors."""

    start: pd.Timestamp
    end: pd.Timestamp
    kind: str  # "flatline" | "clipping" | "spike" | "drift"


@dataclasses.dataclass
class QualityReport:
    """Outcome of the quality-control pass for one session."""

    session_id: int
    channel: str
    sample_rate_hz: float
    total_samples: int
    valid_samples: int
    valid_pct: float
    intervals: List[SuspiciousInterval]
    counts_by_kind: Dict[str, int]
    warnings: List[str]
    params: Dict[str, Any]

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "channel": self.channel,
            "sample_rate_hz": self.sample_rate_hz,
            "total_samples": self.total_samples,
            "valid_samples": self.valid_samples,
            "valid_pct": self.valid_pct,
            "counts_by_kind": dict(self.counts_by_kind),
            "warnings": list(self.warnings),
            "params": dict(self.params),
            "intervals": [
                {"start": iv.start.isoformat(), "end": iv.end.isoformat(),
                 "kind": iv.kind}
                for iv in self.intervals
            ],
        }


@dataclasses.dataclass
class NightSegment:
    """One hourly block of a session night and its breathing activity."""

    start: pd.Timestamp
    end: pd.Timestamp
    active: bool
    activity_mean: float
    activity_std: float


def _require_columns(df, columns):
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(
            "dataframe is missing columns: {}".format(", ".join(missing))
        )


def load_session(csv_path, session_id, channel="resA", limit_hours=4):
    """Load the clean ``resA`` (or other channel) signal of one session.

    Adapts the existing loaders rather than re-parsing: the CSV is read with
    ``read_segmented_csv`` when it already carries ``session_id`` (output of
    ``analysis.py -o``), otherwise it is cleaned with ``clean_and_preprocess``
    and segmented with ``segment_sessions`` first. Device/model metadata is
    omitted: an export is single-device so those fields would be constant.

    The sample rate is not stored by the device, so it is inferred from the
    median positive timestamp delta (25 Hz nominal for a ``-2`` export gives
    a 40 ms period).

    Returns a :class:`SessionData`. Raises ValueError with the available
    session ids if the session is unknown, or for a missing channel.
    """
    header = pd.read_csv(csv_path, nrows=0).columns
    if "session_id" in [c.strip() for c in header]:
        frame = read_segmented_csv(csv_path)
    else:
        frame = segment_sessions(
            clean_and_preprocess(csv_path), limit_hours=limit_hours
        )

    sids = sorted(frame["session_id"].unique())
    if session_id not in sids:
        raise ValueError(
            "session {} not found; available session ids: {}".format(
                session_id, sids))
    if channel not in frame.columns:
        raise ValueError(
            "column '{}' not found: re-export the CSV with -2 "
            "(resA/resB/resC) or -1 (pulse) and clean it".format(channel))

    part = frame[frame["session_id"] == session_id]
    ts = part["timestamp"].reset_index(drop=True)
    values = part[channel].astype(float).reset_index(drop=True)

    dt = ts.diff().dropna().dt.total_seconds()
    dt = dt[dt > 0]
    measured_period_s = float(dt.median()) if len(dt) else 0.0
    sample_rate_hz = (1.0 / measured_period_s) if measured_period_s > 0 else 25.0

    return SessionData(
        session_id=int(session_id),
        channel=channel,
        timestamp=ts,
        values=values,
        start=ts.iloc[0],
        end=ts.iloc[-1],
        sample_rate_hz=sample_rate_hz,
        measured_period_s=measured_period_s,
        n=int(len(part)),
    )


def reconcile_units(df, findings: Optional[List[str]] = None):
    """Detect unit/quantity inconsistencies in the CSV header.

    Inspects every column name for unit tokens and decides whether the
    quantity implied by the name matches the unit: e.g. ``tidal_vol`` is a
    *volume* but its header unit ``(L/min)`` is a *flow* unit, so the pair is
    recorded as uncertain rather than silently converted. Columns without a
    unit token (``resA``/``resB``/``resC``/``pulse``/``usage_day``) are raw
    device words of unknown scaling and are marked unknown.

    No conversion is ever applied (raw words stay faithful to the device); a
    log line is emitted for every decision and any ambiguous column is added
    to ``findings`` when supplied. Returns ``df`` unchanged.
    """
    if findings is None:
        findings = []
    pressure_cols = {"IPAP (0.5 cmH2O)", "EPAP (0.5 cmH2O)"}
    ok_pct = "spO2_pct (%)"

    for col in df.columns:
        name = col.strip()
        if name in pressure_cols:
            log.info("unit ok: %s carries cmH2O steps (raw/2)", name)
        elif name == ok_pct:
            log.info("unit ok: %s carries percent", name)
        elif name.endswith("(bpm)") or name.endswith("(breaths/min)"):
            log.info("unit ok: %s carries a rate", name)
        elif "(L/min)" in name:
            if any(t in name.lower() for t in ("vol", "volume")):
                msg = ("uncertain: {} is a volume but its header unit "
                       "(L/min) is a flow unit; left as stored, do not "
                       "convert blindly").format(name)
                log.warning(msg)
                findings.append(msg)
            else:
                log.info("unit ok: %s carries L/min flow", name)
        else:
            msg = ("unknown: {} has no unit token in its header; raw word, "
                   "scaling not confirmed").format(name)
            log.info(msg)
            findings.append(msg)
    return df


def resample_signal(df, target_hz=None, method="median", column=None,
                    findings: Optional[List[str]] = None):
    """Uniformly resample a signal column onto an exact-rate grid.

    ``df`` must have a datetime64 ``timestamp`` column and one signal column
    (chosen by ``column``; defaults to the only remaining numeric column).
    The original sample timestamps are kept in a new ``orig_timestamp``
    column: for downsampling bins it is the first original sample of the
    bin, for upsampling the nearest original sample.

    ``target_hz`` defaults to the measured rate (1 / median positive delta,
    i.e. ~25 for a ``-2`` export). ``method`` selects the per-bin aggregate
    for downsampling ("median" or "mean"); upsampling fills missing grid
    points from the nearest original sample.

    Gaps in the original data spanning more than 5 seconds are reported
    through ``findings`` (and logged) as (start, end) intervals.

    Returns the resampled DataFrame with columns ``timestamp`` (grid),
    ``orig_timestamp`` and the signal column; grid points covered by no
    sample become NaN, keeping gaps visible to the QC pass.
    """
    if findings is None:
        findings = []
    _require_columns(df, ["timestamp"])
    if column is None:
        candidates = [
            c for c in df.columns
            if c not in ("timestamp", "orig_timestamp")
            and pd.api.types.is_numeric_dtype(df[c])
        ]
        if len(candidates) != 1:
            raise ValueError(
                "cannot guess the signal column from {!r}; pass column=".format(
                    candidates))
        column = candidates[0]

    if method not in ("median", "mean"):
        raise ValueError("method must be 'median' or 'mean', got {!r}".format(method))
    ts = df["timestamp"]
    values = pd.to_numeric(df[column], errors="coerce")
    orig = pd.DataFrame({column: values.values}, index=ts.values)
    orig.index.name = "timestamp"

    dt = ts.diff().dropna().dt.total_seconds()
    dt = dt[dt > 0]
    measured_period = float(dt.median()) if len(dt) else 0.040
    measured_hz = 1.0 / measured_period if measured_period else 25.0
    period = 1.0 / target_hz if target_hz else measured_period
    upsample = target_hz is not None and target_hz > measured_hz * (1 + 1e-6)
    step_ns = max(1, int(round(period * 1e9)))

    t0 = ts.iloc[0]
    t0_ns = t0.value
    dur_s = (ts.iloc[-1] - t0).total_seconds()
    n_target = max(1, int(np.round(dur_s / period)) + 1)
    t_step = pd.Timedelta(step_ns, unit="ns")
    grid = pd.DatetimeIndex(t0 + np.arange(n_target) * t_step)

    offsets_ns = ts.values.view("i8") - t0_ns
    bins = np.floor(offsets_ns / step_ns).astype("int64")
    bins = np.clip(bins, 0, n_target - 1)

    if upsample:
        # Upsampling: take the nearest original sample at each grid point.
        mapper = pd.Series(orig[column], index=orig.index)
        res = mapper.reindex(grid, method="nearest", tolerance=t_step)
        res_orig = pd.Series(orig.index, index=orig.index).reindex(
            grid, method="nearest", tolerance=t_step)
        out_index = pd.RangeIndex(0, n_target)
        out = pd.DataFrame({
            "timestamp": pd.Series(grid, index=out_index),
            "orig_timestamp": pd.Series(res_orig.values, index=out_index),
            column: pd.Series(res.values, index=out_index),
        })
        missing_bins = list(np.flatnonzero(res.isna().to_numpy()))
    else:
        # Downsampling / equal rate: aggregate each grid bin.
        agg = "median" if method == "median" else "mean"
        tagged = orig.copy()
        tagged["_bin"] = bins
        grouped = tagged.groupby("_bin")[column].agg(agg)
        res = pd.Series(np.nan, index=np.arange(n_target))
        res.loc[grouped.index] = grouped.values

        first_orig_ts = tagged.reset_index().groupby("_bin")["timestamp"] \
            .agg(lambda s: s.iloc[0])

        out_index = pd.RangeIndex(0, n_target)
        out = pd.DataFrame({
            "timestamp": pd.Series(grid, index=out_index),
            "orig_timestamp": pd.Series(
                np.nan, index=out_index, dtype="datetime64[ns]"),
            column: pd.Series(res.values, index=out_index),
        })
        out["orig_timestamp"] = first_orig_ts.reindex(out_index).values

        # Bins with no sample count as gaps.
        present = set(int(b) for b in bins)
        missing_bins = [i for i in range(n_target) if i not in present]

    # Interior gaps longer than 5 s -> warnings.
    if missing_bins:
        runs = _consecutive_runs(np.asarray(missing_bins, dtype="int64"))
        for r in runs:
            gap_s = len(r) * period
            if gap_s > 5.0:
                g0 = grid[r[0]]
                g1 = grid[r[-1]] + t_step
                msg = "gap > 5 s in {}: {} -> {} ({:.1f} s)".format(
                    column, g0.isoformat(), g1.isoformat(), gap_s)
                log.warning(msg)
                findings.append(msg)

    return out[["timestamp", "orig_timestamp", column]]


def _consecutive_runs(idxs):
    if len(idxs) == 0:
        return []
    runs = []
    start = idxs[0]
    prev = idxs[0]
    for i in idxs[1:]:
        if i != prev + 1:
            runs.append(list(range(start, prev + 1)))
            start = i
        prev = i
    runs.append(list(range(start, prev + 1)))
    return runs


def _mask_to_intervals(mask, ts, kind):
    intervals = []
    on = False
    start = None
    ts_list = list(ts)
    n = len(mask)
    for i, m in enumerate(mask):
        if m and not on:
            on = True
            start = ts_list[i]
        elif not m and on:
            intervals.append(SuspiciousInterval(start, ts_list[i - 1], kind))
            on = False
    if on:
        intervals.append(SuspiciousInterval(start, ts_list[n - 1], kind))
    return intervals


def detect_signal_quality(df, spike_rel_factor=25.0, clip_lo=None,
                          clip_hi=None, drift_window_s=300.0,
                          drift_rel_tol=0.5,
                          findings: Optional[List[str]] = None,
                          session_id: Optional[int] = None,
                          channel: Optional[str] = None):
    """Detect flatline, clipping, spikes and baseline drift in a signal.

    ``df`` is either a :class:`SessionData` or a DataFrame with ``timestamp``
    and a single signal column (typically the output of ``resample_signal``);
    if a client passes a full frame, ``column``-like guessing selects the
    only numeric column. ``session_id``/``channel`` describe the frame when it
    is not a :class:`SessionData` (otherwise the session is taken from it).

    Semantics are chosen for oscillatory breath/flow signals (like resA at
    25 Hz), where sharp transitions are legitimate signal, not artifacts:

    * flatline - rolling std over ~1 s below ``1e-3 * scale``. Flat stretches
                 are dead/zeroed sensor data (e.g. the device idle).
    * clipping - samples pinned at an A/D saturation bound. ``clip_lo`` /
                 ``clip_hi`` are the sensor's physical extremes, when known;
                 default ``None`` means no clipping detection (the observed
                 extremes of the trace itself are legitimate signal, not
                 clipping). Pass the real raw-word bounds to enable it.
    * spike    - impulse noise: a sample that jumps OUT of the trace and back
                 in, i.e. its step to BOTH neighbours exceeds
                 ``spike_rel_factor`` (25) times the ROLLING median of the
                 trace's own POSITIVE sample-to-sample steps, with opposite
                 signs. The reference is local (breath "holds" do not collapse
                 it), so a sharp legitimate peek costs the same as the edges
                 around it; a ramp of consecutive large steps is a legitimate
                 breath edge and is never flagged. The adaptive threshold
                 keeps equal sensitivity across a smooth pressure channel and
                 a jumpy flow channel.
    * drift    - slow baseline walk of the signal ENVELOPE (1 Hz median bins):
                 a rolling mean over ``drift_window_s`` (default 300 s) that
                 leaves the session median of the envelope by more than
                 ``drift_rel_tol`` (default 0.5) of the envelope scale.
                 Operates on the envelope so periodic breathing is not drift;
                 requires at least ``drift_window_s`` of data (else skipped).

    Overlapping flags are resolved for the validity fraction (a sample counts
    as invalid once regardless of how many kinds flag it) but one interval is
    emitted per kind. Returns a :class:`QualityReport` with the suspicious
    intervals, per-kind counts, per-sample valid fraction and the parameters
    actually used. Input problems are reported through ``findings``/logging.
    """
    if findings is None:
        findings = []
    if isinstance(df, SessionData):
        sid = df.session_id if session_id is None else session_id
        channel = df.channel if channel is None else channel
        ts = df.timestamp
        if not isinstance(ts, pd.Series):
            ts = pd.Series(ts)
        ts = ts.reset_index(drop=True)
        raw = df.values
        if not isinstance(raw, pd.Series):
            raw = pd.Series(raw)
        values = pd.to_numeric(raw, errors="coerce").reset_index(drop=True)
        rate = df.sample_rate_hz
    else:
        _require_columns(df, ["timestamp"])
        sid = session_id if session_id is not None else 0
        ts = df["timestamp"].reset_index(drop=True)
        candidates = [
            c for c in df.columns
            if c not in ("timestamp", "orig_timestamp")
            and pd.api.types.is_numeric_dtype(df[c])]
        if len(candidates) != 1:
            raise ValueError(
                "cannot guess the signal column from {!r}".format(candidates))
        values = pd.to_numeric(df[candidates[0]], errors="coerce").reset_index(drop=True)
        channel = channel if channel is not None else candidates[0]
        dt = ts.diff().dropna().dt.total_seconds()
        dt = dt[dt > 0]
        rate = 1.0 / float(dt.median()) if len(dt) else 25.0

    n = len(values)
    if n == 0 or values.dropna().empty:
        raise ValueError("no valid samples to run quality control on")

    v = values.astype(float)
    scale = float(np.nanstd(v)) or 1.0
    if np.isnan(scale) or scale == 0:
        scale = 1.0
    w = 25
    flat_tol = 1e-3 * scale
    roll_std = v.rolling(w, center=True, min_periods=5).std()
    flatline = (roll_std < flat_tol).fillna(False).to_numpy()
    spread = float(np.nanpercentile(v, 75) - np.nanpercentile(v, 25))

    # Spike: transition-based impulse detection. A legitimate breath edge is
    # a RUN of large sample-to-sample steps in the same direction; a sensor
    # impulse is a single sample that jumps out of the trace and back, i.e.
    # both of its bounding steps exceed the local step size and point in
    # opposite directions. The reference step is the ROLLING median of the
    # trace's own POSITIVE sample-to-sample steps (a breath "hold" writes 0
    # steps, and averaging those in would collapse the reference exactly when
    # a pause frames a tiny jitter peak), so a sharp legitimate peek costs the
    # same as the edges around it. The threshold is ``spike_rel_factor`` (25)
    # above that reference; a ramp of consecutive large steps is a legitimate
    # breath edge and is never flagged. On the real 25 Hz resA channel the
    # typical step is ~1 raw word and legit breath ramps sustain 10-20
    # words/sample, far below an impulse born of a device glitch.
    arr = v.to_numpy(dtype=float)
    dprev = np.full(n, np.nan)
    dprev[1:] = np.abs(arr[1:] - arr[:-1])
    dnext = np.full(n, np.nan)
    dnext[:-1] = np.abs(arr[1:] - arr[:-1])
    dseq = pd.Series(np.abs(np.diff(arr, prepend=np.nan)), index=v.index)
    dpos = dseq.where(dseq > 0)
    step_median = float(np.nanmedian(dpos))
    if not step_median > 0:
        step_median = 0.0
    local_step = dpos.rolling(w, center=True, min_periods=1).median()
    ref_step = np.maximum(
        local_step.fillna(step_median).to_numpy(), 1e-9 * scale)
    thr = spike_rel_factor * ref_step
    dvp = np.full(n, np.nan)
    dvp[1:] = arr[1:] - arr[:-1]
    dvn = np.full(n, np.nan)
    dvn[:-1] = arr[1:] - arr[:-1]
    spike = (
        (dprev > thr) & (dnext > thr) & (np.signbit(dvp) != np.signbit(dvn))
    )
    spike = np.nan_to_num(spike, nan=False).astype(bool)

    # Clipping: only explicit sensor saturation bounds; default: no check.
    # The 0.5 %/99.5 % quantiles of a breathing trace are its legitimate
    # peaks and troughs, NOT clipping, so deriving bounds from the data is
    # wrong. Raw-word bounds for resA are 0..4095 (rarely reached).
    if clip_lo is not None or clip_hi is not None:
        lo = float("-inf") if clip_lo is None else clip_lo
        hi = float("inf") if clip_hi is None else clip_hi
        eps = 1e-6 * max(1.0, abs(hi - lo))
        clipping = ((v <= lo + eps) | (v >= hi - eps)).fillna(False).to_numpy()
    else:
        clipping = np.zeros(n, dtype=bool)

    # Drift: baseline walk of the 1 Hz median envelope (periodic breathing is
    # a fast, bounded oscillation - it cannot look like drift). Skip entirely
    # when the envelope is shorter than the drift window.
    drift = np.zeros(n, dtype=bool)
    ts_ns = pd.DatetimeIndex(ts).asi8
    sec_bin = ts_ns // 1_000_000_000
    env = pd.Series(arr).groupby(sec_bin).median()
    env_scale = float(np.nanstd(env.values)) if len(env) > 1 else 0.0
    if not env_scale > 0:
        env_scale = 1e-9
    drift_win = max(2, int(round(drift_window_s)))
    if len(env) >= drift_win:
        env_med = float(np.nanmedian(env))
        env_roll = env.rolling(drift_win, min_periods=1).mean()
        bad = (env_roll - env_med).abs() > drift_rel_tol * env_scale
        bad_secs = set(int(s) for s in env.index[bad.fillna(False).to_numpy()])
        drift = np.array([True if int(s) in bad_secs else False for s in sec_bin])
    else:
        findings.append(
            "drift detection skipped: envelope shorter than the drift window "
            "({:.0f} s of data needed)".format(drift_window_s))

    masks = {"spike": spike, "flatline": flatline,
             "clipping": clipping, "drift": drift}
    union = np.zeros(n, dtype=bool)
    for m in masks.values():
        union |= m

    intervals = []
    for kind, m in masks.items():
        intervals.extend(_mask_to_intervals(m, ts, kind))
    intervals.sort(key=lambda iv: iv.start)

    counts = {kind: int(np.count_nonzero(m)) for kind, m in masks.items()}
    total = n
    valid = int(np.count_nonzero(~union))
    valid_pct = 100.0 * valid / total if total else 0.0

    params = {
        "spike_rel_factor": spike_rel_factor,
        "spike_thr": spike_rel_factor * max(step_median, 1e-9 * scale),
        "step_median": step_median,
        "flatline_tol": flat_tol,
        "clip_lo": clip_lo,
        "clip_hi": clip_hi,
        "drift_window_s": drift_window_s,
        "drift_rel_tol": drift_rel_tol,
        "envelope_scale": env_scale,
        "scale": scale,
        "spread": spread,
    }
    return QualityReport(
        session_id=int(sid), channel=channel,
        sample_rate_hz=float(rate), total_samples=total,
        valid_samples=valid, valid_pct=valid_pct,
        intervals=intervals, counts_by_kind=counts,
        warnings=list(findings), params=params)


def segment_night(df, bed_start=DEFAULT_NIGHT_START, bed_end=DEFAULT_NIGHT_END,
                  min_activity_amplitude=None, hour_block="1h"):
    """Split a session night into hourly blocks and annotate breathing.

    ``df`` is the resampled session frame (timestamp + signal column).
    Only the part inside the nightly window (``bed_start`` -> ``bed_end``,
    default 22:00 -> 07:00) is considered; blocks are consecutive intervals
    of ``hour_block`` (1 h). A block is ``active`` when the rolling
    peak-to-trough amplitude (5 s window) of the signal stays within a
    physiological band — its default is the 10th percentile of the session's
    positive amplitudes, i.e. derived from the data rather than hard coded,
    since ``resA`` is an unknown unit.

    The real start/end of use is the span of the active blocks (a block with
    zero signal or flat activity is inactive). Returns ``list[NightSegment]``.
    """
    _require_columns(df, ["timestamp"])
    sig = [c for c in df.columns
           if c not in ("timestamp", "orig_timestamp")
           and pd.api.types.is_numeric_dtype(df[c])]
    if len(sig) != 1:
        raise ValueError("need exactly one signal column in the frame")
    col = sig[0]
    ts = df["timestamp"]
    v = df[col].astype(float)

    amp = (v.rolling(125, center=True, min_periods=20).max()
           - v.rolling(125, center=True, min_periods=20).min())

    if min_activity_amplitude is None:
        pos = amp.dropna()
        pos = pos[pos > 0]
        if len(pos):
            min_activity_amplitude = float(pos.quantile(0.10))
        else:
            min_activity_amplitude = 1e-12

    # Restrict to the nightly window(s) spanned by the session.
    win_start = pd.to_datetime(bed_start).time()
    win_end = pd.to_datetime(bed_end).time()
    starts = []
    mask_win = pd.Series(False, index=ts.index)
    t = ts.dt.time
    for i in range(len(ts)):
        tm = t.iloc[i]
        in_win = (tm >= win_start) or (tm < win_end)
        mask_win.iloc[i] = in_win

    sub = pd.DataFrame({"timestamp": ts[mask_win], "amp": amp[mask_win]})
    if sub.empty:
        return []

    start_t = sub["timestamp"].dt.floor("h").iloc[0]
    end_t = sub["timestamp"].dt.ceil("h").iloc[-1]
    blocks = pd.date_range(start_t, end_t, freq=hour_block)

    segments = []
    block_amp = sub.set_index("timestamp")["amp"]
    for b_start, b_end in zip(blocks[:-1], blocks[1:]):
        seg = block_amp[(block_amp.index >= b_start) & (block_amp.index < b_end)]
        if seg.empty:
            continue
        mean_amp = float(seg.mean())
        std_amp = float(seg.std())
        active = mean_amp >= min_activity_amplitude
        segments.append(NightSegment(
            start=b_start, end=b_end, active=active,
            activity_mean=mean_amp, activity_std=std_amp))
    return segments


_PLT = None


def _pyplot():
    global _PLT
    if _PLT is None:
        from matplotlib import pyplot
        _PLT = pyplot
    return _PLT


INTERVAL_COLORS = {
    "flatline": "0.75",
    "clipping": "red",
    "spike": "orange",
    "drift": "violet",
}


def plot_signal_quality(session, report):
    """Plot the channel trace with suspect regions shaded by detector kind.

    Needs matplotlib (lazy import, same Agg/backend pattern as
    ``plotting.py``). Returns the ``(figure, axes)`` pair; the CLI decides
    whether to save it or call ``--show``.
    """
    import matplotlib.patches as mpatches

    fig, ax = _pyplot().subplots(figsize=(14, 4))
    ax.plot(session.timestamp, session.values, color="tab:blue", linewidth=0.6)
    for iv in report.intervals:
        ax.axvspan(iv.start, iv.end, alpha=0.25,
                   facecolor=INTERVAL_COLORS.get(iv.kind, "gray"))
    ax.set_title("Signal quality - session {} ({}) {:%Y-%m-%d %H:%M} -> {:%H:%M}".format(
        session.session_id, session.channel, session.start, session.end))
    ax.set_xlabel("Time")
    ax.set_ylabel("{} (raw units)".format(session.channel))
    handles = [mpatches.Patch(color=c, alpha=0.4, label=k)
               for k, c in INTERVAL_COLORS.items()
               if report.counts_by_kind.get(k, 0) > 0]
    if handles:
        ax.legend(handles=handles, loc="upper right", fontsize="small")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig, ax


def write_quality_report(report, path):
    """Persist a QualityReport as JSON for the downstream layers."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False)
    return path


def read_quality_report(path):
    """Load a persisted QualityReport (JSON) back into the dataclasses.

    Inverse of :func:`write_quality_report`; restores interval timestamps as
    ``pd.Timestamp``. Raises ``ValueError`` for files missing the required
    ``session_id``/``channel``/``sample_rate_hz`` fields.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return QualityReport(
        session_id=int(data["session_id"]),
        channel=str(data["channel"]),
        sample_rate_hz=float(data["sample_rate_hz"]),
        total_samples=int(data.get("total_samples", 0)),
        valid_samples=int(data.get("valid_samples", 0)),
        valid_pct=float(data.get("valid_pct", 0.0)),
        intervals=[
            SuspiciousInterval(
                start=pd.Timestamp(iv["start"]),
                end=pd.Timestamp(iv["end"]),
                kind=str(iv["kind"]),
            )
            for iv in data.get("intervals", [])
        ],
        counts_by_kind=dict(data.get("counts_by_kind", {})),
        warnings=list(data.get("warnings", [])),
        params=dict(data.get("params", {})),
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Layer 1: ingest, preprocess and quality-control a RESmart "
            "session (load -> reconcile units -> resample -> QC -> night "
            "segments). Prints text output, writes a JSON QualityReport and "
            "optionally a shaded plot."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "preprocess",
        help="run the full layer-1 pipeline for one session")
    p.add_argument("--input", required=True,
                   help="CSV export from resmart_parse.py (with -2 for "
                        "resA) or a segmented CSV (with session_id)")
    p.add_argument("session_id", type=int,
                   help="session id to process")
    p.add_argument("--channel", default="resA",
                   choices=["resA", "resB", "resC", "pulse"],
                   help="signal channel to analyze (default: resA)")
    p.add_argument("--report", default=None,
                   help="write the QualityReport JSON to this path (default: "
                        "<report-dir>/qc_session_<id>_<date>.json)")
    p.add_argument("--report-dir", default="reports",
                   help="directory for the default QualityReport JSON "
                        "(default: reports/)")
    p.add_argument("--target-hz", type=float, default=None,
                   help="resample the signal to this rate "
                        "(default: measured rate, ~25)")
    p.add_argument("--spike-rel-factor", type=float, default=25.0,
                   help="spike threshold as a multiple of the trace's typical "
                        "sample-to-sample step (default: 25.0)")
    p.add_argument("--clip-lo", type=float, default=None,
                   help="sensor A/D lower bound to flag clipping; when omitted "
                        "no clipping detection runs")
    p.add_argument("--clip-hi", type=float, default=None,
                   help="sensor A/D upper bound to flag clipping; when omitted "
                        "no clipping detection runs")
    p.add_argument("--limit-hours", type=float, default=4,
                   help="segmentation gap threshold (default: 4 h)")
    p.add_argument("--plot", action="store_true",
                   help="also render the shaded signal-quality plot")
    p.add_argument("-o", "--output", default=None,
                   help="PNG file to write with --plot "
                        "(default: qc_session_<id>_<date>.png next to the "
                        "input)")
    p.add_argument("--show", action="store_true",
                   help="with --plot, display the plot on screen")
    return parser


def _print_report(report):
    print("session {} | channel {} | rate {:.2f} Hz | {} samples, "
          "{:.1f}% valid".format(
              report.session_id, report.channel, report.sample_rate_hz,
              report.total_samples, report.valid_pct))
    if report.intervals:
        print("suspect intervals ({}):".format(len(report.intervals)))
        print("  {:<24} {:<24} {}".format("start", "end", "kind"))
        for iv in sorted(report.intervals, key=lambda i: i.start):
            print("  {:%Y-%m-%d %H:%M:%S} -> {:%Y-%m-%d %H:%M:%S}  {}".format(
                iv.start, iv.end, iv.kind))
    else:
        print("no suspect intervals")
    if report.warnings:
        print("warnings:")
        for w in report.warnings:
            print("  - {}".format(w))


def _print_segments(segments):
    if not segments:
        print("no night segments (session outside the bed window?)")
        return
    active = [s for s in segments if s.active]
    print("night segments: {} blocks, {} active".format(len(segments), len(active)))
    print("  {:<24} {:<24} {:<7} {:>10} {:>10}".format(
        "start", "end", "active", "mean_amp", "std_amp"))
    for s in segments:
        print("  {:%Y-%m-%d %H:%M} -> {:%Y-%m-%d %H:%M}  {!s:<7} {:>10.1f} {:>10.1f}".format(
            s.start, s.end, s.active, s.activity_mean, s.activity_std))
    if active:
        print("real use of signal: {} -> {}".format(
            active[0].start, active[-1].end))


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "preprocess":
        findings = []
        _print_load = logging.getLogger("quality")

        session = load_session(
            args.input, args.session_id, channel=args.channel,
            limit_hours=args.limit_hours)
        print("loaded session {}: {:%Y-%m-%d %H:%M} -> {:%H:%M} "
              "({} samples, measured {:.1f} Hz)".format(
                  session.session_id, session.start, session.end,
                  session.n, session.sample_rate_hz))

        # reconcile runs on the header of the cleaned frame the session came
        # from; load_session already produced it, so reflect and check units.
        frame = pd.DataFrame({
            "timestamp": session.timestamp,
            session.channel: session.values,
        })
        reconcile_units(frame, findings)

        resampled = resample_signal(
            frame, target_hz=args.target_hz, method="median",
            column=session.channel, findings=findings)

        report = detect_signal_quality(
            resampled, spike_rel_factor=args.spike_rel_factor,
            clip_lo=args.clip_lo, clip_hi=args.clip_hi, findings=findings,
            session_id=session.session_id, channel=session.channel)

        segments = segment_night(resampled)

        print()
        _print_report(report)
        print()
        _print_segments(segments)

        if args.report:
            report_path = args.report
        else:
            report_path = os.path.join(
                args.report_dir,
                "qc_session_{0}_{1:%Y-%m-%d}.json".format(
                    session.session_id, session.start))
            os.makedirs(args.report_dir, exist_ok=True)
        write_quality_report(report, report_path)
        print()
        print("wrote {}".format(report_path))

        if args.plot:
            if not args.show:
                import matplotlib
                matplotlib.use("Agg")
            fig, _ = plot_signal_quality(session, report)
            if args.show:
                _pyplot().show()
                return 0
            if args.output is None:
                out = os.path.join(
                    os.path.dirname(os.path.abspath(args.input)),
                    "qc_session_{0}_{1:%Y-%m-%d}.png".format(
                        session.session_id, session.start))
            else:
                out = args.output
            fig.savefig(out, dpi=110)
            print("wrote {}".format(out))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())