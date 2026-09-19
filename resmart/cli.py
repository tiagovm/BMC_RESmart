"""Consolidated command-line entry point for the resmart package.

Every layer of the analysis is reachable as a subcommand that forwards the
remaining arguments verbatim to the matching module's ``main``:

    python -m resmart parse      ... resmart.parse          (raw SD-card decode)
    python -m resmart preprocess ... resmart.preprocess     (clean the CSV)
    python -m resmart segment    ... resmart.analysis       (nights of use)
    python -m resmart plot       ... resmart.plotting       (pressure/wave plots)
    python -m resmart analyze    ... resmart.analyze_cpap   (one-command plots)
    python -m resmart quality    ... resmart.quality_cli    (Layer 1)
    python -m resmart stats      ... resmart.stats_cli      (Layer 2)
    python -m resmart events     ... resmart.events_cli     (Layer 3)
    python -m resmart pipeline   ... resmart.workflow       (all layers, one shot)

Each subcommand keeps its own argparse contract (flags, defaults, exit codes),
so the individual CLIs behave identically when invoked through the package.

Not for medical use.
"""

import argparse
import sys

from resmart import analyze_cpap, analysis, events_cli, parse, plotting, preprocess
from resmart import quality_cli, stats_cli
from resmart.config import CHANNELS, DEFAULT_CHANNEL, REPORT_DIR
from resmart.events import APNEA_DROP_PCT, DROP_PCT, MASK_OFF_MINUTES, MIN_DURATION_S


MODULES = {
    "parse": parse.main,
    "preprocess": preprocess.main,
    "segment": analysis.main,
    "plot": plotting.main,
    "analyze": analyze_cpap.main,
    "quality": quality_cli.main,
    "stats": stats_cli.main,
    "events": events_cli.main,
}


def _help_text():
    lines = [
        "usage: python -m resmart <step> [options]",
        "",
        "steps:",
    ]
    for name, fn in MODULES.items():
        lines.append("  {:<12} {}".format(name, (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""))
    lines.append("  {:<12} run quality + stats + events for every session".format("pipeline"))
    lines.append("")
    lines.append("run 'python -m resmart <step> -h' for the options of one step")
    return "\n".join(lines)


def _build_pipeline_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Run the full analysis (quality -> stats -> events) for every "
            "session of one CSV export in a single call. Writes the same "
            "artifacts under the report directory as the individual steps."
        )
    )
    parser.add_argument("--input", required=True,
                        help="CSV export from the parser (with -2 for resA) "
                             "or a segmented CSV (with session_id)")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL,
                        choices=list(CHANNELS),
                        help="signal channel to analyze (default: {})".format(
                            DEFAULT_CHANNEL))
    parser.add_argument("--report-dir", default=REPORT_DIR,
                        help="directory for derived artifacts "
                             "(default: {}/)".format(REPORT_DIR))
    parser.add_argument("--steps", default="quality,stats,events",
                        help="comma-separated subset of "
                             "quality,stats,events (default: all three)")
    parser.add_argument("--limit-hours", type=float, default=4.0,
                        help="session segmentation gap threshold (default: 4 h)")
    parser.add_argument("--block-minutes", type=float, default=5.0,
                        help="respiratory-rate estimation block length "
                             "(default: 5)")
    parser.add_argument("--drop-pct", type=float, default=DROP_PCT,
                        help="sustained amplitude drop starting an event "
                             "(default: {})".format(DROP_PCT))
    parser.add_argument("--min-duration-s", type=float, default=MIN_DURATION_S,
                        help="minimum sustained length of an event in seconds "
                             "(default: {})".format(MIN_DURATION_S))
    parser.add_argument("--apnea-drop-pct", type=float, default=APNEA_DROP_PCT,
                        help="drop starting a probable apnea "
                             "(default: {})".format(APNEA_DROP_PCT))
    parser.add_argument("--mask-off-minutes", type=float, default=MASK_OFF_MINUTES,
                        help="near-zero activity this long = mask removal "
                             "(default: {})".format(MASK_OFF_MINUTES))
    return parser


def _cmd_pipeline(argv) -> int:
    args = _build_pipeline_parser().parse_args(argv)
    steps = [s.strip() for s in (args.steps or "").split(",") if s.strip()]
    valid = {"quality", "stats", "events"}
    unknown = [s for s in steps if s not in valid]
    if unknown:
        print("pipeline: unknown step(s): {}".format(", ".join(unknown)),
              file=sys.stderr)
        return 2
    if not steps:
        print("pipeline: --steps must name at least one of quality,stats,events",
              file=sys.stderr)
        return 2

    from resmart.workflow import run_full_pipeline

    result = run_full_pipeline(
        args.input, channel=args.channel, report_dir=args.report_dir,
        limit_hours=args.limit_hours, block_minutes=args.block_minutes,
        drop_pct=args.drop_pct, min_duration_s=args.min_duration_s,
        apnea_drop_pct=args.apnea_drop_pct,
        mask_off_minutes=args.mask_off_minutes,
        run_quality="quality" in steps, run_stats="stats" in steps,
        run_events="events" in steps)

    print("pipeline done: {} session(s)".format(len(result.sessions)))
    for sid in result.session_ids:
        r = result.by_session[sid]
        line = "  session {}:".format(sid)
        parts = []
        if r.quality is not None:
            parts.append("valid {:.1f}%".format(r.quality.valid_pct))
        if r.stats is not None:
            parts.append("use {:.2f} h".format(r.stats.usage_duration_s / 3600.0))
        if r.ahi is not None:
            parts.append("events {} | estimated_AHI {:.2f} /h".format(
                len(r.events),
                r.ahi.estimated_ahi if r.ahi.estimated_ahi is not None else 0.0))
        print(line + (" " + " | ".join(parts) if parts else ""))
    print()
    for path in result.artifacts:
        print("wrote {}".format(path))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(_help_text(), file=sys.stderr)
        return 2

    step, rest = argv[0], argv[1:]
    if step in MODULES:
        return MODULES[step](rest)
    if step == "pipeline":
        return _cmd_pipeline(rest)

    print(_help_text(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())