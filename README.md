# BMC_RESmart
Decode data files from BMC Medical RESMart GII systems.

Note: the operation of this code has been reverse-engineered from
undocumented data. Data outputs may be misunderstood and/or
erroneously interpreted.

WARNING: UNFIT FOR ANY MEDICAL USE INCLUDING BUT NOT LIMITED TO
DIAGNOSIS AND MONITORING. NO WARRANTY EXPRESS OR IMPLIED. UNDOCUMENTED
DATA INTERFACE: USE AT OWN RISK

The RESMart II system records and stores operational data on an SD
card. This data is also analyzed and interpreted by the RESmart nPAP
Data Analysis Software also made by BMC. Since this is not freely
available, I have created this software as an alternative.

This software extracts raw data for your own analysis, including time
and duration of use, IPAP, EPAP, and pressure settings,
airflow, tidal volume, respiration rate, as well as SP02 and pulse
rate if the auxiliary pulse oximeter is used.

Note that the determination of apnea/hypopnea events seems to be done
only in the BMC software and not on the device, so this is not
currently supported by the software here.

## Workflow

End-to-end usage of the scripts in this repository.

Python dependencies for the analysis/plotting steps are listed in
`requirements.txt` (`pip install -r requirements.txt`): `pandas` for
preprocessing/analysis, `matplotlib` for plotting, and `seaborn` for the
tidal-volume distribution plot. `resmart_parse.py` itself is standard
library only. `pytest` (dev-only) runs the Layer-1 test suite in `tests/`:
`python -m pytest -q`.

### 1. Get the data off the device

Copy the SD-card contents into a folder on your computer. The raw
data files are the ones with a three-digit numeric extension:

~~~~
NNCNNNNN.000
NNCNNNNN.001
.
.
NNCNNNNN.nnn
~~~~

where `NNCNNNNN` is the device serial number (e.g. `16C01034`). The
`.usr`, `.evt`, `.idx` and `.log` files are written by the BMC PC
software and are not read by the parser.

### 2. Inspect what the card contains (optional)

From inside the data folder, run the parser in read-only info mode (`-i`
never writes a file):

    python resmart_parse.py -i -q

It prints a day-by-day summary: hours with recorded data, whether pulse
data is present, and session length.

