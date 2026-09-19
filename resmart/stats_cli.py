"""Layer-2 CLI: descriptive per-session statistics and the nightly trend.

Thin command-line wrapper around the :mod:`resmart.stats` core. Consumes a
Layer-1 :class:`SessionData` and the persisted JSON :class:`QualityReport`.
"""

import argparse
import json
import os

import numpy as np
import pandas as pd

from resmart.config import CHANNELS, DEFAULT_CHANNEL, REPORT_DIR
from resmart.io import all_session_ids, load_quality_or_error, qc_report_path
from resmart.quality import load_session, read_quality_report
from resmart.stats import (
    _pyplot,
    nightly_trend_summary,
    plot_volume_distribution,
    respiratory_rate,
    session_summary,
    volume_distribution,
)


def _print_stats(session, stats, dist, rr):
    print("session {} | channel {} | {:%Y-%m-%d %H:%M} -> {:%Y-%m-%d %H:%M}".format(
        stats.session_id, stats.channel, stats.start, stats.end))
    print("use: {:.2f} h of {:.2f} h ({:.1f}%) | {:.1f}% of samples valid".format(
        stats.usage_duration_s / 3600.0, stats.total_duration_s / 3600.0,
        stats.night_usage_pct, stats.signal_valid_pct))
    print("flow (raw units): median {:.1f} | mean {:.1f} | IQR {:.1f} | "
          "p05 {:.1f} p95 {:.1f}  ({} valid samples)".format(
              stats.flow_median if stats.flow_median is not None else float("nan"),
              stats.flow_mean if stats.flow_mean is not None else float("nan"),
              stats.flow_iqr if stats.flow_iqr is not None else float("nan"),
              stats.flow_p05 if stats.flow_p05 is not None else float("nan"),
              stats.flow_p95 if stats.flow_p95 is not None else float("nan"),
              stats.n_valid))
    print("tidal volume ({}): mode {:.3g} | p90 {:.3g} | {} breaths | "
          "skew {:.2f} | Hartigan BC {:.3f}".format(
              stats.tidal_units,
              stats.tidal_mode if stats.tidal_mode is not None else float("nan"),
              stats.tidal_p90 if stats.tidal_p90 is not None else float("nan"),
              stats.n_breaths,
              dist.skew if dist.skew is not None else float("nan"),
              dist.hartigan_bc if dist.hartigan_bc is not None else float("nan")))
    if dist.secondary_modes:
        sec = ", ".join("{:.2g}@[{}..{}]".format(s["value"], s["interval"][0],
                                                 s["interval"][1])
                        for s in dist.secondary_modes)
        print("  secondary modes: {}".format(sec))
    print("respiratory rate (flow-derived, autocorr): median {:.2f} bpm | "
          "mean {:.2f} | IQR {:.2f} | {}/{} blocks estimated ({:.1f}%)".format(
              stats.rr_median if stats.rr_median is not None else float("nan"),
              stats.rr_mean if stats.rr_mean is not None else float("nan"),
              stats.rr_iqr if stats.rr_iqr is not None else float("nan"),
              stats.n_blocks_valid, stats.n_blocks, stats.rr_coverage_pct))
    if stats.warnings:
        print("warnings:")
        for w in stats.warnings:
            print("  - {}".format(w))


