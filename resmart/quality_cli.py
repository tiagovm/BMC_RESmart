"""Layer-1 CLI: ingest, preprocess and quality-control one RESmart session.

Thin command-line wrapper around the :mod:`resmart.quality` core. Prints text
output, writes a JSON QualityReport and optionally a shaded plot.

    python -m resmart quality preprocess <session_id> --input out.csv
        [--channel resA] [--report qc.json | --report-dir reports]
        [--target-hz 25] [--plot] [-o qc.png]

Not for medical use.
"""

import argparse
import logging
import os

import pandas as pd

from resmart.config import CHANNELS, DEFAULT_CHANNEL, REPORT_DIR
from resmart.io import qc_report_path
from resmart.quality import (
    _pyplot,
    detect_signal_quality,
    load_session,
    plot_signal_quality,
    reconcile_units,
    resample_signal,
    segment_night,
    write_quality_report,
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
    p.add_argument("--channel", default=DEFAULT_CHANNEL,
                   choices=list(CHANNELS),
                   help="signal channel to analyze (default: {})".format(
                       DEFAULT_CHANNEL))
    p.add_argument("--report", default=None,
                   help="write the QualityReport JSON to this path (default: "
                        "<report-dir>/qc_session_<id>_<date>.json)")
    p.add_argument("--report-dir", default=REPORT_DIR,
                   help="directory for the default QualityReport JSON "
                        "(default: {}/)".format(REPORT_DIR))
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
            report_path = qc_report_path(
                args.report_dir, session.session_id, session.start)
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