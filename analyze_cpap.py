"""CLI that chains the analysis workflow into a single command.

Reads a RESmart CSV export, cleans and chronologically sorts it, segments it
into nights of use (sessions), selects one session and plots that night's
IPAP/EPAP pressure curve — or overlays the last N nights' IPAP curves on a
time axis relative to each session's start. Requires pandas and matplotlib
(approved exceptions). Not for medical use.

Examples:
    python analyze_cpap.py -i out.csv --session 3
    python analyze_cpap.py -i out.csv --overlay 10
    python analyze_cpap.py -i out.csv --overlay 10 --overlay-epap
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
            "Analyze RESmart CPAP data and plot IPAP/EPAP pressure curves, "
            "either for a single night or overlapped for the last N nights."
        )
    )
    parser.add_argument("-i", "--input", required=True,
                        help="CSV export from resmart_parse.py")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--session", type=int, default=None,
                        help="session_id to plot (default: most recent session)")
    target.add_argument("--overlay", type=int, default=None,
                        help="overlay the last N sessions on a relative "
                             "hours-since-start time axis")
    parser.add_argument("--overlay-epap", action="store_true",
                        help="with --overlay, draw the EPAP curves as well")
    parser.add_argument("--wave", default=None,
                        choices=["resA", "resB", "resC", "pulse"],
                        help="plot a high-rate waveform channel of the "
                             "selected session instead of the pressure "
                             "curves (input CSV must have been exported "
                             "with -2 or -1)")
    parser.add_argument("-o", "--output", default=None,
                        help="PNG file to write "
                             "(default: pressure_session_<id>_<date>.png, "
                             "overlapped_sessions_last_<N>.png or "
                             "wave_<channel>_session_<id>_<date>.png next "
                             "to the input)")
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
    from plotting import plot_overlapped_sessions, plot_pressure_curve, plot_waveform

    df = segment_sessions(
        clean_and_preprocess(args.input), limit_hours=args.limit_hours
    )

    session_ids = sorted(df["session_id"].unique())
    if not session_ids:
        print("no data found in {}".format(args.input), file=sys.stderr)
        return 1

    if args.overlay:
        fig, _ = plot_overlapped_sessions(
            df, num_sessions=args.overlay, include_epap=args.overlay_epap)
        sids = sorted(df["session_id"].unique())[-args.overlay:]
        printed = ", ".join("{} ({:%b %d})".format(
            s, df[df["session_id"] == s]["timestamp"].iloc[0]) for s in sids)
        print("overlaying session ids: {}".format(printed))
        if args.show:
            plt.show()
            return 0
        if args.output is None:
            out = os.path.join(
                os.path.dirname(os.path.abspath(args.input)),
                "overlapped_sessions_last_{:d}.png".format(len(sids)))
        else:
            out = args.output
        fig.savefig(out, dpi=110)
        print("wrote {}".format(out))
        return 0

    session_ids = sorted(df["session_id"].unique())
    if args.session is None:
        sid = session_ids[-1]
    elif args.session in session_ids:
        sid = args.session
    else:
        print("session {} not found; available session ids: {}".format(
            args.session, session_ids), file=sys.stderr)
        return 1

    session_df = df[df["session_id"] == sid]

    if args.wave:
        fig, _ = plot_waveform(session_df, channel=args.wave)
        default_name = "wave_{0}_session_{1}_{2:%Y-%m-%d}.png".format(
            args.wave, sid, session_df["timestamp"].iloc[0])
    else:
        fig, _ = plot_pressure_curve(session_df)
        default_name = "pressure_session_{0}_{1:%Y-%m-%d}.png".format(
            sid, session_df["timestamp"].iloc[0])

    start = session_df["timestamp"].iloc[0]
    end = session_df["timestamp"].iloc[-1]
    print("session {}: {:%Y-%m-%d %H:%M} -> {:%H:%M} ({} rows)".format(
        sid, start, end, len(session_df)))

    if args.show:
        plt.show()
        return 0

    if args.output is None:
        out = os.path.join(os.path.dirname(os.path.abspath(args.input)), default_name)
    else:
        out = args.output
    fig.savefig(out, dpi=110)
    print("wrote {}".format(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())