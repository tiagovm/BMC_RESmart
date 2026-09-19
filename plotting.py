"""Plotting helpers and a standalone plotting CLI for RESmart sessions.

Requires matplotlib and pandas (approved exceptions). Not for medical use.

Library use:
    from plotting import plot_pressure_curve, plot_overlapped_sessions
    fig, ax = plot_pressure_curve(session_df)

CLI use (input must be the segmented CSV from step 5, i.e. the output of
``analysis.py -o``, with a ``session_id`` column):
    python plotting.py -i sessions.csv --session 3
    python plotting.py -i sessions.csv --overlay 10
    python plotting.py -i sessions.csv --wave resA --session 3
    python plotting.py -i sessions.csv --tidal

The pyplot backend is left to the caller: the CLI forces Agg for file
output and leaves the default interactive backend for --show. Pyplot is
imported lazily inside the plot functions so the backend can be selected
first.
"""

import argparse
import os

import matplotlib

import pandas as pd

IPAP_RAW = "IPAP (0.5 cmH2O)"
EPAP_RAW = "EPAP (0.5 cmH2O)"

IPAP_CMH2O = "IPAP_cmH2O"
EPAP_CMH2O = "EPAP_cmH2O"

HOURS_SINCE_START = "hours_since_start"

RAW_PER_CMH2O = 2.0

_plt = None


def _pyplot():
    """Return the pyplot module, importing it lazily on first use.

    Importing pyplot creates its backend, so this happens lazily inside the
    functions to let a CLI call ``matplotlib.use("Agg")`` (for headless file
    output) before any figure exists. Calling ``use()`` after pyplot is
    already imported has no effect.
    """
    global _plt
    if _plt is None:
        from matplotlib import pyplot
        _plt = pyplot
    return _plt


def plot_pressure_curve(session_df):
    """Plot the IPAP/EPAP pressure curves of a single sleep session.

    ``session_df`` must hold exactly one session: output of
    ``clean_and_preprocess`` filtered down to one ``session_id``. The raw
    device values count in steps of 0.5 cmH2O (1 raw unit = 0.5 cmH2O), so
    two new columns are derived by dividing them by 2:

      IPAP_cmH2O = IPAP (0.5 cmH2O) / 2.0
      EPAP_cmH2O = EPAP (0.5 cmH2O) / 2.0

    e.g. a raw stored value of 20 becomes 10.0 cmH2O. Invalid readings (65535
    already converted to NaN by preprocessing) simply leave gaps in the line.

    Returns the ``(figure, axes)`` pair; the caller decides whether to save
    it or display it on screen.
    """
    missing = [c for c in (IPAP_RAW, EPAP_RAW) if c not in session_df.columns]
    if missing:
        raise ValueError(
            "session dataframe is missing columns: {}".format(", ".join(missing))
        )
    if len(session_df) < 2:
        raise ValueError("need at least 2 data points to plot a pressure curve")

    df = session_df.copy()
    df[IPAP_CMH2O] = df[IPAP_RAW] / RAW_PER_CMH2O
    df[EPAP_CMH2O] = df[EPAP_RAW] / RAW_PER_CMH2O

    sid = int(df["session_id"].iloc[0]) if "session_id" in df.columns else 0
    start = df["timestamp"].iloc[0]
    end = df["timestamp"].iloc[-1]

    fig, ax = _pyplot().subplots(figsize=(14, 4))
    ax.plot(df["timestamp"], df[IPAP_CMH2O], label="IPAP", color="tab:blue")
    ax.plot(df["timestamp"], df[EPAP_CMH2O], label="EPAP", color="tab:orange")
    ax.set_title(
        "Pressure - session {} ({:%Y-%m-%d %H:%M} -> {:%H:%M})".format(sid, start, end)
    )
    ax.set_xlabel("Time")
    ax.set_ylabel("Pressure (cmH2O)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig, ax


def plot_overlapped_sessions(df, num_sessions=10, include_epap=False):
    """Overlay the IPAP curves of the last ``num_sessions`` nights.

    Sessions start at different wall-clock times (e.g. 22:45 one night,
    23:10 the next), so plotting absolute timestamps would place the curves
    side by side instead of on top of each other. The x axis is therefore
    rebuilt as time *relative to each session's own start*: a column
    ``hours_since_start`` is derived by subtracting each row's session start
    timestamp and converting to hours, so every curve begins at x=0 and can
    be compared for recurring pressure patterns after falling asleep.

    The device stores IPAP/EPAP in 0.5 cmH2O steps, so the raw words are
    divided by 2 into cmH2O (see ``plot_pressure_curve``). Only IPAP is
    drawn by default; pass ``include_epap=True`` to overlay EPAP as well (in
    fainter dashes on the same pair of axes).

    ``df`` must be the output of ``clean_and_preprocess`` followed by
    ``segment_sessions`` (chronologically sorted, with ``session_id``). If
    fewer sessions are available than requested, all of them are used.
    Duplicate same-second rows (wrap) and NaN columns draw fine.

    Returns the ``(figure, axes)`` pair; the caller decides whether to save
    it or display it on screen.
    """
    required = {"timestamp", "session_id", IPAP_RAW}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            "dataframe is missing columns: {}".format(", ".join(sorted(missing)))
        )

    sids = sorted(df["session_id"].unique())[-num_sessions:]
    sub = df[df["session_id"].isin(sids)].copy()
    sub[HOURS_SINCE_START] = (
        (sub["timestamp"] - sub.groupby("session_id")["timestamp"].transform("first"))
        / pd.Timedelta(hours=1)
    )
    sub[IPAP_CMH2O] = sub[IPAP_RAW] / RAW_PER_CMH2O
    if include_epap:
        sub[EPAP_CMH2O] = sub[EPAP_RAW] / RAW_PER_CMH2O

    colors = _pyplot().get_cmap("tab10")
    fig, ax = _pyplot().subplots(figsize=(14, 5))
    for i, sid in enumerate(sids):
        part = sub[sub["session_id"] == sid]
        if part.empty:
            continue
        start = part["timestamp"].iloc[0]
        ax.plot(part[HOURS_SINCE_START], part[IPAP_CMH2O], label="{0} ({1:%Y-%m-%d})".format(sid, start),
                color=colors(i % colors.N))
        if include_epap:
            ax.plot(part[HOURS_SINCE_START], part[EPAP_CMH2O], label="{0} EPAP ({1:%Y-%m-%d})".format(sid, start),
                    color=colors(i % colors.N), linestyle="--", alpha=0.4)
    ax.set_title("Overlapped sessions - IPAP (last {} night{})".format(
        len(sids), "" if len(sids) == 1 else "s"))
    ax.set_xlabel("Hours since start of session")
    ax.set_ylabel("Pressure (cmH2O)")
    ax.legend(loc="upper right", fontsize="small")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig, ax


