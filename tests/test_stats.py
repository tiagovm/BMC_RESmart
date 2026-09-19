"""Tests for the Layer-2 statistics (stats.py): per-session summary, volume
distribution, respiratory-rate estimation, nightly trend and CLI, using
synthetic breathing-like signals.

Not for medical use. Run with:  pytest  (from the repository root).
"""

import json

import numpy as np
import pandas as pd
import pytest

import stats
from quality import (
    QualityReport,
    SessionData,
    SuspiciousInterval,
    load_session,
    write_quality_report,
)


def _clean_report(session_id=1, channel="resA", n=0, valid_pct=100.0):
    return QualityReport(
        session_id=session_id, channel=channel, sample_rate_hz=25.0,
        total_samples=n, valid_samples=int(n * valid_pct / 100.0),
        valid_pct=valid_pct, intervals=[], counts_by_kind={},
        warnings=[], params={},
    )


def _make_session(ts, values, session_id=1, channel="resA", period_s=0.04):
    return SessionData(
        session_id=session_id, channel=channel,
        timestamp=ts, values=pd.Series(values, dtype="float64"),
        start=ts[0], end=ts[-1], sample_rate_hz=1.0 / period_s,
        measured_period_s=period_s, n=len(ts),
    )


def _breathing(n, period_s, rate_bpm, amp=1.0, base=5.0, seed=0, noise=0.02):
    """A breathing-like (sine) flow trace with noise. Rate bpm -> 0.25 Hz @15."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) * period_s
    flow = amp * np.sin(2 * np.pi * t * rate_bpm / 60.0)
    return base + flow + noise * rng.normal(size=n)


def _timestamps(start="2026-09-17 22:30:00", n=7500, period_s=0.04):
    return pd.date_range(start, periods=n, freq=f"{period_s}s")


def _write_segmented_csv(path, session_id=1, values=None, n=7500,
                         period_s=0.04):
    ts = _timestamps(n=n, period_s=period_s)
    if values is None:
        values = _breathing(n, period_s, 15.0)
    df = pd.DataFrame({"timestamp": ts, "session_id": session_id,
                       "resA": values})
    df.to_csv(path, index=False)
    return ts, values


def test_session_summary_clean_sine():
    period = 0.04
    ts = _timestamps(n=7500, period_s=period)
    vals = _breathing(7500, period, 15.0)
    session = _make_session(ts, vals, session_id=7)
    summary = stats.session_summary(session, _clean_report(7))
    assert summary.session_id == 7
    assert summary.signal_valid_pct == pytest.approx(100.0)
    assert summary.n_total == 7500 and summary.n_valid == 7500
    assert summary.night_usage_pct == pytest.approx(100.0, abs=1.0)
    assert summary.flow_median == pytest.approx(5.0, abs=0.2)
    assert 0.5 < summary.flow_iqr < 2.0
    assert summary.flow_p05 is not None and summary.flow_p95 is not None
    assert summary.n_breaths >= 60  # 15 bpm * 5 min
    assert summary.tidal_mode is not None
    expected_mode = 4.0 / np.pi  # integral of a 1-unit sine positive lobe
    assert 0.5 * expected_mode < summary.tidal_mode < 1.5 * expected_mode
    assert summary.tidal_units == "raw units (uncertain scaling)"
    assert summary.n_blocks == 1 and summary.n_blocks_valid == 1
    assert summary.rr_mean == pytest.approx(15.0, abs=1.0)
    assert summary.rr_coverage_pct == pytest.approx(100.0)


def test_session_summary_usage_gated_by_qc_intervals():
    period = 0.04
    ts = _timestamps(n=7500, period_s=period)
    vals = _breathing(7500, period, 15.0)
    vals[2500:5000] = 5.0  # flat middle chunk
    session = _make_session(ts, vals, session_id=1)
    report = _clean_report(n=7500, valid_pct=100.0 * 5000 / 7500)
    report.intervals = [SuspiciousInterval(ts[2500], ts[4999], "flatline")]
    summary = stats.session_summary(session, report)
    assert summary.n_total == 7500
    assert summary.n_valid == 5000
    assert summary.signal_valid_pct == pytest.approx(100.0 * 5000 / 7500)
    assert summary.usage_duration_s == pytest.approx(5000 * period)
    # the resampled grid spans (n-1)*period -> 7499 intervals, not 7500
    assert summary.night_usage_pct == pytest.approx(
        100.0 * 5000 / 7499)
    assert summary.flow_median == pytest.approx(5.0, abs=0.2)


def test_volume_distribution_unimodal():
    period = 0.04
    ts = _timestamps(n=7500, period_s=period)
    session = _make_session(ts, _breathing(7500, period, 15.0), session_id=1)
    dist = stats.volume_distribution(session, _clean_report(1))
    assert dist.n_breaths >= 60
    assert len(dist.bin_edges) - 1 == len(dist.counts)
    assert dist.skew is not None and dist.hartigan_bc is not None
    assert dist.global_mode is not None
    assert 0.5 * (4.0 / np.pi) < dist.global_mode < 1.5 * (4.0 / np.pi)
    assert dist.bimodal is False
    assert dist.units == "raw units (uncertain scaling)"


def test_volume_distribution_bimodal_flag():
    period = 0.04
    n = 7500
    ts = _timestamps(n=n, period_s=period)
    vals_a = _breathing(n // 2, period, 15.0, amp=1.0, seed=1)
    vals_b = _breathing(n // 2, period, 15.0, amp=6.0, seed=2)
    session = _make_session(ts, np.concatenate([vals_a, vals_b]),
                            session_id=1)
    dist = stats.volume_distribution(session, _clean_report(1))
    assert dist.hartigan_bc is not None and dist.hartigan_bc > 5.0 / 9.0
    assert dist.bimodal is True
    assert dist.secondary_modes
    values = [dist.global_mode] + [m["value"] for m in dist.secondary_modes]
    assert max(values) - min(values) > 3.0  # both volume populations present


def test_respiratory_rate_known_rate():
    period = 0.04
    ts = _timestamps(n=7500, period_s=period)
    session = _make_session(ts, _breathing(7500, period, 15.0), session_id=1)
    rr = stats.respiratory_rate(session, _clean_report(1), block_minutes=5.0)
    assert len(rr) == 1
    row = rr.iloc[0]
    assert bool(row["valid"]) is True
    assert row["valid_frac"] == pytest.approx(1.0)
    assert row["rr_bpm"] == pytest.approx(15.0, abs=1.0)
    assert row["method"] == "autocorr"


def test_respiratory_rate_flat_block_invalid():
    period = 0.04
    ts = _timestamps(n=7500, period_s=period)
    session = _make_session(ts, np.full(7500, 7.0), session_id=1)
    rr = stats.respiratory_rate(session, _clean_report(1), block_minutes=5.0)
    assert len(rr) == 1
    assert not rr.iloc[0]["valid"]
    assert np.isnan(rr.iloc[0]["rr_bpm"])


def test_nightly_trend_aggregates_sessions():
    period = 0.04
    ts1 = _timestamps("2026-09-17 22:30:00", n=7500, period_s=period)
    ts2 = _timestamps("2026-09-18 22:30:00", n=7500, period_s=period)
    s1 = _make_session(ts1, _breathing(7500, period, 15.0), session_id=1)
    s2 = _make_session(ts2, _breathing(7500, period, 18.0), session_id=2)
    df = stats.nightly_trend_summary([s2, s1],  # order must not matter
                                     {1: _clean_report(1), 2: _clean_report(2)})
    assert list(df["session_id"]) == [1, 2]
    assert list(df["date"]) == ["2026-09-17", "2026-09-18"]
    for col in ("night_usage_pct", "signal_valid_pct", "flow_median",
                "tidal_mode", "rr_mean", "rr_iqr", "n_blocks_valid"):
        assert col in df.columns
    assert df.loc[df["session_id"] == 1, "rr_mean"].iloc[0] == \
        pytest.approx(15.0, abs=1.0)


def test_nightly_trend_missing_report_raises():
    period = 0.04
    ts = _timestamps(n=7500, period_s=period)
    s1 = _make_session(ts, _breathing(7500, period, 15.0), session_id=1)
    with pytest.raises(ValueError) as e:
        stats.nightly_trend_summary([s1], {})
    assert "quality.py preprocess first" in str(e.value)


def test_cli_stats_json_smoke(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv, session_id=1)
    qc = tmp_path / "qc.json"
    write_quality_report(_clean_report(1, n=7500), str(qc))
    out = tmp_path / "stats.json"
    rc = stats.main(["stats", "1", "--input", str(csv),
                     "--report-path", str(qc), "--json", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert set(data) == {"session", "volume_distribution", "respiratory_rate"}
    assert data["session"]["session_id"] == 1
    assert data["session"]["channel"] == "resA"
    assert "hartigan_bc" in data["volume_distribution"]
    assert isinstance(data["respiratory_rate"], list)


def test_cli_stats_missing_report_returns_2(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv, session_id=1)
    rc = stats.main(["stats", "1", "--input", str(csv),
                     "--report-path", str(tmp_path / "none.json")])
    assert rc == 2


def test_cli_trend_writes_csv(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv, session_id=1)
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    write_quality_report(_clean_report(1, n=7500),
                         str(reports_dir / "qc_session_1_2026-09-17.json"))
    out = tmp_path / "trend.csv"
    rc = stats.main(["trend", "--input", str(csv),
                     "--report-dir", str(reports_dir), "--output", str(out)])
    assert rc == 0
    assert out.exists()
    df = pd.read_csv(out)
    assert list(df["session_id"]) == [1]
    assert df.loc[0, "n_blocks_valid"] == 1


def test_cli_trend_missing_report_returns_2(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv, session_id=1)
    rc = stats.main(["trend", "--input", str(csv),
                     "--report-dir", str(tmp_path / "empty")])
    assert rc == 2