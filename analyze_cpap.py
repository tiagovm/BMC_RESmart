"""CLI that chains the analysis workflow into a single command.

Reads a RESmart CSV export, cleans and chronologically sorts it, segments it
into nights of use (sessions), selects one session and plots that night's
IPAP/EPAP pressure curve. Requires pandas and matplotlib (approved
exceptions). Not for medical use.

Examples:
    python analyze_cpap.py -i out.csv --session 3
    python analyze_cpap.py -i out.csv --show
    python analyze_cpap.py -i out.csv -o night.png --limit-hours 6
"""

import argparse
import os
import sys

import matplotlib

from analysis import segment_sessions
from preprocess import clean_and_preprocess


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Analyze RESmart CPAP data and plot one night's IPAP/EPAP "
            "pressure curve."
        )
    )
    parser.add_argument("-i", "--input", required=True,
                        help="CSV export from resmart_parse.py")
    parser.add_argument("--session", type=int, default=None,
                        help="session_id to plot (default: most recent session)")
    parser.add_argument("-o", "--output", default=None,
                        help="PNG file to write "
                             "(default: pressure_session_<id>_<date>.png "
                             "next to the input)")
    parser.add_argument("--show", action="store_true",
                        help="display the plot on screen instead of writing a file")
    parser.add_argument("--limit-hours", type=float, default=4,
                        help="a time gap longer than this between consecutive "
                             "packets starts a new session (default: 4 h)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if not args.show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from plotting import plot_pressure_curve

    df = segment_sessions(
        clean_and_preprocess(args.input), limit_hours=args.limit_hours
    )

    session_ids = sorted(df["session_id"].unique())
    if not session_ids:
        print("no data found in {}".format(args.input), file=sys.stderr)
        return 1
    if args.session is None:
        sid = session_ids[-1]
    elif args.session in session_ids:
        sid = args.session
    else:
        print("session {} not found; available session ids: {}".format(
            args.session, session_ids), file=sys.stderr)
        return 1

    session_df = df[df["session_id"] == sid]

    fig, _ = plot_pressure_curve(session_df)

    start = session_df["timestamp"].iloc[0]
    end = session_df["timestamp"].iloc[-1]
    print("session {}: {:%Y-%m-%d %H:%M} -> {:%H:%M} ({} rows)".format(
        sid, start, end, len(session_df)))

    if args.show:
        plt.show()
        return 0

    if args.output is None:
        out = os.path.join(os.path.dirname(os.path.abspath(args.input)),
                           "pressure_session_{0}_{1:%Y-%m-%d}.png".format(sid, start))
    else:
        out = args.output
    fig.savefig(out, dpi=110)
    print("wrote {}".format(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())