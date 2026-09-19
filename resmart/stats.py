"""Layer 2 of the CPAP analysis roadmap: descriptive per-session statistics.

Consumes Layer 1 outputs — a :class:`SessionData` (from ``quality.load_session``)
and the persisted JSON :class:`QualityReport` (``quality.preprocess``) — and
never re-does loading or quality control:

    SessionData + QualityReport
        -> session_summary (SessionStats)
        -> volume_distribution (DistributionStats)
        -> respiratory_rate (per-block DataFrame)
        -> nightly_trend_summary (one row per night) -> reports/nightly_trend.csv

Every metric is computed only over the QC-valid samples (valid mask rebuilt
deterministically from the report's ``intervals``) and carries the coverage
/validity fraction it was computed on. ``resA`` carries no confirmed scaling,
so flow/volume values are reported in *raw units* with a warning — never
converted to liters. ``rr`` here is an *estimate derived from the flow
channel*, not a clinical measure.

CLI:
    python stats.py stats 89 --input sessions.csv [--json out.json]
                             [--plot] [-o stats.png] [--show] [--report-path qc.json]
    python stats.py trend --input sessions.csv [--all] [--output reports/nightly_trend.csv]
Not for medical use.
"""

import dataclasses
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional

import numpy as np
import pandas as pd

from resmart.quality import (
    QualityReport,
    SessionData,
    load_session,
    read_quality_report,
    resample_signal,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("stats")

BREATH_MIN_S = 0.25
BREATH_MAX_S = 8.0
BASELINE_S = 45.0
MIN_PEAK_FACTOR = 12.0
RR_LAG_MIN_S = 2.0
RR_LAG_MAX_S = 10.0
RR_MIN_AC = 0.2
RR_MIN_VALID_FRAC = 0.5


@dataclasses.dataclass
class SessionStats:
    """Descriptive statistics of one session over its QC-valid signal."""

    session_id: int
    channel: str
    start: pd.Timestamp
    end: pd.Timestamp
    n_total: int
    n_valid: int
    signal_valid_pct: float
    total_duration_s: float
    usage_duration_s: float
    night_usage_pct: float
    flow_mean: Optional[float]
    flow_median: Optional[float]
    flow_iqr: Optional[float]
    flow_p05: Optional[float]
    flow_p95: Optional[float]
    n_breaths: int
    tidal_mode: Optional[float]
    tidal_p90: Optional[float]
    tidal_units: str
    rr_mean: Optional[float]
    rr_median: Optional[float]
    rr_iqr: Optional[float]
    n_blocks: int
    n_blocks_valid: int
    rr_coverage_pct: float
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "channel": self.channel,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "n_total": self.n_total,
            "n_valid": self.n_valid,
            "signal_valid_pct": self.signal_valid_pct,
            "total_duration_s": self.total_duration_s,
            "usage_duration_s": self.usage_duration_s,
            "night_usage_pct": self.night_usage_pct,
            "flow_mean": self.flow_mean,
            "flow_median": self.flow_median,
            "flow_iqr": self.flow_iqr,
            "flow_p05": self.flow_p05,
            "flow_p95": self.flow_p95,
            "n_breaths": self.n_breaths,
            "tidal_mode": self.tidal_mode,
            "tidal_p90": self.tidal_p90,
            "tidal_units": self.tidal_units,
            "rr_mean": self.rr_mean,
            "rr_median": self.rr_median,
            "rr_iqr": self.rr_iqr,
            "n_blocks": self.n_blocks,
            "n_blocks_valid": self.n_blocks_valid,
            "rr_coverage_pct": self.rr_coverage_pct,
            "warnings": list(self.warnings),
        }


