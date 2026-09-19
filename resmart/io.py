"""Shared I/O helpers for the resmart package.

Central home for the loaders and path helpers that were previously duplicated
across ``quality.py``, ``stats.py``, ``events.py`` and ``plotting.py``:
reading the cleaned/segmented CSV, discovering its session ids, and resolving
or loading the persisted JSON :class:`QualityReport`.

This module never imports other package modules at import time (the one
lazy import avoids a ``quality <-> io`` cycle): ``read_quality_report`` is
imported inside :func:`load_quality_or_error`.
"""

import os
from typing import List

import pandas as pd

from resmart.analysis import segment_sessions
from resmart.preprocess import clean_and_preprocess


def read_segmented_csv(path):
    """Read the step-5 output (a segmented CSV) back into a DataFrame.

    The file must be the cleaned, chronologically sorted frame with the
    ``session_id`` column added by :func:`segment_sessions`. Column names are
    stripped defensively. Raises a ``ValueError`` with a hint when the file
    lacks ``session_id`` or the timestamps are not in order.
    """
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df.columns = [c.strip() for c in df.columns]
    if "session_id" not in df.columns:
        raise ValueError(
            "column 'session_id' not found in {}: run "
            "clean_and_preprocess() and segment_sessions() first so the "
            "frame carries the sessions".format(path)
        )
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError(
            "timestamp must be chronologically sorted: run "
            "clean_and_preprocess() and segment_sessions() first"
        )
    return df


def _csv_has_session_id(input_path: str) -> bool:
    header = pd.read_csv(input_path, nrows=0).columns
    return "session_id" in [c.strip() for c in header]


def all_session_ids(input_path: str) -> List[int]:
    """Return the sorted session ids found in a CSV export.

    Works whether the input is a raw parser export (cleaned and segmented on
    the fly) or the already segmented step-5 output.
    """
    if _csv_has_session_id(input_path):
        frame = read_segmented_csv(input_path)
    else:
        frame = segment_sessions(clean_and_preprocess(input_path))
    return sorted(int(s) for s in frame["session_id"].unique())


def qc_report_path(report_dir: str, session_id: int, start: pd.Timestamp) -> str:
    """Default path of a session's persisted QualityReport JSON."""
    return os.path.join(
        report_dir, "qc_session_{0}_{1:%Y-%m-%d}.json".format(
            session_id, start))


def load_quality_or_error(report_path: str):
    """Load a persisted QualityReport or fail with an actionable error.

    Raises ``ValueError`` (with a message pointing at the Layer-1 command)
    when the file does not exist.
    """
    if not os.path.exists(report_path):
        raise ValueError(
            "no QualityReport at {}: run quality.py preprocess for this "
            "session first".format(report_path))
    from resmart.quality import read_quality_report
    return read_quality_report(report_path)