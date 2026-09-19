"""Tests for the Layer-1 pipeline (quality.py): load, reconcile, resample,
quality control and night segmentation, using synthetic signals.

Not for medical use. Run with:  pytest  (from the repository root).
"""

import json

import numpy as np
import pandas as pd
import pytest

from analysis import segment_sessions
from preprocess import clean_and_preprocess
from quality import (
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    SessionData,
    detect_signal_quality,
    load_session,
    main,
    read_quality_report,
    reconcile_units,
    resample_signal,
    segment_night,
    write_quality_report,
)


def _timed_series(start, n, period_s, value_fn):
    """Build a timestamp Series with n samples at a fixed period plus noise-free steps."""
    ts = pd.date_range(start, periods=n, freq=pd.Timedelta(seconds=period_s))
    return ts, pd.Series([value_fn(t) for t in range(n)], dtype="float64")


def _write_segmented_csv(path, session_id=1, n=50, period_s=0.04,
                         channel="resA"):
    """Write a minimal segmented CSV: timestamp + session_id + one channel."""
    ts = pd.date_range("2026-09-17 22:30:00", periods=n, freq=f"{period_s}s")
    values = 5.0 + 0.3 * np.sin(np.linspace(0, 6 * np.pi, n))
    df = pd.DataFrame({"timestamp": ts, "session_id": session_id,
                       channel: values})
    df.to_csv(path, index=False)
    return ts, values


def test_load_session_from_segmented_csv(tmp_path):
    csv = tmp_path / "seg.csv"
    ts, values = _write_segmented_csv(csv, session_id=1, n=50, period_s=0.04)
    s = load_session(str(csv), 1, channel="resA")
    assert isinstance(s, SessionData)
    assert s.session_id == 1
    assert s.channel == "resA"
    assert s.n == 50
    assert s.start == ts[0] and s.end == ts[-1]
    assert s.measured_period_s == pytest.approx(0.04)
    assert s.sample_rate_hz == pytest.approx(25.0)
    assert s.values.iloc[0] == pytest.approx(values[0])


def test_load_session_unknown_and_missing_channel(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv, session_id=1, n=10, channel="resA")
    with pytest.raises(ValueError) as e1:
        load_session(str(csv), 2)
    assert "available session ids: [1]" in str(e1.value)
    with pytest.raises(ValueError) as e2:
        load_session(str(csv), 1, channel="resC")
    assert "column 'resC' not found" in str(e2.value)


def test_load_session_from_raw_csv(tmp_path):
    csv = tmp_path / "raw.csv"
    t1 = pd.date_range("2026-09-17 22:00:00", periods=20, freq="40ms")
    t2 = pd.date_range("2026-09-18 06:00:00", periods=20, freq="40ms")
    ts = t1.append(t2)
    df = pd.DataFrame({"timestamp": ts, "usage_day": 1, "IPAP": 13, "EPAP": 13,
                       "resA": 5.0 + 0.1 * np.sin(np.arange(len(ts)))})
    df.to_csv(csv, index=False)
    s1 = load_session(str(csv), 1, channel="resA")
    assert s1.n == 20
    assert s1.end < t2[0]
    with pytest.raises(ValueError) as e:
        load_session(str(csv), 3)
    assert "available session ids: [1, 2]" in str(e.value)


def test_reconcile_units_marks_volume_as_flow():
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-01"]),
        "IPAP (0.5 cmH2O)": [1],
        "spO2_pct (%)": [98],
        "rep_rate (breaths/min)": [14],
        "tidal_vol (L/min)": [300],
        "resA": [264],
    })
    findings = []
    out = reconcile_units(df, findings)
    assert out is df
    messages = " ".join(findings)
    assert "tidal_vol (L/min)" in messages and "uncertain" in messages
    assert any("unknown" in m and "resA" in m for m in findings)


def pseudo_random_signal(n, period_s, amp=5.0, noise=0.03, seed=0):
    rng = np.random.default_rng(seed)
    base = amp + 0.5 * np.sin(np.linspace(0, 6 * np.pi, n))
    return base + noise * rng.normal(size=n)


@pytest.fixture
def clean_frame():
    n = 400
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    values = pseudo_random_signal(n, 0.04)
    return pd.DataFrame({"timestamp": ts, "resA": values})


def test_resample_signal_equal_rate_is_identity(clean_frame):
    out = resample_signal(clean_frame, column="resA")
    assert list(out.columns) == ["timestamp", "orig_timestamp", "resA"]
    assert len(out) == len(clean_frame)
    assert out["timestamp"].is_monotonic_increasing
    assert out["orig_timestamp"].eq(out["timestamp"]).all()
    assert out["resA"].isna().sum() == 0
    assert out["resA"].iloc[0] == pytest.approx(clean_frame["resA"].iloc[0])


def test_resample_signal_downsample_by_median():
    n = 400
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="10ms")
    ramp = np.linspace(0, 1, n)
    df = pd.DataFrame({"timestamp": ts, "resA": ramp})
    out = resample_signal(df, target_hz=25, column="resA")
    assert len(out) == pytest.approx(101, abs=1)
    filled = out["resA"].notna()
    assert filled.sum() == 100
    assert out.loc[filled, "orig_timestamp"].is_monotonic_increasing
    assert out["orig_timestamp"].iloc[0] == ts[0]


