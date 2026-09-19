"""Layer-3 CLI: respiratory events and the per-night summary.

Thin command-line wrapper around the :mod:`resmart.events` core. Detects
probable apneas/hypopneas and mask removal, writes the per-session timeline
and the one-row-per-night summary, and optionally renders the annotated plot.
"""

import argparse
import json
import os

import numpy as np
import pandas as pd

from resmart.config import CHANNELS, DEFAULT_CHANNEL, REPORT_DIR
from resmart.events import (
    APNEA_DROP_PCT,
    DROP_PCT,
    MASK_OFF_MINUTES,
    MIN_DURATION_S,
    detect_flow_limitation,
    detect_mask_removal,
    estimate_ahi,
    event_timeline,
    events_by_hour,
    events_report,
    plot_night_events,
)
from resmart.io import all_session_ids, load_quality_or_error, qc_report_path
from resmart.quality import load_session, read_quality_report
from resmart.stats import _pyplot


def _print_events(session, quality, events, removals, ahi, timeline, params):
    print("session {} | channel {} | {:%Y-%m-%d %H:%M} -> {:%Y-%m-%d %H:%M}".format(
        session.session_id, session.channel, session.start, session.end))
    print("valid {:.1f}% | effective use {:.2f} h | params {}".format(
        quality.valid_pct, ahi.usage_hours, params))
    if removals:
        print("mask removal ({}):".format(len(removals)))
        for r in sorted(removals, key=lambda r: r.start):
            print("  {:%Y-%m-%d %H:%M:%S} -> {:%Y-%m-%d %H:%M:%S} "
                  "({:.1f} min)".format(r.start, r.end, r.duration_s / 60.0))
    else:
        print("mask removal: none")
    if not events:
        print("events: none")
    else:
        print("events ({}): estimated_AHI {:.2f} /h | confident {:.2f} /h "
              "({} suspect)".format(
                  len(events),
                  ahi.estimated_ahi or float("nan"),
                  ahi.estimated_ahi_confident or float("nan"),
                  sum(1 for e in events if e.quality_flag == "suspect")))
        print("  {:<20} {:<20} {:>6} {:>7} {:<9} {:<6}".format(
            "start", "end", "dur", "drop%", "type", "flag"))
        for e in sorted(events, key=lambda e: e.start):
            print("  {:%Y-%m-%d %H:%M:%S} {:%Y-%m-%d %H:%M:%S} {:>6.1f} "
                  "{:>6.0f}% {:<9} {:<6}".format(
                      e.start, e.end, e.duration_s, 100.0 * e.reduction,
                      e.event_type, e.quality_flag))
        bh = events_by_hour(timeline)
        if bh:
            print("per hour: {}".format(
                " | ".join("{}h: {}".format(k, v)
                           for k, v in sorted(bh.items()))))