@dataclasses.dataclass
class DistributionStats:
    """Volume distribution of one session: histogram bins and moments."""

    session_id: int
    n_breaths: int
    method: str
    units: str
    bin_edges: List[float]
    counts: List[int]
    skew: Optional[float]
    kurtosis: Optional[float]
    hartigan_bc: Optional[float]
    bimodal: Optional[bool]
    global_mode: Optional[float]
    global_mode_interval: Optional[List[float]]
    secondary_modes: List[Dict[str, Any]]
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _analysis_frame(session: SessionData, target_hz: Optional[float] = None):
    """Return the session signal on the same regular grid the QC used."""
    frame = pd.DataFrame({
        "timestamp": session.timestamp,
        session.channel: session.values,
    })
    return resample_signal(
        frame, target_hz=target_hz, method="median", column=session.channel)


def valid_mask(grid_ts: pd.Series, report: QualityReport) -> np.ndarray:
    """Reconstruct the QC per-sample valid mask from a QualityReport.

    The report persists contiguous suspect ``intervals`` per kind; the validity
    mask is their complement (union over kinds), which is exactly how
    ``detect_signal_quality`` computed ``valid_samples``.
    """
    ts = np.asarray(grid_ts.to_numpy(), dtype="datetime64[ns]")
    invalid = np.zeros(len(ts), dtype=bool)
    for iv in report.intervals:
        s = np.datetime64(iv.start)
        e = np.datetime64(iv.end)
        invalid |= (ts >= s) & (ts <= e)
    return ~invalid


def _period_s(session: SessionData) -> float:
    period = getattr(session, "measured_period_s", 0.0) or 0.0
    if period > 0:
        return period
    rate = getattr(session, "sample_rate_hz", 0.0) or 0.0
    return 1.0 / rate if rate > 0 else 0.04


def _consecutive_runs(mask: np.ndarray):
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