def test_resample_signal_keeps_interior_gap_visible():
    t1 = pd.date_range("2026-09-17 22:00:00", periods=25, freq="40ms")
    t2 = pd.date_range("2026-09-17 22:00:02.600", periods=10, freq="40ms")
    ts = t1.append(t2)
    df = pd.DataFrame({"timestamp": ts, "resA": np.ones(len(ts))})
    out = resample_signal(df, column="resA")
    # 2.6 s - 1.0 s = 1.6 s of uncovered grid -> NaN bins left visible.
    assert out["resA"].isna().sum() >= 20


def test_resample_signal_reports_large_gap():
    t1 = pd.date_range("2026-09-17 22:00:00", periods=25, freq="40ms")
    t2 = pd.date_range("2026-09-17 22:00:08.000", periods=10, freq="40ms")
    ts = t1.append(t2)
    df = pd.DataFrame({"timestamp": ts, "resA": np.ones(len(ts))})
    findings = []
    out = resample_signal(df, column="resA", findings=findings)
    assert any("gap > 5 s" in m for m in findings)
    assert out["resA"].isna().sum() > 0


def test_resample_signal_upsamples_keeping_gap_nan():
    t1 = pd.date_range("2026-09-17 22:00:00", periods=25, freq="40ms")
    t2 = pd.date_range("2026-09-17 22:00:08.000", periods=25, freq="40ms")
    ts = t1.append(t2)
    df = pd.DataFrame({"timestamp": ts, "resA": np.ones(len(ts))})
    findings = []
    out = resample_signal(df, target_hz=50, column="resA", findings=findings)
    assert len(out) > 25
    assert out["resA"].isna().sum() > 5
    assert any("gap > 5 s" in m for m in findings)


def test_resample_signal_rejects_bad_method_and_ambiguous_column(clean_frame):
    with pytest.raises(ValueError):
        resample_signal(clean_frame, method="trim")
    two = clean_frame.assign(resB=clean_frame["resA"] * 2)
    with pytest.raises(ValueError):
        resample_signal(two)


def test_detect_clean_signal_all_valid(clean_frame):
    report = detect_signal_quality(clean_frame, session_id=7, channel="resA")
    assert report.session_id == 7
    assert report.channel == "resA"
    assert report.valid_pct > 90.0
    # A handful of single-sample flags at the sharpest curvature points is
    # expected (the ~1 s rolling window straddles a steep local slope); the
    # spike detector must not, however, flag stretches of the signal en masse.
    assert report.counts_by_kind["spike"] < 5


def test_detect_flatline():
    n = 300
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    df = pd.DataFrame({"timestamp": ts, "resA": np.full(n, 5.0)})
    report = detect_signal_quality(df, session_id=1, channel="resA")
    assert report.counts_by_kind["flatline"] > 200
    assert any(iv.kind == "flatline" for iv in report.intervals)


def test_detect_clipping():
    n = 400
    rng = np.random.default_rng(1)
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    values = 5.0 + 0.5 * np.sin(np.linspace(0, 6 * np.pi, n)) \
        + 0.01 * rng.normal(size=n)
    values[100:200] = 10.0
    df = pd.DataFrame({"timestamp": ts, "resA": values})
    report = detect_signal_quality(df, clip_lo=0.0, clip_hi=10.0,
                                   session_id=1, channel="resA")
    assert report.counts_by_kind["clipping"] >= 90
    assert any(iv.kind == "clipping" for iv in report.intervals)


def test_detect_spikes():
    n = 600
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    values = pseudo_random_signal(n, 0.04, seed=3)
    for pos in (120, 240, 480, 590):
        values[pos] += 30.0
    df = pd.DataFrame({"timestamp": ts, "resA": values})
    report = detect_signal_quality(df, session_id=1, channel="resA")
    assert report.counts_by_kind["spike"] >= 1
    spikes = [iv for iv in report.intervals if iv.kind == "spike"]
    assert len(spikes) >= 1


