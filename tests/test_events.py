"""Tests for the Layer-3 respiratory events and timeline (events.py), using
synthetic breathing-like signals with planted amplitude dips and dead
stretches.

Not for medical use. Run with:  pytest  (from the repository root).
"""

import json

import numpy as np
import pandas as pd
import pytest

import events
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


def _timestamps(start="2026-09-17 22:30:00", n=7500, period_s=0.04):
    return pd.date_range(start, periods=n, freq="{}s".format(period_s))


def _breathing_60(n, period_s=0.04, amp=5.0, seed=0, noise=0.0):
    """A breathing-like trace at exactly 60 bpm (1 Hz).

    One full sine cycle per second aligns the per-second amplitude envelope
    with a constant amp/sqrt(2) (about 3.54 for amp=5), so planted dips are
    detected with stable, predictable boundaries.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n) * period_s
    flow = amp * np.sin(2 * np.pi * t)
    return flow + noise * rng.normal(size=n)


def _with_dips(values, period_s, dips, factor=0.1, seed=0, noise=0.0):
    """Scale a breathing trace by ``factor`` over integer-second half-open
    intervals ``dips`` (list of (start_s, end_s) pairs)."""
    v = values.copy()
    pp = int(round(1.0 / period_s))
    rng = np.random.default_rng(seed)
    for s0, s1 in dips:
        i0, i1 = s0 * pp, s1 * pp
        v[i0:i1] = v[i0:i1] * factor + noise * rng.normal(size=i1 - i0)
    return v


def _with_dead(values, period_s, dead, noise=0.005, seed=0):
    """Zero the trace (tiny noise) over integer-second intervals ``dead``."""
    v = values.copy()
    pp = int(round(1.0 / period_s))
    rng = np.random.default_rng(seed)
    for s0, s1 in dead:
        i0, i1 = s0 * pp, s1 * pp
        v[i0:i1] = noise * rng.normal(size=i1 - i0)
    return v


def _write_segmented_csv(path, session_id=1, values=None, n=7500,
                         period_s=0.04):
    ts = _timestamps(n=n, period_s=period_s)
    if values is None:
        values = _breathing_60(n, period_s)
    df = pd.DataFrame({"timestamp": ts, "session_id": session_id,
                       "resA": values})
    df.to_csv(path, index=False)
    return ts, values


def test_detect_known_dips():
    period = 0.04
    n = 7500  # 300 s of 60 bpm breathing
    ts = _timestamps(n=n, period_s=period)
    values = _with_dips(_breathing_60(n, period), period,
                        [(60, 75), (120, 135), (210, 225)])
    session = _make_session(ts, values, session_id=1)
    quality = _clean_report(session_id=1, n=n)

    removals, thr, _ = events.detect_mask_removal(session, quality)
    assert removals == []          # normal breathing is not mask removal
    assert thr > 0

    evs = events.detect_flow_limitation(session, quality)
    assert len(evs) == 3
    expect = {60.0: "apnea", 120.0: "apnea", 210.0: "apnea"}
    for e in evs:
        off = (e.start - ts[0]).total_seconds()
        assert abs(off - expect_for(expect, off)) <= 3.0
        assert 10.0 <= e.duration_s <= 20.0
        assert e.reduction == pytest.approx(0.90, abs=0.03)
        assert e.event_type == "apnea"
        assert e.quality_flag == "ok"
        assert e.confidence == "high"


def expect_for(expect, off):
    return min(expect, key=lambda k: abs(k - off))


def test_short_dips_ignored():
    period = 0.04
    n = 7500
    ts = _timestamps(n=n, period_s=period)
    values = _with_dips(_breathing_60(n, period), period, [(60, 64)])
    session = _make_session(ts, values, session_id=1)
    evs = events.detect_flow_limitation(
        session, _clean_report(session_id=1, n=n))
    assert evs == []  # 4 s < 10 s minimum


def test_hypopnea_classification():
    period = 0.04
    n = 7500
    ts = _timestamps(n=n, period_s=period)
    values = _with_dips(_breathing_60(n, period), period, [(60, 75)],
                        factor=0.45)
    session = _make_session(ts, values, session_id=1)
    evs = events.detect_flow_limitation(session, _clean_report(session_id=1))
    assert len(evs) == 1
    e = evs[0]
    assert e.event_type == "hypopnea"
    assert e.reduction == pytest.approx(0.55, abs=0.05)
    assert e.confidence == "high"


def test_qc_overlap_flags_suspect():
    period = 0.04
    n = 7500
    ts = _timestamps(n=n, period_s=period)
    values = _with_dips(_breathing_60(n, period), period, [(60, 75)])
    session = _make_session(ts, values, session_id=1)
    quality = _clean_report(session_id=1, n=n)
    quality.intervals = [SuspiciousInterval(ts[25 * 65], ts[25 * 70],
                                            "flatline")]
    evs = events.detect_flow_limitation(session, quality)
    assert len(evs) == 1
    assert evs[0].quality_flag == "suspect"
    assert evs[0].confidence == "low"


def test_mask_removal_detected_and_excluded():
    period = 0.04
    n = 19500  # 780 s: 300 breathing + 180 dead + 300 breathing
    ts = _timestamps(n=n, period_s=period)
    values = _with_dead(_breathing_60(n, period), period, [(300, 480)])
    session = _make_session(ts, values, session_id=1)
    quality = _clean_report(session_id=1, n=n)

    removals, thr, hours = events.detect_mask_removal(session, quality)
    assert len(removals) == 1
    off = (removals[0].start - ts[0]).total_seconds()
    assert abs(off - 300.0) <= 3.0
    assert 170.0 <= removals[0].duration_s <= 190.0
    assert 0.5 < thr < 1.0          # ~20 % of the median activity
    assert hours == pytest.approx(180.0 / 3600.0, abs=0.01)

    ex = [(r.start, r.end) for r in removals]
    evs = events.detect_flow_limitation(session, quality,
                                        exclude_intervals=ex)
    assert evs == []                # no false apneas while the mask is off


def test_dead_stretch_caught_as_apnea_without_mask_length():
    # A 90-s dead stretch is below the 2-min mask threshold, so the detector
    # itself must call it a prolonged apnea-family event.
    period = 0.04
    n = 17250  # 690 s: 300 breathing + 90 dead + 300 breathing
    ts = _timestamps(n=n, period_s=period)
    values = _with_dead(_breathing_60(n, period), period, [(300, 390)])
    session = _make_session(ts, values, session_id=1)
    quality = _clean_report(session_id=1, n=n)

    removals, _, _ = events.detect_mask_removal(session, quality)
    assert removals == []
    evs = events.detect_flow_limitation(session, quality)
    assert len(evs) == 1
    assert evs[0].event_type == "apnea"
    assert 80.0 <= evs[0].duration_s <= 100.0


def test_midnight_crossing_hour_bands():
    period = 0.04
    n = 180000  # 120 min starting 23:00 -> crosses midnight
    ts = _timestamps(start="2026-09-17 23:00:00", n=n, period_s=period)
    values = _with_dips(_breathing_60(n, period), period,
                        [(600, 615), (3900, 3915), (5700, 5715)])
    session = _make_session(ts, values, session_id=1)
    evs = events.detect_flow_limitation(session, _clean_report(session_id=1))
    assert len(evs) == 3
    df = events.event_timeline(session, evs)
    assert list(df["start"]) == sorted(df["start"])
    dates = [pd.Timestamp(t).date().isoformat() for t in df["start"]]
    assert dates == ["2026-09-17", "2026-09-18", "2026-09-18"]
    assert list(df["hour_of_night"]) == [23, 0, 0]
    assert events.events_by_hour(df) == {"23": 1, "00": 2}


def test_estimate_ahi_usage_based():
    period = 0.04
    n = 15000  # 600 s
    ts = _timestamps(n=n, period_s=period)
    values = _with_dips(_breathing_60(n, period), period,
                        [(60, 75), (120, 135), (180, 195), (480, 495)])
    session = _make_session(ts, values, session_id=1)
    quality = _clean_report(session_id=1, n=n)
    quality.intervals = [
        SuspiciousInterval(ts[25 * 300], ts[25 * 420], "drift"),  # invalid
        SuspiciousInterval(ts[25 * 485], ts[25 * 490], "flatline"),  # suspect
    ]
    evs = events.detect_flow_limitation(session, quality)
    assert len(evs) == 4
    assert [e.quality_flag for e in evs].count("ok") == 3

    ahi = events.estimate_ahi(session, evs, quality)
    usage_h = ahi.usage_hours
    # invalid samples: drift [300,420] = 3001 points (inclusive) + flatline
    # [485,490] = 126 points -> all 4x25-boundary samples are valid boundaries
    assert usage_h == pytest.approx(
        (15000 - 3001 - 126) * period / 3600.0)
    assert ahi.n_events == 4 and ahi.n_events_confident == 3
    assert ahi.estimated_ahi == pytest.approx(4.0 / usage_h, rel=1e-6)
    assert ahi.estimated_ahi_confident == pytest.approx(3.0 / usage_h,
                                                        rel=1e-6)
    # sanity: usage-based, not wall-clock (4 / 0.1667 h would be ~24)
    assert ahi.estimated_ahi > 25.0


def test_estimate_ahi_zero_usage_none():
    period = 0.04
    n = 7500
    ts = _timestamps(n=n, period_s=period)
    values = _with_dips(_breathing_60(n, period), period, [(60, 75)])
    session = _make_session(ts, values, session_id=1)
    quality = _clean_report(session_id=1, n=n)
    quality.intervals = [SuspiciousInterval(ts[0], ts[-1], "flatline")]
    evs = events.detect_flow_limitation(session, quality)
    ahi = events.estimate_ahi(session, evs, quality)
    assert ahi.usage_hours == 0.0
    assert ahi.estimated_ahi is None
    assert ahi.estimated_ahi_confident is None


def test_event_timeline_empty_columns():
    period = 0.04
    ts = _timestamps(n=7500, period_s=period)
    session = _make_session(ts, _breathing_60(7500, period), session_id=1)
    df = events.event_timeline(session, [])
    assert df.empty
    assert list(df.columns) == [
        "start", "end", "duration_s", "reduction", "event_type",
        "quality_flag", "confidence", "hour_of_night"]
    assert events.events_by_hour(df) == {}


def test_cli_events_json_and_csv(tmp_path):
    csv = tmp_path / "seq.csv"
    n = 7500
    ts, values = _write_segmented_csv(csv, session_id=1,
                                      values=_with_dips(
                                          _breathing_60(n), 0.04,
                                          [(60, 75), (120, 135)]))
    rep = tmp_path / "reports"
    rep.mkdir()
    write_quality_report(_clean_report(1, n=n), str(rep / "qc_session_1_2026-09-17.json"))
    out = tmp_path / "events.json"
    rc = events.main(["events", "1", "--input", str(csv),
                      "--report-dir", str(rep), "--json", str(out)])
    assert rc == 0

    timeline_csv = rep / "events_session_1_2026-09-17.csv"
    assert timeline_csv.exists()
    tl = pd.read_csv(timeline_csv)
    assert len(tl) == 2
    assert list(tl.columns) == [
        "start", "end", "duration_s", "reduction", "event_type",
        "quality_flag", "confidence", "hour_of_night"]

    payload = json.loads(out.read_text())
    assert payload["session"]["session_id"] == 1
    assert len(payload["events"]) == 2
    assert payload["ahi"]["estimated_ahi"] == pytest.approx(
        2.0 / (n * 0.04 / 3600.0), rel=1e-4)
    assert payload["events_by_hour"] == {"22": 2}


def test_cli_events_missing_report_returns_2(tmp_path):
    csv = tmp_path / "seq.csv"
    _write_segmented_csv(csv)
    rc = events.main(["events", "1", "--input", str(csv),
                      "--report-path", str(tmp_path / "none.json")])
    assert rc == 2


def test_cli_events_plot_smoke(tmp_path):
    csv = tmp_path / "seq.csv"
    n = 7500
    _write_segmented_csv(csv, session_id=1,
                         values=_with_dips(_breathing_60(n), 0.04,
                                           [(60, 75)]))
    rep = tmp_path / "reports"
    rep.mkdir()
    write_quality_report(_clean_report(1, n=n), str(rep / "qc_session_1_2026-09-17.json"))
    png = tmp_path / "events.png"
    rc = events.main(["events", "1", "--input", str(csv),
                      "--report-dir", str(rep), "--plot", "-o", str(png)])
    assert rc == 0
    assert png.exists()


def test_cli_events_report_two_sessions(tmp_path):
    csv = tmp_path / "seq_multi.csv"
    n = 7500
    ts1 = _timestamps("2026-09-17 22:30:00", n=n)
    ts2 = _timestamps("2026-09-18 22:30:00", n=n)
    df = pd.DataFrame({
        "timestamp": list(ts1) + list(ts2),
        "session_id": [1] * n + [2] * n,
        "resA": list(_breathing_60(n)) + list(_breathing_60(n, seed=3)),
    })
    df.to_csv(csv, index=False)

    rep = tmp_path / "reports"
    rep.mkdir()
    write_quality_report(_clean_report(1, n=n),
                         str(rep / "qc_session_1_2026-09-17.json"))
    write_quality_report(_clean_report(2, n=n),
                         str(rep / "qc_session_2_2026-09-18.json"))
    out = tmp_path / "summary.csv"
    rc = events.main(["events-report", "--input", str(csv),
                      "--report-dir", str(rep), "--output", str(out)])
    assert rc == 0
    summ = pd.read_csv(out)
    assert list(summ["session_id"]) == [1, 2]
    for col in ("usage_hours", "n_events", "n_suspect", "suspect_pct",
                "estimated_ahi", "estimated_ahi_confident",
                "events_by_hour", "n_mask_removals", "mask_removal_hours"):
        assert col in summ.columns
    assert list(summ["n_events"]) == [0, 0]


def test_cli_events_report_missing_report_returns_2(tmp_path):
    csv = tmp_path / "seq.csv"
    _write_segmented_csv(csv, session_id=1)
    rc = events.main(["events-report", "--input", str(csv),
                      "--report-dir", str(tmp_path / "empty")])
    assert rc == 2