def _cmd_events(args) -> int:
    session = load_session(args.input, args.session_id, channel=args.channel)
    report_path = args.report_path or qc_report_path(
        args.report_dir, session.session_id, session.start)
    try:
        quality = load_quality_or_error(report_path)
    except ValueError as exc:
        print(str(exc))
        return 2

    removals, thr, _ = detect_mask_removal(
        session, quality, threshold=args.mask_off_threshold,
        min_minutes=args.mask_off_minutes)
    events = detect_flow_limitation(
        session, quality, drop_pct=args.drop_pct,
        min_duration_s=args.min_duration_s, apnea_drop_pct=args.apnea_drop_pct,
        exclude_intervals=[(r.start, r.end) for r in removals])
    ahi = estimate_ahi(session, events, quality)
    timeline = event_timeline(session, events)
    params = {
        "drop_pct": args.drop_pct, "min_duration_s": args.min_duration_s,
        "apnea_drop_pct": args.apnea_drop_pct,
        "mask_off_minutes": args.mask_off_minutes,
        "mask_removal_threshold": round(thr, 4) if thr > 0 else None,
    }

    print()
    _print_events(session, quality, events, removals, ahi, timeline, params)

    out = args.csv or os.path.join(
        args.report_dir,
        "events_session_{0}_{1:%Y-%m-%d}.csv".format(
            session.session_id, session.start))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    timeline.to_csv(out, index=False)
    print()
    print("wrote {}".format(out))

    if args.json:
        payload = {
            "session": {
                "session_id": session.session_id, "channel": session.channel,
                "start": session.start.isoformat(),
                "end": session.end.isoformat(),
            },
            "params": params,
            "mask_removal_threshold": params["mask_removal_threshold"],
            "mask_removals": [r.to_dict() for r in removals],
            "events": [e.to_dict() for e in sorted(events, key=lambda e: e.start)],
            "events_by_hour": events_by_hour(timeline),
            "ahi": ahi.to_dict(),
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print()
        print("wrote {}".format(args.json))

    if args.plot:
        if not args.show:
            import matplotlib
            matplotlib.use("Agg")
        fig, _ = plot_night_events(session, events, removals, quality)
        if args.show:
            _pyplot().show()
            return 0
        out = args.output or os.path.join(
            args.report_dir,
            "events_session_{0}_{1:%Y-%m-%d}.png".format(
                session.session_id, session.start))
        fig.savefig(out, dpi=110)
        print("wrote {}".format(out))
    return 0


def _cmd_events_report(args) -> int:
    ids = all_session_ids(args.input)
    sessions = []
    reports = {}
    missing = []
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

    df = events_report(
        sessions, reports, drop_pct=args.drop_pct,
        min_duration_s=args.min_duration_s, apnea_drop_pct=args.apnea_drop_pct,
        min_mask_off_minutes=args.mask_off_minutes)
    print()
    _print_events_report(df)

    out = args.output or os.path.join(args.report_dir, "events_summary.csv")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    df.to_csv(out, index=False)
    print()
    print("wrote {}".format(out))
    return 0


def _print_events_report(df):
    if df.empty:
        print("events report: no sessions")
        return
    print("events report ({} nights):".format(len(df)))
    cols = ["session_id", "date", "usage_hours", "n_events", "n_suspect",
            "estimated_ahi", "estimated_ahi_confident", "n_mask_removals"]
    print("  " + "  ".join("{:<14}".format(c) for c in cols))
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if pd.isna(v):
                cells.append("")
            elif isinstance(v, np.floating) or isinstance(v, float):
                cells.append("{:.3f}".format(v))
            else:
                cells.append(str(v))
        print("  " + "  ".join("{:<14}".format(x) for x in cells))


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Layer 3: respiratory events and per-night timeline consuming "
            "Layer 1 (SessionData + persisted QualityReport) and Layer 2 "
            "(effective usage). Subcommands: events (one session, full "
            "timeline) and events-report (one row per night)."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("events", help="detect events + timeline of one session")
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
    p.add_argument("--drop-pct", type=float, default=DROP_PCT,
                   help="sustained amplitude drop starting an event "
                        "(default: {})".format(DROP_PCT))
    p.add_argument("--min-duration-s", type=float, default=MIN_DURATION_S,
                   help="minimum sustained length of an event in seconds "
                        "(default: {})".format(MIN_DURATION_S))
    p.add_argument("--apnea-drop-pct", type=float, default=APNEA_DROP_PCT,
                   help="drop starting a probable apnea "
                        "(default: {})".format(APNEA_DROP_PCT))
    p.add_argument("--mask-off-minutes", type=float, default=MASK_OFF_MINUTES,
                   help="near-zero activity this long = mask removal "
                        "(default: {})".format(MASK_OFF_MINUTES))
    p.add_argument("--mask-off-threshold", type=float, default=None,
                   help="absolute per-second activity below which a stretch "
                        "counts as near-zero (default: derived from the data)")
    p.add_argument("--csv", default=None,
                   help="write the per-session event timeline to this CSV "
                        "(default: reports/events_session_<id>_<date>.csv)")
    p.add_argument("--json", metavar="PATH", default=None,
                   help="write the full structured result as JSON")
    p.add_argument("--plot", action="store_true",
                   help="also render the annotated night-events plot")
    p.add_argument("-o", "--output", default=None,
                   help="PNG file to write with --plot (default: "
                        "reports/events_session_<id>_<date>.png)")
    p.add_argument("--show", action="store_true",
                   help="with --plot, display the plot on screen")

    r = sub.add_parser("events-report", help="one summary row per session night")
    r.add_argument("--input", required=True,
                   help="CSV export from the parser or a segmented CSV")
    r.add_argument("--all", action="store_true",
                   help="explicitly select every session (default behaviour)")
    r.add_argument("--channel", default=DEFAULT_CHANNEL,
                   choices=list(CHANNELS),
                   help="signal channel to analyze (default: {})".format(
                       DEFAULT_CHANNEL))
    r.add_argument("--report-dir", default=REPORT_DIR,
                   help="directory holding the default QualityReport JSONs "
                        "(default: {}/)".format(REPORT_DIR))
    r.add_argument("--output", default=None,
                   help="CSV file to write (default: reports/events_summary.csv)")
    r.add_argument("--drop-pct", type=float, default=DROP_PCT,
                   help="sustained amplitude drop starting an event "
                        "(default: {})".format(DROP_PCT))
    r.add_argument("--min-duration-s", type=float, default=MIN_DURATION_S,
                   help="minimum sustained length of an event in seconds "
                        "(default: {})".format(MIN_DURATION_S))
    r.add_argument("--apnea-drop-pct", type=float, default=APNEA_DROP_PCT,
                   help="drop starting a probable apnea "
                        "(default: {})".format(APNEA_DROP_PCT))
    r.add_argument("--mask-off-minutes", type=float, default=MASK_OFF_MINUTES,
                   help="near-zero activity this long = mask removal "
                        "(default: {})".format(MASK_OFF_MINUTES))
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "events":
        return _cmd_events(args)
    if args.command == "events-report":
        return _cmd_events_report(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())