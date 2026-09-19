"""Tests for the consolidated CLI (python -m resmart) and the one-shot
workflow (resmart.workflow.run_full_pipeline), using a synthetic segmented
CSV.

Not for medical use. Run with:  pytest  (from the repository root).
"""

import numpy as np
import pandas as pd
import pytest

from resmart import cli
from resmart import workflow


def _breathing(n, period_s=0.04, rate_bpm=15.0):
    t = np.arange(n) * period_s
    envelope = 1.0 + 0.35 * np.sin(2 * np.pi * 0.02 * t + 1.0)
    return 200.0 + 40.0 * envelope * np.sin(2 * np.pi * rate_bpm / 60.0 * t)


def _write_segmented_csv(path, n=5000, period_s=0.04):
    def ts_for(start):
        return pd.date_range(start, periods=n, freq="{:.0f}ms".format(period_s * 1000))
    rows = []
    for sid, start in enumerate((1, 2), start=1):
        start_dt = "2026-09-17 22:30:00" if sid == 1 else "2026-09-18 22:30:00"
        ts = ts_for(start_dt)
        rows.append(pd.DataFrame({
            "timestamp": ts, "session_id": sid, "resA": _breathing(n, period_s),
        }))
    df = pd.concat(rows, ignore_index=True)
    df.to_csv(path, index=False)
    return df


def test_consolidated_no_args_returns_2():
    assert cli.main([]) == 2


def test_consolidated_unknown_step_returns_2():
    assert cli.main(["bogus-step"]) == 2


def test_consolidated_forwarding_preprocess_no_args(capsys):
    assert cli.main(["preprocess"]) == 2
    err = capsys.readouterr().err
    assert "python -m resmart preprocess" in err


def test_consolidated_parse_no_args_help_exits_2():
    with pytest.raises(SystemExit) as exc:
        cli.main(["parse"])
    assert exc.value.code == 2


def test_pipeline_unknown_steps_returns_2(capsys):
    rc = cli.main(["pipeline", "--input", "x.csv", "--steps", "bogus"])
    assert rc == 2
    assert "unknown step" in capsys.readouterr().err


def test_workflow_run_full_pipeline_smoke(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv)
    report_dir = tmp_path / "reports"

    result = workflow.run_full_pipeline(str(csv), report_dir=str(report_dir))

    assert result.session_ids == [1, 2]
    assert len(result.by_session) == 2
    assert result.nightly_trend is not None and len(result.nightly_trend) == 2
    assert result.event_summary is not None and len(result.event_summary) == 2

    a = result.by_session[1]
    assert a.quality is not None and a.quality.session_id == 1
    assert a.stats is not None and a.stats.session_id == 1
    assert a.ahi is not None and a.ahi.usage_hours > 0
    assert a.timeline is not None
    assert {"start", "end", "event_type", "hour_of_night"} <= set(a.timeline.columns)
    assert set(a.artifact_paths) == {"quality", "events"}

    artifacts = set(result.artifacts)
    for sid in (1, 2):
        assert list(report_dir.glob("qc_session_{}_*.json".format(sid)))
        assert list(report_dir.glob("events_session_{}_*.csv".format(sid)))
    assert (report_dir / "nightly_trend.csv").exists()
    assert (report_dir / "events_summary.csv").exists()
    assert len(artifacts) == 6


def test_pipeline_cli_smoke(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv)
    report_dir = tmp_path / "pipeline"
    rc = cli.main(["pipeline", "--input", str(csv), "--report-dir", str(report_dir)])
    assert rc == 0
    assert (report_dir / "nightly_trend.csv").exists()
    assert (report_dir / "events_summary.csv").exists()


def test_workflow_subset_steps(tmp_path):
    csv = tmp_path / "seg.csv"
    _write_segmented_csv(csv)
    report_dir = tmp_path / "reports"

    result = workflow.run_full_pipeline(
        str(csv), report_dir=str(report_dir), run_quality=True,
        run_stats=True, run_events=False)

    assert result.nightly_trend is not None
    assert result.event_summary is None
    for sid in (1, 2):
        r = result.by_session[sid]
        assert r.quality is not None and r.stats is not None
        assert r.ahi is None and r.events == []
    assert not (report_dir / "events_summary.csv").exists()