def test_detect_drift():
    n = 7500  # 300 s @ 40 ms: at least the 300 s drift window
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    rng = np.random.default_rng(4)
    values = 5.0 + 0.01 * rng.normal(size=n)
    values[n // 2:] += 2.0
    df = pd.DataFrame({"timestamp": ts, "resA": values})
    report = detect_signal_quality(df, session_id=1, channel="resA")
    assert report.counts_by_kind["drift"] > 500
    assert any(iv.kind == "drift" for iv in report.intervals)


def test_detect_no_clipping_without_explicit_bounds():
    # The extremes of a breathing trace are legitimate peaks/troughs, NOT
    # clipping: without explicit sensor bounds nothing may be flagged.
    n = 400
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    df = pd.DataFrame({"timestamp": ts,
                       "resA": pseudo_random_signal(n, 0.04, amp=5.0)})
    report = detect_signal_quality(df, session_id=1, channel="resA")
    assert report.counts_by_kind["clipping"] == 0


def test_detect_fast_legitimate_ramp_is_not_a_spike():
    # Real breath edges slew 20-40 words/sample as a RUN of consecutive
    # same-direction steps; that is legitimate flow, not impulse noise.
    n = 1000
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    ramp = np.linspace(0, 300, 30) + 50.0  # 10 words/sample over 30 samples
    values = np.full(n, 250.0)
    values[100:130] = ramp
    df = pd.DataFrame({"timestamp": ts, "resA": values})
    report = detect_signal_quality(df, session_id=1, channel="resA")
    assert report.counts_by_kind["spike"] == 0


def test_detect_from_session_data():
    n = 400
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    values = pseudo_random_signal(n, 0.04)
    session = SessionData(
        session_id=3, channel="resB", timestamp=ts, values=pd.Series(values),
        start=ts[0], end=ts[-1], sample_rate_hz=25.0,
        measured_period_s=0.04, n=n)
    report = detect_signal_quality(session)
    assert report.session_id == 3
    assert report.channel == "resB"
    assert report.sample_rate_hz == pytest.approx(25.0)
    assert report.valid_pct > 90.0


def test_detect_rejects_ambiguous_frame_and_empty_signal(clean_frame):
    two = clean_frame.assign(resB=clean_frame["resA"] * 2)
    with pytest.raises(ValueError):
        detect_signal_quality(two)
    empty = clean_frame.assign(resA=np.nan)
    with pytest.raises(ValueError):
        detect_signal_quality(empty)


def test_night_segmentation_active_boundaries():
    n = int(4 * 3600)  # 22:00 -> 02:00 at 1 Hz
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="1s")
    values = np.full(n, 5.0)
    active_mask = ts.to_series().between("2026-09-17 22:00:00",
                                         "2026-09-18 01:00:00")
    values[active_mask.values] += 0.5 * np.sin(
        np.linspace(0, 6 * np.pi * (active_mask.sum() / 120.0),
                    active_mask.sum()))
    df = pd.DataFrame({"timestamp": ts, "resA": values})
    segments = segment_night(df)
    assert len(segments) == 4  # 22-23, 23-00, 00-01, 01-02
    assert [s.active for s in segments] == [True, True, True, False]


def test_night_segmentation_empty_outside_window():
    ts = pd.date_range("2026-09-18 10:00:00", periods=600, freq="1s")
    df = pd.DataFrame({"timestamp": ts, "resA": np.ones(600)})
    assert segment_night(df) == []


def test_report_json_round_trip(tmp_path):
    ts = pd.date_range("2026-09-17 22:00:00", periods=200, freq="40ms")
    df = pd.DataFrame({"timestamp": ts, "resA": pseudo_random_signal(200, 0.04)})
    report = detect_signal_quality(df, session_id=9, channel="resA")
    path = write_quality_report(report, str(tmp_path / "qc.json"))
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["session_id"] == 9
    assert data["channel"] == "resA"
    assert "sample_rate_hz" in data and "valid_pct" in data
    assert "counts_by_kind" in data and "intervals" in data
    for iv in data["intervals"]:
        assert set(iv) == {"start", "end", "kind"}


def test_read_quality_report_round_trip(tmp_path):
    n = 400
    ts = pd.date_range("2026-09-17 22:00:00", periods=n, freq="40ms")
    values = np.asarray(pseudo_random_signal(n, 0.04))
    values[100:200] = 5.0  # guaranteed suspect interval
    df = pd.DataFrame({"timestamp": ts, "resA": values})
    report = detect_signal_quality(df, session_id=9, channel="resA")
    path = write_quality_report(report, str(tmp_path / "qc.json"))
    loaded = read_quality_report(path)
    assert loaded.session_id == report.session_id
    assert loaded.channel == report.channel
    assert loaded.sample_rate_hz == pytest.approx(report.sample_rate_hz)
    assert loaded.valid_pct == pytest.approx(report.valid_pct)
    assert loaded.counts_by_kind == report.counts_by_kind
    assert loaded.warnings == report.warnings
    assert loaded.params == report.params
    assert len(loaded.intervals) == len(report.intervals)
    assert loaded.intervals
    for a, b in zip(loaded.intervals, report.intervals):
        assert a.kind == b.kind
        assert isinstance(a.start, pd.Timestamp) and isinstance(a.end, pd.Timestamp)
        assert a.start == b.start and a.end == b.end


def test_read_quality_report_missing_field_raises_value_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"channel": "resA"}), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        read_quality_report(str(p))
    assert "session_id" in str(e.value)


def test_preprocess_cli_writes_default_report(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv, session_id=1, n=50, period_s=0.04)
    report_dir = tmp_path / "reports"
    rc = main(["preprocess", "1", "--input", str(csv),
               "--report-dir", str(report_dir)])
    assert rc == 0
    default = report_dir / "qc_session_1_2026-09-17.json"
    assert default.exists()
    assert read_quality_report(str(default)).session_id == 1
    explicit = tmp_path / "explicit.json"
    rc = main(["preprocess", "1", "--input", str(csv),
               "--report", str(explicit)])
    assert rc == 0
    assert explicit.exists()
    assert read_quality_report(str(explicit)).session_id == 1