WAVE_CHANNELS = {"resA": "resA", "resB": "resB", "resC": "resC", "pulse": "pulse"}

MAX_WAVE_POINTS = 200000


def plot_waveform(session_df, channel="resA"):
    """Plot a high-rate waveform channel of one sleep session.

    The 25 Hz (resA/resB/resC) and 10 Hz (pulse) arrays are only present in
    a CSV exported with ``-2``/``-1``: the parser writes one row per
    sub-second sample with a millisecond timestamp, so the channel column is
    already a dense time series (rows of the same second are in sample
    order). ``session_df`` must hold exactly one session of such a frame.

    The values are stored verbatim (raw device words; the scaling of these
    channels is not known), so the y axis is labelled as raw units.

    Very long sessions are downsampled to at most ``MAX_WAVE_POINTS`` to
    keep plotting fast; the line is drawn from every k-th sample.

    Returns the ``(figure, axes)`` pair; the caller decides whether to save
    it or display it on screen.
    """
    if channel not in WAVE_CHANNELS:
        raise ValueError("unknown channel {!r}; choose from {}".format(
            channel, ", ".join(sorted(WAVE_CHANNELS))))
    col = WAVE_CHANNELS[channel]
    if col not in session_df.columns:
        raise ValueError(
            "column '{}' not found: re-export the CSV with -2 "
            "(resA/resB/resC) or -1 (pulse) and run clean_and_preprocess".format(col)
        )

    ts = session_df["timestamp"]
    if "session_id" in session_df.columns:
        sid = int(session_df["session_id"].iloc[0])
    else:
        sid = 0
    start = ts.iloc[0]
    end = ts.iloc[-1]

    n = len(session_df)
    step = max(1, round(n / MAX_WAVE_POINTS))
    x = ts.iloc[::step]
    y = session_df[col].iloc[::step].astype(float)

    fig, ax = _pyplot().subplots(figsize=(14, 4))
    ax.plot(x, y, color="tab:blue", linewidth=0.6)
    ax.set_title("Waveform {} - session {} ({:%Y-%m-%d %H:%M} -> {:%H:%M})".format(
        channel, sid, start, end))
    ax.set_xlabel("Time")
    ax.set_ylabel("{} (raw units)".format(channel))
    if step > 1:
        ax.set_title(ax.get_title() + " (downsampled {:d}x)".format(step))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig, ax


TIDAL_RAW = "tidal_vol (L/min)"


