"""Plotting helpers for RESmart session data.

Requires matplotlib (approved exception). Not for medical use.

The pyplot backend is left to the caller: ``analyze_cpap.py`` forces Agg
for file output and leaves the default interactive backend for --show.
"""

import matplotlib.pyplot as plt
import pandas as pd

IPAP_RAW = "IPAP (0.5 cmH2O)"
EPAP_RAW = "EPAP (0.5 cmH2O)"

IPAP_CMH2O = "IPAP_cmH2O"
EPAP_CMH2O = "EPAP_cmH2O"

HOURS_SINCE_START = "hours_since_start"

RAW_PER_CMH2O = 2.0


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

    fig, ax = plt.subplots(figsize=(14, 4))
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

    colors = plt.get_cmap("tab10")
    fig, ax = plt.subplots(figsize=(14, 5))
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

    fig, ax = plt.subplots(figsize=(14, 4))
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