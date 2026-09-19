"""Analysis helpers for RESmart data, built on top of preprocess.py.

The device records one packet per second while it is powered on (a night of
use) and nothing while it is off (the daytime). A "session" is therefore a
contiguous block of use, isolated by large gaps: the time difference between
consecutive rows is seconds within the same night and hours between nights.

Requires pandas (same approved exception as preprocess.py). Not for medical
use.
"""

import argparse

import pandas as pd

from resmart.preprocess import clean_and_preprocess


def segment_sessions(df, limit_hours=4):
    """Assign each row a numeric ``session_id`` for its night of use.

    The input must be the output of ``clean_and_preprocess``: a DataFrame
    whose ``timestamp`` column is a datetime64 series sorted ascending. If it
    is not chronologically sorted, interval calculation would be meaningless,
    so a ValueError is raised up front.

    Algorithm:
      1. ``gap = timestamp.diff()`` — the timedelta between each row and the
         previous one (the first row has no predecessor and is treated as a
         session start);
      2. a new session starts wherever ``gap > limit_hours`` (a long daytime
         off-window);
      3. the boolean marker (True = 1) is cumulatively summed, so every True
         increments the identifier and all rows in between share it.

    Sessions are numbered starting at 1. Small gaps (the duplicate same-second
    rows kept at the circular-wrap overlap, or a few missing seconds inside a
    night) do not start a new session. ``limit_hours=0`` degenerates to one
    session per row and is allowed but useless.

    Returns the input DataFrame with an added int ``session_id`` column.
    """
    if df.empty:
        df["session_id"] = pd.Series(dtype="int64")
        return df

    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError(
            "timestamp must be chronologically sorted: "
            "run clean_and_preprocess() on this frame first"
        )

    gap = df["timestamp"].diff()
    df["session_id"] = (gap > pd.Timedelta(hours=limit_hours)).cumsum().astype("int64") + 1
    return df


def build_parser():
    parser = argparse.ArgumentParser(
        description="Segment a RESmart CSV into sessions (nights of use).",
    )
    parser.add_argument("input", help="CSV export from resmart_parse.py")
    parser.add_argument("limit_hours", type=float, nargs="?", default=4,
                        help="a time gap longer than this between consecutive "
                             "packets starts a new session (default: 4 h)")
    parser.add_argument("-o", "--output", default=None,
                        help="also write the segmented DataFrame to a CSV "
                             "(with the session_id column) so a later step "
                             "can plot it, e.g. python -m resmart plot")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    df = segment_sessions(
        clean_and_preprocess(args.input), limit_hours=args.limit_hours
    )
    rows = df.groupby("session_id").size()
    span = df.groupby("session_id")["timestamp"].agg(["min", "max"])
    merged = pd.concat([rows.rename("rows"), span], axis=1)
    merged["duration"] = merged["max"] - merged["min"]
    for sid, r in merged.iterrows():
        print(f"session {sid:>4d}: {r['min']:%Y-%m-%d %H:%M} -> "
              f"{r['max']:%H:%M}  {r['rows']:,} s  ({r['duration']})")
    print(f"{len(merged)} session(s), {len(df):,} rows total")
    if args.output is not None:
        df.to_csv(args.output, index=False)
        print("wrote {}".format(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())