def plot_tidal_volume_distribution(df):
    """Plot a histogram with a density (KDE) overlay of tidal volume.

    ``df`` is the cleaned frame from ``clean_and_preprocess``; the values
    come from the parser's tidal volume word and are already in L/min
    (column ``tidal_vol (L/min)``), so no unit conversion is needed. Invalid
    device reads (0xFFFF -> NaN by preprocessing) are dropped up front:
    histogram bins and a kernel density estimate are distorted or fail when
    given NaN values.

    The whole frame is plotted (all sessions together); the distribution
    covers every recorded night, not a single one.

    Returns the ``(figure, axes)`` pair; the caller decides whether to save
    it or display it on screen.
    """
    col = None
    for c in df.columns:
        if c.strip() == TIDAL_RAW:
            col = c
            break
    if col is None:
        raise ValueError(
            "column '{}' not found: it is produced by resmart_parse.py + "
            "clean_and_preprocess".format(TIDAL_RAW)
        )

    values = df[col].dropna().astype(float)
    if values.empty:
        raise ValueError("tidal volume column '{}' is empty or all NaN".format(TIDAL_RAW))

    import seaborn as sns

    fig, ax = _pyplot().subplots(figsize=(10, 5))
    sns.histplot(values, kde=True, stat="density", ax=ax,
                 color="tab:blue", edgecolor="white", linewidth=0.2)
    ax.set_title("Tidal Volume Distribution")
    ax.set_xlabel("Volume (L/min)")
    ax.set_ylabel("Density")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    return fig, ax


def read_segmented_csv(path):
    """Read the step-5 output (a segmented CSV) for the plotting CLI.

    The file must be the frame written by ``analysis.py -o``: cleaned,
    chronologically sorted, with the ``session_id`` column added by
    ``segment_sessions``. The standalone plotting CLI deliberately does not
    re-run cleaning/segmentation — use ``analyze_cpap.py`` if you want the
    whole workflow chained into one command.

    The file is the output of ``analysis.py -o``, so it must be already
    cleaned and chronologically sorted with a ``session_id`` column; column
    names are stripped defensively. Raises a ValueError with a hint when the
    sorted, instead of producing a meaningless plot.
    """
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df.columns = [c.strip() for c in df.columns]
    if "session_id" not in df.columns:
        raise ValueError(
            "column 'session_id' not found in {}: run step 5 first "
            "(python analysis.py <exported.csv> -o <segmented.csv>) "
            "so the plot sees the sessions".format(path)
        )
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError(
            "timestamp must be chronologically sorted: run "
            "clean_and_preprocess() and segment_sessions() first"
        )
    return df


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Plot RESmart session data (IPAP/EPAP pressure curves, "
            "overlapped nights, or a high-rate waveform channel) from a "
            "segmented CSV. Input is the step-5 output: a cleaned, "
            "chronologically sorted CSV that already has the session_id "
            "column (see README step 5, or combine all steps with "
            "analyze_cpap.py)."
        )
    )
    parser.add_argument("-i", "--input", required=True,
                        help="segmented CSV from step 5 "
                             "(python analysis.py out.csv -o sessions.csv)")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--session", type=int, default=None,
                        help="session_id to plot (default: most recent session)")
    target.add_argument("--overlay", type=int, default=None,
                        help="overlay the last N sessions on a relative "
                             "hours-since-start time axis")
    target.add_argument("--tidal", action="store_true",
                        help="plot a histogram + density curve of tidal "
                             "volume across the whole frame instead of a "
                             "per-session plot")
    parser.add_argument("--overlay-epap", action="store_true",
                        help="with --overlay, draw the EPAP curves as well")
    parser.add_argument("--wave", default=None,
                        choices=["resA", "resB", "resC", "pulse"],
                        help="plot a high-rate waveform channel of the "
                             "selected session instead of the pressure "
                             "curves (the CSV must come from an export made "
                             "with -2 or -1)")
    parser.add_argument("-o", "--output", default=None,
                        help="PNG file to write "
                             "(default: pressure_session_<id>_<date>.png, "
                             "overlapped_sessions_last_<N>.png or "
                             "wave_<channel>_session_<id>_<date>.png next "
                             "to the input)")
    parser.add_argument("--show", action="store_true",
                        help="display the plot on screen instead of writing a file")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.tidal and args.wave:
        parser.error("--tidal and --wave select different plot modes; use one")

    if not args.show:
        matplotlib.use("Agg")

    df = read_segmented_csv(args.input)

    session_ids = sorted(df["session_id"].unique())
    if not session_ids:
        print("no data found in {}".format(args.input))
        return 1

    if args.tidal:
        fig, _ = plot_tidal_volume_distribution(df)
        default_name = "tidal_volume_distribution.png"
        if args.show:
            _pyplot().show()
            return 0
        if args.output is None:
            out = os.path.join(os.path.dirname(os.path.abspath(args.input)), default_name)
        else:
            out = args.output
        fig.savefig(out, dpi=110)
        print("wrote {}".format(out))
        return 0

    if args.overlay:
        fig, _ = plot_overlapped_sessions(
            df, num_sessions=args.overlay, include_epap=args.overlay_epap)
        sids = sorted(df["session_id"].unique())[-args.overlay:]
        printed = ", ".join("{} ({:%b %d})".format(
            s, df[df["session_id"] == s]["timestamp"].iloc[0]) for s in sids)
        print("overlaying session ids: {}".format(printed))
        if args.show:
            _pyplot().show()
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

    if args.session is None:
        sid = session_ids[-1]
    elif args.session in session_ids:
        sid = args.session
    else:
        print("session {} not found; available session ids: {}".format(
            args.session, session_ids))
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
        _pyplot().show()
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