> Note: there is no directory argument. The parser looks for `*.nnn`
> files in the current working directory, so `cd` into the data folder
> (e.g. `resources\2026-09-18\`) and give the script's full path:
> `python ..\..\resmart_parse.py -i -q`.

### 3. Export the data to CSV

Still inside the data folder, export to a CSV (the file appears
immediately; reading is streaming, roughly 2 seconds per day):

    python resmart_parse.py -o out.csv                       # whole card
    python resmart_parse.py -o out.csv -d 2026-07-21         # one day
    python resmart_parse.py -o out.csv -d 2026-07-21 2026-07-31   # date range
    python resmart_parse.py -a -o out.csv                    # every raw field

- Without `-d` the whole card is exported (this dump: ~500 MB input,
  ~92 MB / 1.97 M rows of CSV in ~14 s).
- Row order follows the order of the files on the SD card, which is
  **not** chronological: storage is circular, so the timeline can jump
  back in time at the wrap-around file. The preprocessing step repairs
  this.

### 4. Clean and preprocess the CSV

The analysis-ready step (`preprocess.py`, requires pandas):

    python preprocess.py out.csv            # prints shape + data summary

or, for use inside your own analysis code:

    from preprocess import clean_and_preprocess
    df = clean_and_preprocess("out.csv")

It returns a DataFrame where the ISO timestamps are real datetimes,
rows are sorted chronologically (wrap-around repaired), invalid sensor
reads (65535 / 0xFFFF) are converted to NaN, column names are stripped,
and the index is reset. See the module docstring for the exact steps.

### 5. Segment the data into sessions (nights of use)

The device records one packet per second while it is powered on (a night
of use) and nothing while it is off (the daytime). A session is a
contiguous block of use, isolated by large gaps in the timeline:

    from analysis import segment_sessions
    df = segment_sessions(df)                 # default: new session after 4 h

or standalone (which also prints a per-session summary):

    python analysis.py out.csv                # analysis.py out.csv 6 (limit hours)

To keep the segmented frame for the next steps, persist it with `-o`:

    python analysis.py out.csv -o sessions.csv

`segment_sessions` adds a `session_id` column (integer, sessions start at
1) that every row of the same night shares. Only a gap of more than
`limit_hours` between consecutive rows starts a new session; small gaps
(duplicate same-second rows at the wrap, missing seconds inside a night)
do not. It requires the preprocessed, chronologically sorted DataFrame
and raises an error otherwise. See `DESIGN.md` for the algorithm.

### 6. Plot the sessions (standalone)

The cleaned, session-tagged DataFrame from steps 4-5 is the common input
for the analysis and visualization scripts (statistics per session,
plots, etc.). Obey the data contract below when writing them.

Plotting is its own step (`plotting.py`), fed by the segmented CSV saved
at the end of step 5 with `analysis.py -o`:

    python plotting.py -i sessions.csv --session 3        # night 3 -> PNG
    python plotting.py -i sessions.csv --overlay 10       # last 10 nights overlapped
    python plotting.py -i sessions.csv --overlay 10 --overlay-epap   # ... + EPAP
    python plotting.py -i sessions.csv --show             # most recent night, on screen
    python plotting.py -i sessions.csv -o night.png
    python plotting.py -i sessions.csv --tidal            # tidal-volume histogram + KDE

To plot the measured 25 Hz waveform (the configured IPAP/EPAP are flat
presets, see Data format), the *export* must have used `-2`, and the same
steps 4-5 then produce a segmented CSV with the wave columns. Because
`analysis.py` cleans internally, only two commands are needed:

    python resmart_parse.py -2 -o wave.csv -d 2026-07-21
    python analysis.py wave.csv -o sessions.csv           # clean + segment + save
    python plotting.py -i sessions.csv --wave resA        # also: resB, resC, pulse

- `-i/--input` is the segmented CSV from step 5 (already cleaned, sorted,
  with `session_id`); the plotting step deliberately does not re-run
  cleaning or segmentation — a file without `session_id` gives a
  descriptive error. Without `--session` the most recent night is used;
  an unknown id prints the available ids.
- `--session N`, `--overlay N` and `--tidal` are mutually exclusive.
  `--overlay N`
  overlays the last `N` nights on a common time axis of *hours since each
  session started* (sessions begin at different wall-clock times, so a
  relative axis is what makes them line up for comparison);
  `--overlay-epap` adds the EPAP curves in dashed faint lines. If fewer
  sessions exist than requested, all of them are drawn.
- `--tidal` plots a histogram with a kernel-density overlay of the tidal
  volume (respiratory analysis), saved as
  `tidal_volume_distribution.png` next to the input (or `-o`). It uses the
  whole frame (all nights together); the values come from the parser's
  `tidal_vol (L/min)` column, already in L/min, with invalid reads
  (65535 -> NaN) dropped before binning.
- `--wave <channel>` requires a CSV exported with `-2` (resA/resB/resC)
  or `-1` (pulse); `--session N` still selects the night (default: most
  recent), the output is `wave_<channel>_session_<id>_<date>.png`.
- The plot shows the night's IPAP/EPAP pressure curves in cmH2O (the raw
  device values are stored in 0.5 cmH2O steps and divided by 2), saved as
  `pressure_session_<id>_<date>.png` / `overlapped_sessions_last_<N>.png`
  next to the input unless `-o` is given; `--show` displays it on screen
  instead (no Agg backend).
- Programmatic use: `plot_pressure_curve(session_df)` and
  `plot_overlapped_sessions(df, num_sessions=10)` from `plotting.py`
  return the Matplotlib figure/axes for a single-session DataFrame;
  `plot_tidal_volume_distribution(df)` does the same for the whole cleaned
  frame.

### 7. Verify signal quality (quality.py, Layer 1)

`quality.py` is the first analysis layer: it ingests a session's signal,
preprocesses it (reconcile units → load → resample), runs detection on
flatline/clipping/spike/drift artifacts, splits the night into hourly
activity blocks, and emits a machine-readable JSON report plus (optionally)
a shaded PNG plot. Not for medical use.

It consumes either the raw parser CSV (step 3) or the segmented CSV
(step 5); the input is recognized by the presence of a `session_id`
column. The `-2` export is preferred so the 25 Hz `resA` waveform is
analyzed (default channel `--channel resA`):

    python quality.py preprocess out2.csv 2 --report qc.json --plot -o qc.png
    python quality.py preprocess sessions.csv 1 --report qc_1.json
    python quality.py preprocess sessions.csv 2 --plot --show

Prints a text summary (valid fraction, suspect intervals, night blocks)
and, with `--report`, writes a JSON `QualityReport` sized for downstream
layers: `session_id`, `channel`, `sample_rate_hz`, `total_samples`,
`valid_samples`, `valid_pct`, `counts_by_kind`
(`flatline`/`clipping`/`spike`/`drift`), `params` (thresholds actually
used) and a list of `intervals` (`start`/`end`/`kind`). `--plot` renders
the signal with invalid stretches shaded, next to the input as
`qc_session_<id>_<date>.png` unless `-o`/`--show` is given. Tunables:
`--spike-rel-factor`, `--clip-lo`, `--clip-hi`, `--limit-hours`
(segmentation passthrough) and `--target-hz` (default: measured rate).

Detector semantics (chosen for oscillatory breath/flow signals where fast
transitions are legit signal — see `DESIGN.md`):

- **flatline** – rolling std over ~1 s below 1e-3 of the signal scale
  (dead/zeroed sensor data);
- **clipping** – samples pinned at explicit sensor saturation bounds.
  Pass the real A/D raw-word bounds via `--clip-lo`/`--clip-hi`; without
  them no clipping is flagged (the endpoints of a breathing trace are its
  legit peaks/troughs, not clipping);
- **spike** – impulse noise: a sample that jumps out of the trace *and
  back* (its step to both neighbours exceeds `--spike-rel-factor` × the
  rolling median of the trace's own positive sample-to-sample steps, with
  opposite signs). A run of consecutive large steps is a legitimate breath
  edge and is never flagged;
- **drift** – slow baseline walk of the signal *envelope* (1 Hz median
  bins): its 300 s rolling mean leaving the session median by > 0.5 of the
  envelope scale. Requires ≥ 300 s of data, else skipped.

### 8. All-in-one option (analyze_cpap.py)

For convenience, steps 4-6 can be chained into a single command that
takes the raw step-3 export and does clean → segment → plot internally
(`analyze_cpap.py`):

    python analyze_cpap.py -i out.csv --session 3        # night 3 -> PNG
    python analyze_cpap.py -i out.csv --overlay 10       # last 10 nights overlapped
    python analyze_cpap.py -i out.csv --overlay 10 --overlay-epap   # ... + EPAP
    python analyze_cpap.py -i out.csv --show             # most recent night, on screen
    python analyze_cpap.py -i out.csv -o night.png --limit-hours 6
    python analyze_cpap.py -i out.csv --tidal            # tidal-volume histogram + KDE

It shares the target/`--overlay`/`--overlay-epap`/`--wave`/`-o`/`--show`
flags with the plotting CLI of step 6 and adds `--limit-hours` as a
passthrough to the segmentation; its input is the raw parser CSV (step 3).
`--tidal` (mutually exclusive with `--session`/`--overlay`/`--wave`) skips
the segmentation and plots the tidal-volume distribution of the whole
cleaned frame.
Use the step-by-step commands when you want to keep and reuse the
intermediate segmented CSV, or this single command when you only want one
plot of the full pipeline.

`graph_data.py` is an unfinished placeholder GUI and does not read RESmart
data yet.

## Data contract for downstream tools

Any tool that consumes the parser's CSV or the preprocessed DataFrame
should assume:

- There is always a header row naming every column. Known fields carry
  their unit in the column name (e.g. `IPAP (0.5 cmH2O)`).
- The first column defaults to `name="timestamp"`, an ISO 8601
  timestamp (`2026-07-21T23:59:45`). Sub-second modes (`-2`, `-1`)
  give each row a millisecond timestamp. See the CLI reference for the
  `-y` / `-s` alternatives, which do not produce a `timestamp` column.
- Column names in the raw CSV may carry a leading space (the parser
  glues fields with `", "`); preprocess strips them.
- The raw parser output is ordered by file, not by time; timestamps can
  go backwards at the circular-wrap boundary and the same second can
  appear twice there. Sort on `timestamp` before analysis
  (preprocess does this and keeps duplicates).
- Value 65535 (0xFFFF) means "invalid measurement", e.g. respiration
  rate for the first ~30 seconds after the start of airflow, or SpO2 /
  heart rate when no oximeter is attached. Replace with NaN before
  statistics or plotting (preprocess does this).
- After `segment_sessions`, the column `session_id` is an int starting
  at 1 that identifies the night (contiguous block of use); new sessions
  start only where the gap between consecutive rows exceeds
  `limit_hours`. It assumes chronologically sorted data.
- The standalone plotting CLI (step 6) reads the segmented CSV written by
  `analysis.py -o` as-is: `timestamp` in ISO format, the `session_id`
  column, and the data columns (no index row is written). Do not re-order
  the rows afterwards — `plotting.py` refuses unsorted timestamps.

## CLI reference

~~~~

usage: resmart_parse.py [-h] [--info] [--f25_hz] [--f10_hz] [--all_data]
                        [--time_ymd] [--time_seconds] [--quiet]
                        [--dates DATES [DATES ...]]
                        [-o OUTPUT]
[in a directory containing .000, .001... raw data files ]

options:
  -h, --help            show this help message and exit
  -o, --output          Output data CSV file (default: RESmart_data.csv);
                        it overwrites existing data
  --info, -i            Prints readable summary of data and dates to stdout
  --f25_hz, -2          Print out all 25 Hz (flow) data. this will make output
                        files 25x as big.
  --f10_hz, -1          Print out all 10 Hz (pulse) data. this will make output
                        files 10x as big.
  --all_data, -a        Print out all 1Hz data fields known or unknown
  --time_ymd, -y        Print timestamp in Y, M, D, H, M, S format (separate
                        columns)
  --time_seconds, -s    Print timestamp in seconds since beginning of year
  --quiet, -q           Do not print progress and info to stderr
  --dates, -d DATES...  select date range in YYYY-MM-DD format. Single date is
                        one day, two dates are start and end of time range.

Running with no arguments prints this help and exits without writing any
files. --info prints a summary only and never writes the CSV.

Example:

    resmart_parse.py -o out.csv -d 2026-07-21
~~~~

## Data format

The raw data files (`*.nnn`) are read in sequence. Each file consists
of a sequence of 256-byte packets; each packet corresponds to one
second of data formatted as 106 two-byte unsigned integers followed by
an 8-byte timestamp. The format of the integers is as follows (these
are guesses!):

~~~~

Address      Interpretatation
000          Always 0xAAAA
001          Usage day counter: increments ~1/day while the device is used
             (320 -> 406 over this 91-day dump); not a setting
002          IPAP value in units of 0.5 cm H20 (divide by 2 to get cm)
003          EPAP value in units of 0.5 cm H20 (divide by 2 to get cm)
004-028      25 values of something related to pressure at 25 Hz
029-053      25 values of of something related to pressure at 25 Hz
054-078      25 values of instaneous flow at 25 Hz
079-084      10 values of something oscillatory at 10Hz (motor drive?)
085-094      unknown values related to pressure?
095          Tidal volume in liters per minute
098          spO2 (blood oxygenation) in percent (only if oximeter attached)
099          Heart rate in bpm (only if oximeter attached)
100          Respiration rate in breaths per minute
101-120      zero padding
~~~~

Some values are 0xffff (65535) when not valid, for example the
respiration rate takes 30 or more seconds to become valid after the
start of pressure flow.

The word 001 field (stored in the CSV under the column `usage_day`) is not
a Reslex softness setting as originally guessed: it is a counter that
ticks up roughly once per day of use (observed 320 -> 406 across this
dump's 91 days, with edge transitions at power-on and around midnight).
Its exact semantics are not fully confirmed.

IPAP and EPAP words are the machine's *configured* pressures, not measured
instantaneous pressure. In fixed-single-pressure CPAP mode both are
constant and identical: every packet of this dump reports raw 13
(6.5 cmH2O). The values actually measured over each breath are the 25 Hz
arrays above (words 4-53 are pressure-related, 54-78 flow), or their CSV
forms `resA`/`resB`/`resC` (see the `-2`/`--wave` workflow in step 6).

The last 8 bytes are the timestamp, one 16-bit integer for the year,
followed by 5 unsigned bytes for month, day, hour, minute, and second.
The final byte is an unknown value.

The CSV output always starts with a header row that names every
column; where a unit is known it appears in the column name, e.g.
`IPAP (0.5 cmH2O)`. The first column is `timestamp`, an ISO 8601
timestamp (`2026-07-21T23:59:45`). `-y` replaces it with separate
year/month/day/hour/minute/second columns and `-s` with an opaque
seconds value. When `-2` or `-1` are used, each sub-second sample row
carries its own ISO timestamp with milliseconds.