def _print_trend(df):
    if df.empty:
        print("trend: no sessions")
        return
    cols = ["session_id", "date", "night_usage_pct", "signal_valid_pct",
            "flow_median", "tidal_mode", "rr_mean", "rr_iqr"]
    print("nightly trend ({} nights):".format(len(df)))
    print("  " + "  ".join("{:<16}".format(c) for c in cols))
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if pd.isna(v):
                cells.append("")
            elif isinstance(v, bool):
                cells.append(str(v))
            elif isinstance(v, (int, np.integer)):
                cells.append(str(int(v)))
            elif isinstance(v, float):
                cells.append("{:.3f}".format(v))
            else:
                cells.append(str(v))
        print("  " + "  ".join("{:<16}".format(x) for x in cells))


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Layer 2: descriptive per-session statistics consuming Layer 1 "
            "(SessionData + persisted QualityReport). Subcommands: stats "
            "(detailed summary of one session) and trend (one row per night)."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("stats", help="summary statistics of one session")
    p.add_argument("--input", required=True,
                   help="CSV export from the parser (with -2 for resA) "
                        "or a segmented CSV (with session_id)")
    p.add_argument("session_id", type=int, help="session id to analyze")
    p.add_argument("--channel", default=DEFAULT_CHANNEL,
                   choices=list(CHANNELS),
                   help="signal channel to analyze (default: {})".format(
                       DEFAULT_CHANNEL))
    p.add_argument("--report-path", default=None,
                   help="path to the persisted QualityReport JSON (default: "
                        "<report-dir>/qc_session_<id>_<date>.json)")
    p.add_argument("--report-dir", default=REPORT_DIR,
                   help="directory holding the default QualityReport JSONs "
                        "(default: {}/)".format(REPORT_DIR))
    p.add_argument("--block-minutes", type=float, default=5.0,
                   help="respiratory-rate estimation block length (default: 5)")
    p.add_argument("--json", metavar="PATH", default=None,
                   help="write the full structured result as JSON")
    p.add_argument("--plot", action="store_true",
                   help="also render the annotated volume-distribution plot")
    p.add_argument("-o", "--output", default=None,
                   help="PNG file to write with --plot "
                        "(default: stats_session_<id>_<date>.png next to the "
                        "input)")
    p.add_argument("--show", action="store_true",
                   help="with --plot, display the plot on screen")

    t = sub.add_parser("trend", help="one summary row per session night")
    t.add_argument("--input", required=True,
                   help="CSV export from the parser or a segmented CSV")
    t.add_argument("--all", action="store_true",
                   help="explicitly select every session (default behaviour)")
    t.add_argument("--channel", default=DEFAULT_CHANNEL,
                   choices=list(CHANNELS),
                   help="signal channel to analyze (default: {})".format(
                       DEFAULT_CHANNEL))
    t.add_argument("--report-dir", default=REPORT_DIR,
                   help="directory holding the default QualityReport JSONs "
                        "(default: {}/)".format(REPORT_DIR))
    t.add_argument("--output", default=None,
                   help="CSV file to write (default: reports/nightly_trend.csv)")
    return parser


def _cmd_stats(args) -> int:
    session = load_session(args.input, args.session_id, channel=args.channel)
    report_path = args.report_path or qc_report_path(
        args.report_dir, session.session_id, session.start)
    try:
        report = load_quality_or_error(report_path)
    except ValueError as exc:
        print(str(exc))
        return 2

    rr = respiratory_rate(session, report, block_minutes=args.block_minutes)
    stats = session_summary(session, report, block_minutes=args.block_minutes,
                            rr=rr)
    dist = volume_distribution(session, report, warnings=list(stats.warnings))

    print()
    _print_stats(session, stats, dist, rr)

    if args.json:
        payload = {
            "session": stats.to_dict(),
            "volume_distribution": dist.to_dict(),
            "respiratory_rate": [
                {k: (v.isoformat() if isinstance(v, pd.Timestamp) else v)
                 for k, v in row.items()}
                for row in rr.to_dict(orient="records")
            ],
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print()
        print("wrote {}".format(args.json))

    if args.plot:
        if not args.show:
            import matplotlib
            matplotlib.use("Agg")
        fig, _ = plot_volume_distribution(dist, session, report)
        if args.show:
            _pyplot().show()
            return 0
        out = args.output or os.path.join(
            os.path.dirname(os.path.abspath(args.input)),
            "stats_session_{0}_{1:%Y-%m-%d}.png".format(
                session.session_id, session.start))
        fig.savefig(out, dpi=110)
        print("wrote {}".format(out))
    return 0


def _cmd_trend(args) -> int:
    ids = all_session_ids(args.input)
    reports = {}
    missing = []
    sessions = []
    for sid in ids:
        try:
            session = load_session(args.input, sid, channel=args.channel)
        except ValueError as exc:
            print(str(exc))
            return 2
        path = qc_report_path(args.report_dir, session.session_id, session.start)
        if not os.path.exists(path):
            missing.append((sid, path))
            continue
        sessions.append(session)
        reports[sid] = read_quality_report(path)
    if missing:
        for sid, path in missing:
            print("no QualityReport at {}: run quality.py preprocess for "
                  "session {} first".format(path, sid))
        return 2

    df = nightly_trend_summary(sessions, reports)
    print()
    _print_trend(df)

    out = args.output or os.path.join(args.report_dir, "nightly_trend.csv")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    df.to_csv(out, index=False)
    print()
    print("wrote {}".format(out))
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "stats":
        return _cmd_stats(args)
    if args.command == "trend":
        return _cmd_trend(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())