def _per_breath_volumes(values, valid, period_s: float,
                        baseline_s: float = BASELINE_S,
                        min_peak_units: Optional[float] = None) -> np.ndarray:
    """Per-breath flow integrals (raw units x s) from the QC-valid signal.

    A slow baseline (rolling median over ``baseline_s``, several breath cycles
    on purpose) centers the flow around the expiratory plateau; each positive
    excursion above it is one inspiratory phase and its integral is a breath's
    raw volume. To keep jitter lobes (tiny sub-breath crossings of the
    baseline) out of the tally, an excursion must also reach a minimum peak
    height: ``min_peak_units`` when given, else ``MIN_PEAK_FACTOR`` times the
    signal's noise scale (the median positive sample-to-sample step).
    """
    win = max(7, int(round(baseline_s / period_s)))
    base = pd.Series(values).rolling(
        win, center=True, min_periods=win // 3).median()
    base = base.fillna(pd.Series(values, index=base.index)).to_numpy()
    f = values - base
    if min_peak_units is None:
        d = np.abs(np.diff(values[valid], prepend=values[valid][0])) \
            if np.any(valid) else np.asarray([np.nan])
        pos = d[d > 0]
        if len(pos):
            min_peak_units = MIN_PEAK_FACTOR * float(np.nanmedian(pos))
        else:
            min_peak_units = 0.0
    pos = (f > 0.0) & valid
    volumes = []
    for i0, i1 in _consecutive_runs(pos):
        seg = f[i0:i1 + 1]
        dur = len(seg) * period_s
        if BREATH_MIN_S <= dur <= BREATH_MAX_S and \
                seg.max() > min_peak_units:
            volumes.append(float(np.sum(seg) * period_s))
    return np.asarray(volumes, dtype=float)


def _fd_bin_edges(x: np.ndarray) -> np.ndarray:
    """Freedman-Diaconis bin edges, never coarser than the sqrt-n rule.

    FD width (2*IQR*n^(-1/3)) is adaptive to the spread but collapses to a
    handful of bins for small n or a wide bimodal spread, hiding the modes it
    exists to find; the width is floored so the number of bins is at least
    ~sqrt(n), which keeps well-separated populations resolvable at any size.
    """
    n = len(x)
    lo = float(np.nanmin(x))
    hi = float(np.nanmax(x))
    if not (hi > lo):
        return np.asarray([lo, lo + 1.0])
    iqr = float(np.nanpercentile(x, 75) - np.nanpercentile(x, 25))
    n_min = max(2, int(round(np.sqrt(n))))
    h_floor = (hi - lo) / float(n_min)
    h = 2.0 * iqr / float(n) ** (1.0 / 3.0)
    if not (np.isfinite(h) and h > 0):
        h = h_floor
    h = min(h, h_floor)
    edges = np.arange(lo, hi + h, h)
    if edges[-1] < hi:
        edges = np.append(edges, hi)
    return edges


def _bin_mode_value(edges: np.ndarray, counts: np.ndarray):
    """Mode (with quadratic refinement) and its bin interval."""
    peak = int(np.argmax(counts))
    lo_b, hi_b = edges[peak], edges[peak + 1]
    center = 0.5 * (lo_b + hi_b)
    c = counts[peak]
    c_l = counts[peak - 1] if peak > 0 else c
    c_r = counts[peak + 1] if peak + 1 < len(counts) else c
    half = (hi_b - lo_b) / 2.0
    denom = max(c_l - 2.0 * c + c_r, 1e-12)
    delta = half * (c_l - c_r) / (2.0 * denom)
    value = center + min(max(delta, -half), half)
    return value, [float(lo_b), float(hi_b)]


def _secondary_peaks(counts: np.ndarray, edges: np.ndarray) -> List[int]:
    """Bins forming real secondary modes of the histogram.

    A secondary mode must be a local maximum with at least 15 % of the main
    peak's count AND be separated from the main mode by a genuine valley
    (the lowest count between the two peaks dips below 70 % of the smaller
    peak). The valley criterion suppresses the spurious shoulders of a single
    smooth hump, which a raw local-maximum scan picks up at fine binning.
    """
    n = len(counts)
    main = int(np.argmax(counts))
    main_c = counts[main]
    candidates = []
    for i in range(n):
        if i == main:
            continue
        if i == 0 and n > 1 and counts[0] > counts[1]:
            candidates.append(0)
        elif i == n - 1 and n > 1 and counts[-1] > counts[-2]:
            candidates.append(n - 1)
        elif 0 < i < n - 1 and \
                counts[i] >= counts[i - 1] and counts[i] > counts[i + 1]:
            candidates.append(i)
    out = []
    for p in sorted(candidates, key=lambda i: -counts[i]):
        if counts[p] < 0.15 * main_c:
            continue
        a, b = min(p, main), max(p, main)
        valley = min(counts[a:b + 1])
        if valley < 0.7 * min(counts[p], main_c):
            out.append(int(p))
    return out


def volume_distribution(session: SessionData, quality: QualityReport,
                        warnings: Optional[List[str]] = None) -> DistributionStats:
    """Distribution of per-breath volumes of one session.

    Builds on the same per-breath flow integrals as :func:`session_summary`,
    binned with the Freedman-Diaconis rule, and reports its global mode,
    secondary modes (local peaks ≥ 15 % of the main bin), skew, kurtosis and
    Hartigan's bimodality coefficient (BC > 0.555 hints at bimodality).
    ResA has no confirmed scaling, so all values are in raw units.
    """
    if warnings is None:
        warnings = []
    resampled = _analysis_frame(session)
    col = session.channel
    ts = resampled["timestamp"]
    values = resampled[col].to_numpy(dtype=float)
    mask = valid_mask(ts, quality) & np.isfinite(values)
    period = _period_s(session)
    vols = _per_breath_volumes(values, mask, period)
    units = "raw units (uncertain scaling)"
    if len(vols) == 0:
        warnings.append(
            "no breath cycles found in the QC-valid signal ({})".format(
                session.channel))
        return DistributionStats(
            session_id=session.session_id, n_breaths=0, method="fd",
            units=units, bin_edges=[], counts=[], skew=None, kurtosis=None,
            hartigan_bc=None, bimodal=None, global_mode=None,
            global_mode_interval=None, secondary_modes=[], warnings=list(warnings))

    edges = _fd_bin_edges(vols)
    counts, _ = np.histogram(vols, bins=edges)

    n = len(vols)
    s = pd.Series(vols)
    skew = float(s.skew()) if n >= 3 else None
    kurt = float(s.kurt()) if n >= 4 else None
    hartigan = None
    if n > 3 and kurt is not None:
        denom = kurt + 3.0 * (n - 1.0) ** 2 / ((n - 2.0) * (n - 3.0))
        if denom > 0 and skew is not None:
            hartigan = min(float((skew ** 2 + 1.0) / denom), 1.0)

    mode_val, mode_int = _bin_mode_value(edges, counts)
    secondary_picked = _secondary_peaks(counts, edges)
    secondary_modes = [
        {"value": 0.5 * (edges[p] + edges[p + 1]),
         "interval": [float(edges[p]), float(edges[p + 1])],
         "count": int(counts[p])}
        for p in secondary_picked
    ]

    return DistributionStats(
        session_id=session.session_id, n_breaths=len(vols), method="fd",
        units=units, bin_edges=[float(e) for e in edges],
        counts=[int(c) for c in counts], skew=skew, kurtosis=kurt,
        hartigan_bc=hartigan,
        bimodal=(hartigan is not None and hartigan > 5.0 / 9.0
                 and bool(secondary_modes)),
        global_mode=float(mode_val), global_mode_interval=mode_int,
        secondary_modes=secondary_modes, warnings=list(warnings))


def respiratory_rate(session: SessionData, quality: QualityReport,
                     block_minutes: float = 5.0) -> pd.DataFrame:
    """Per-block respiratory-rate estimates (autocorrelation, flow-derived).

    Blocks of ``block_minutes`` aligned to the session start. For each block
    the detrended, QC-valid flow is autocorrelated (FFT); the first strong
    autocorrelation peak in the 0.1-0.5 Hz band (lag 2-10 s, i.e. 6-30 bpm)
    gives the period. A block is ``valid`` only when ≥ 50 % of its samples are
    QC-valid and a strong periodic peak exists; otherwise ``rr_bpm`` is NaN.

    ``rr`` is an *estimate from the flow channel*, not a clinical rate.
    """
    resampled = _analysis_frame(session)
    col = session.channel
    ts = resampled["timestamp"].reset_index(drop=True)
    values = resampled[col].to_numpy(dtype=float)
    mask = valid_mask(ts, quality) & np.isfinite(values)
    period = _period_s(session)
    block_s = block_minutes * 60.0
    n_block = max(5, int(round(block_s / period)))

    rows = []
    start = ts.iloc[0]
    end = ts.iloc[-1]
    cur = start
    while cur <= end:
        nxt = cur + pd.Timedelta(seconds=block_s)
        sel = (ts >= cur) & (ts < nxt)
        if not sel.any():
            cur = nxt
            continue
        blk_ts = ts[sel]
        blk = values[sel]
        blk_mask = mask[sel]
        frac = float(blk_mask.sum()) / len(blk)
        rr = None
        valid = False
        if frac >= RR_MIN_VALID_FRAC:
            x = blk[blk_mask]
            rr = _est_rr(x, period)
            valid = rr is not None
        rows.append({
            "block_start": blk_ts.iloc[0],
            "block_end": blk_ts.iloc[-1],
            "rr_bpm": np.nan if rr is None else rr,
            "valid": bool(valid),
            "valid_frac": round(float(frac), 6),
            "method": "autocorr",
        })
        cur = nxt

    return pd.DataFrame(rows, columns=[
        "block_start", "block_end", "rr_bpm", "valid", "valid_frac", "method"])


def _est_rr(x: np.ndarray, period_s: float) -> Optional[float]:
    """Respiratory period (s) from the flow autocorrelation of a block."""
    min_samples = int(np.ceil(4.0 * (RR_LAG_MAX_S + RR_LAG_MIN_S) / period_s))
    if len(x) < min_samples:
        return None
    x = x - np.median(x)
    n = len(x)
    xz = np.concatenate([x, np.zeros(n)])
    spec = np.fft.rfft(xz)
    ac = np.fft.irfft(spec.real ** 2 + spec.imag ** 2)[:n]
    if ac[0] <= 0 or not np.isfinite(ac[0]):
        return None
    ac = ac / ac[0]
    lags = np.arange(n) * period_s
    lo = int(np.searchsorted(lags, RR_LAG_MIN_S))
    hi = int(np.searchsorted(lags, RR_LAG_MAX_S))
    if hi <= lo + 2:
        return None
    seg = ac[lo:hi]
    for i in range(1, len(seg) - 1):
        if (seg[i] > seg[i - 1] and seg[i] >= seg[i + 1]
                and seg[i] >= RR_MIN_AC):
            return 60.0 / float(lags[lo + i])
    return None


def session_summary(session: SessionData, quality: QualityReport,
                    block_minutes: float = 5.0, rr=None,
                    warnings: Optional[List[str]] = None) -> SessionStats:
    """Summary statistics of one session over its QC-valid signal.

    Usage, flow stats (mean/median/IQR/p05/p95), per-breath modal and p90
    tidal volume, and the respiratory-rate summary are all computed on the
    samples the quality report marks valid; every figure reports the fraction
    it was computed over. Values are raw units (resA scaling unconfirmed).
    """
    if warnings is None:
        warnings = []
    resampled = _analysis_frame(session)
    col = session.channel
    ts = resampled["timestamp"].reset_index(drop=True)
    values = resampled[col].to_numpy(dtype=float)
    mask = valid_mask(ts, quality) & np.isfinite(values)
    period = _period_s(session)

    n_total = len(values)
    n_valid = int(np.count_nonzero(mask))

    total_dur = (session.end - session.start).total_seconds()
    usage_dur = n_valid * period
    night_usage_pct = 100.0 * usage_dur / total_dur if total_dur > 0 else 0.0

    vv = values[mask]
    if len(vv) >= 1:
        q05 = float(np.nanpercentile(vv, 5))
        q25 = float(np.nanpercentile(vv, 25))
        q50 = float(np.nanmedian(vv))
        q75 = float(np.nanpercentile(vv, 75))
        q95 = float(np.nanpercentile(vv, 95))
        flow_mean, flow_median = float(np.nanmean(vv)), q50
        flow_iqr = q75 - q25
    else:
        q05 = q25 = q50 = q75 = q95 = None
        flow_mean = flow_median = flow_iqr = None
        warnings.append("no QC-valid samples: flow statistics are undefined")

    vols = _per_breath_volumes(values, mask, period)
    tidal_mode = tidal_p90 = None
    if len(vols):
        tidal_mode = float(_bin_mode_value(
            _fd_bin_edges(vols),
            np.histogram(vols, bins=_fd_bin_edges(vols))[0])[0])
        tidal_p90 = float(np.percentile(vols, 90))
    units = "raw units (uncertain scaling)"

    if rr is None:
        rr = respiratory_rate(session, quality, block_minutes=block_minutes)
    n_blocks = len(rr)
    rrvals = rr.loc[rr["valid"], "rr_bpm"].astype(float)
    if len(rrvals):
        rr_mean = float(rrvals.mean())
        rr_median = float(rrvals.median())
        rr_iqr = float(rrvals.quantile(0.75) - rrvals.quantile(0.25))
    else:
        rr_mean = rr_median = rr_iqr = None
        warnings.append(
            "no valid respiratory-rate estimate in any block")
    rr_coverage = (100.0 * len(rrvals) / n_blocks) if n_blocks else 0.0

    if quality.warnings:
        warnings.extend(quality.warnings)

    return SessionStats(
        session_id=session.session_id, channel=session.channel,
        start=session.start, end=session.end,
        n_total=n_total, n_valid=n_valid,
        signal_valid_pct=float(quality.valid_pct),
        total_duration_s=total_dur, usage_duration_s=usage_dur,
        night_usage_pct=night_usage_pct,
        flow_mean=flow_mean, flow_median=flow_median, flow_iqr=flow_iqr,
        flow_p05=q05, flow_p95=q95, n_breaths=len(vols),
        tidal_mode=tidal_mode, tidal_p90=tidal_p90, tidal_units=units,
        rr_mean=rr_mean, rr_median=rr_median, rr_iqr=rr_iqr,
        n_blocks=n_blocks, n_blocks_valid=int(len(rrvals)),
        rr_coverage_pct=rr_coverage, warnings=warnings)


def nightly_trend_summary(sessions: Iterable[SessionData],
                          reports: Mapping[int, QualityReport]) -> pd.DataFrame:
    """One row per session night, summed over sessions of an export.

    ``sessions`` is an iterable of :class:`SessionData`; ``reports`` maps
    ``session_id`` to its persisted :class:`QualityReport`. A missing report
    raises ``ValueError`` naming the sessions to run ``quality.py preprocess``
    on first. Returns the trend frame (also written by the CLI as
    ``reports/nightly_trend.csv``) for the time-series / anomaly layers.
    """
    rows = []
    missing = [s.session_id for s in sessions if s.session_id not in reports]
    if missing:
        raise ValueError(
            "no QualityReport for session(s) {}: run quality.py preprocess "
            "first".format(sorted(missing)))
    summaries = [
        session_summary(s, reports[s.session_id]) for s in sessions
    ]
    for st in summaries:
        rows.append({
            "session_id": st.session_id,
            "date": st.start.date().isoformat(),
            "total_duration_s": round(st.total_duration_s, 3),
            "usage_duration_s": round(st.usage_duration_s, 3),
            "night_usage_pct": round(st.night_usage_pct, 3),
            "signal_valid_pct": round(st.signal_valid_pct, 3),
            "flow_median": st.flow_median,
            "tidal_mode": st.tidal_mode,
            "rr_mean": st.rr_mean,
            "rr_iqr": st.rr_iqr,
            "n_blocks_valid": st.n_blocks_valid,
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


def plot_volume_distribution(dist: DistributionStats, session: SessionData,
                             quality: QualityReport):
    """Histogram of per-breath volumes annotated with the detected modes.

    Needs matplotlib (lazy import, mirrors ``plotting.py``). The plot shows the
    mode and secondary modes and, in the subtitle, the QC validity the data was
    computed over. Returns the ``(figure, axes)`` pair.
    """
    fig, ax = _pyplot().subplots(figsize=(10, 5))
    if len(dist.bin_edges) and len(dist.counts):
        edges = np.asarray(dist.bin_edges)
        widths = np.diff(edges)
        ax.bar(edges[:-1], dist.counts, width=widths, align="edge",
               color="tab:blue", alpha=0.75, edgecolor="white", linewidth=0.2)
        for m in [dist.global_mode] + [s["value"] for s in dist.secondary_modes]:
            if m is not None:
                ax.axvline(m, color="tab:red", linestyle="--", linewidth=1.0)
    else:
        ax.text(0.5, 0.5, "no breath cycles in the QC-valid signal",
                transform=ax.transAxes, ha="center", va="center")
    title = "Per-breath volume distribution - session {}".format(
        session.session_id)
    ax.set_title(title)
    sub = ("valid {:.1f}% | n = {} breaths | raw units | "
           "mode {:.2g}".format(quality.valid_pct, dist.n_breaths,
                                dist.global_mode if dist.global_mode is not None
                                else float("nan")))
    ax.text(0.01, 0.99, sub, transform=ax.transAxes, va="top", fontsize=9,
            color="0.4")
    ax.set_xlabel("Per-breath volume (raw units)")
    ax.set_ylabel("Count")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig, ax