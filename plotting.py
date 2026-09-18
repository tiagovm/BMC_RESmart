"""Plotting helpers for RESmart session data.

Requires matplotlib (approved exception). Not for medical use.

The pyplot backend is left to the caller: ``analyze_cpap.py`` forces Agg
for file output and leaves the default interactive backend for --show.
"""

import matplotlib.pyplot as plt

IPAP_RAW = "IPAP (0.5 cmH2O)"
EPAP_RAW = "EPAP (0.5 cmH2O)"

IPAP_CMH2O = "IPAP_cmH2O"
EPAP_CMH2O = "EPAP_cmH2O"

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