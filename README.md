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
`requirements.txt` (`pip install -r requirements.txt`): `numpy`/`pandas`
for preprocessing/analysis/statistics, `matplotlib` for plotting, and
`seaborn` for the tidal-volume distribution plot. The `parse` step
(`resmart/parse.py`) itself is standard library only. `pytest` (dev-only)
runs the test suites in `tests/` (66 tests: 25 quality + 18 stats + 15
events + 8 CLI): `python -m pytest -q`. Not for medical use.

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

    python -m resmart parse -i -q

It prints a day-by-day summary: hours with recorded data, whether pulse
data is present, and session length.

> Note: there is no directory argument. The `parse` step looks for `*.nnn`
> files in the current working directory, so `cd` into the data folder
> (e.g. `resources\2026-09-18\`) first. Because the code now lives in the
> `resmart/` package, running `python -m resmart parse` from that folder
> needs the repo root on `PYTHONPATH` (PowerShell:
> `$env:PYTHONPATH='C:\Users\tiago\OneDrive\Documentos\Devel\BMC_RESmart'`).
> From the repo root itself it just works.

### 3. Export the data to CSV

Still inside the data folder, export to a CSV (the file appears
immediately; reading is streaming, roughly 2 seconds per day):

    python -m resmart parse -o out.csv                       # whole card
    python -m resmart parse -o out.csv -d 2026-07-21         # one day
    python -m resmart parse -o out.csv -d 2026-07-21 2026-07-31   # date range
    python -m resmart parse -a -o out.csv                    # every raw field

- Without `-d` the whole card is exported (this dump: ~500 MB input,
  ~92 MB / 1.97 M rows of CSV in ~14 s).
- Row order follows the order of the files on the SD card, which is
  **not** chronological: storage is circular, so the timeline can jump
  back in time at the wrap-around file. The preprocessing step repairs
  this.

### 4. Clean and preprocess the CSV

The analysis-ready step (`python -m resmart preprocess`, requires pandas):

    python -m resmart preprocess out.csv            # prints shape + data summary

or, for use inside your own analysis code:

    from resmart.preprocess import clean_and_preprocess
    df = clean_and_preprocess("out.csv")

It returns a DataFrame where the ISO timestamps are real datetimes,
rows are sorted chronologically (wrap-around repaired), invalid sensor
reads (65535 / 0xFFFF) are converted to NaN, column names are stripped,
and the index is reset. See the module docstring for the exact steps.

### 5. Segment the data into sessions (nights of use)

The device records one packet per second while it is powered on (a night
of use) and nothing while it is off (the daytime). A session is a
contiguous block of use, isolated by large gaps in the timeline:

    from resmart.analysis import segment_sessions
    df = segment_sessions(df)                 # default: new session after 4 h

or standalone (which also prints a per-session summary):

    python -m resmart segment out.csv         # python -m resmart segment out.csv 6 (limit hours)

To keep the segmented frame for the next steps, persist it with `-o`:

    python -m resmart segment out.csv -o sessions.csv

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

Plotting is its own step (`python -m resmart plot`), fed by the segmented CSV saved
at the end of step 5 with `python -m resmart segment -o`:

    python -m resmart plot -i sessions.csv --session 3        # night 3 -> PNG
    python -m resmart plot -i sessions.csv --overlay 10       # last 10 nights overlapped
    python -m resmart plot -i sessions.csv --overlay 10 --overlay-epap   # ... + EPAP
    python -m resmart plot -i sessions.csv --show             # most recent night, on screen
    python -m resmart plot -i sessions.csv -o night.png
    python -m resmart plot -i sessions.csv --tidal            # tidal-volume histogram + KDE

To plot the measured 25 Hz waveform (the configured IPAP/EPAP are flat
presets, see Data format), the *export* must have used `-2`, and the same
steps 4-5 then produce a segmented CSV with the wave columns. Because
the `segment` step cleans internally, only two commands are needed:

    python -m resmart parse -2 -o wave.csv -d 2026-07-21
    python -m resmart segment wave.csv -o sessions.csv    # clean + segment + save
    python -m resmart plot -i sessions.csv --wave resA    # also: resB, resC, pulse

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
  `plot_overlapped_sessions(df, num_sessions=10)` from `resmart.plotting`
  return the Matplotlib figure/axes for a single-session DataFrame;
  `plot_tidal_volume_distribution(df)` does the same for the whole cleaned
  frame.

### 7. Verify signal quality (Layer 1, quality step)

The `quality` step is the first analysis layer: it ingests a session's signal,
preprocesses it (reconcile units → load → resample), runs detection on
flatline/clipping/spike/drift artifacts, splits the night into hourly
activity blocks, and emits a machine-readable JSON report plus (optionally)
a shaded PNG plot. Not for medical use.

It consumes either the raw parser CSV (step 3) or the segmented CSV
(step 5); the input is recognized by the presence of a `session_id`
column. The `-2` export is preferred so the 25 Hz `resA` waveform is
analyzed (default channel `--channel resA`):

    python -m resmart quality preprocess out2.csv 2 --report qc.json --plot -o qc.png
    python -m resmart quality preprocess sessions.csv 1 --report qc_1.json
    python -m resmart quality preprocess sessions.csv 2 --plot --show

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

### 8. Descriptive statistics per session (Layer 2, stats step)

The `stats` step is the second analysis layer: it turns one session's signal and
its persisted `QualityReport` (Layer 1) into descriptive statistics — usage,
flow shape, per-breath tidal volume with its distribution, and a respiratory
rate estimate — plus a one-row-per-night trend across sessions. It never
re-loads or re-checks the signal itself: `python -m resmart quality preprocess` must have
run first and written the JSON report (Layer 1 now does so by default into
`reports/`, gitignored). Not for medical use.

    python -m resmart stats 2 --input sessions.csv        # detail for session 2
    python -m resmart stats 2 --input out2.csv --json stats_2.json
    python -m resmart stats trend --input sessions.csv    # one row per night

Both subcommands accept the raw parser CSV (step 3, `-2` gives the 25 Hz
`resA` waveform) or the segmented CSV (step 5); they locate each session's
`QualityReport` at `reports/qc_session_<id>_<date>.json` unless
`--report-path` (`stats`) or `--report-dir` (both) is given. A missing
report is a clear error telling you to run `python -m resmart quality preprocess` first —
the layer never silently re-runs QC.

`stats` prints:

- **usage** — hours used / hours on record, and the % of samples the QC
  pass marked valid; every figure below is computed on those samples only;
- **flow shape** — median / mean / IQR / p05 / p95 of the signal in raw
  units;
- **tidal volume** — per-breath integrals over the inspiratory phases (raw
  units: `resA` scaling is unconfirmed, so volumes are never converted to
  liters), with the distribution's mode, p90, skew, kurtosis and Hartigan's
  bimodality coefficient (BC > ~0.555 hints at bimodality; a session is
  flagged `bimodal` only when BC *and* a valley-separated secondary mode
  agree);
- **respiratory rate** — per-5-min-block estimates from the flow's
  autocorrelation (lag peak in 2-10 s → 6-30 bpm), summarized as
  median/IQR with block coverage. This is a *flow-derived estimate*, not a
  clinical rate; apnea/hypopnea events remain out of scope (BMC software
  only).

`--json PATH` writes the full structured result (session + distribution +
per-block RR table), `--plot` renders the annotated volume-distribution
histogram (`-o out.png`, `--show` to display), and `--channel` switches the
analyzed signal (resA/resB/resC/pulse).

`trend` aggregates one row per session night (usage, validity, flow median,
tidal mode, mean RR) into `reports/nightly_trend.csv`, the input basis for
the later time-series/anomaly layers.

### 9. Respiratory events and timeline (Layer 3, events step)

The `events` step is the third analysis layer: it detects respiratory events from
the flow signal and builds the per-night timeline, consuming the same
`SessionData` + persisted `QualityReport` as Layer 2 (nothing is reloaded or
re-checked). Not for medical use.

    python -m resmart events 2 --input sessions.csv --json events_2.json
    python -m resmart events 2 --input out2.csv --plot -o night_events.png
    python -m resmart events events-report --input sessions.csv  # one row per night

`events` works on a single session and reports:

- **mask removal** — sustained near-zero-activity stretches (≥ 2 min by
  default) are read as the mask being off, not as physiology. The activity
  threshold is derived from the data (20 % of the night's median per-second
  amplitude) and surfaced so the report states what was decided;
- **events** — sustained amplitude reductions of the per-second flow
  envelope vs. a *local* 5-minute rolling baseline: a drop of ≥ 30 % lasting
  ≥ 10 s is a hypopnea, ≥ 80 % an apnea (simplified AASM conventions, flow
  derived only — no oximetry, no thoracic-effort channel, central vs.
  obstructive not separable). Each event carries start/end timestamps,
  duration, reduction, type and a quality flag: events overlapping a
  QC-suspect interval (flatline/clipping/spike/drift) or sitting mostly on
  invalid samples are marked `suspect` and never presented as clean
  physiology;
- **estimated_AHI** — events per QC-valid usage hour (the Layer-2 effective
  use, not wall-clock); both a total and a confident variant that excludes
  suspect events. A flow-based estimate, never a clinical AHI;
- **timeline** — the events above, sorted chronologically with their
  wall-clock hour band (midnight-crossing sessions keep the correct dates),
  persisted to `reports/events_session_<id>_<date>.csv` and rendered with
  `--plot` (mask removal in gray, suspect events hatched).

`events-report` aggregates one row per session night (usage, event count,
suspect share, estimated_AHI + confident variant, per-hour breakdown, mask
removals) into `reports/events_summary.csv`.

Both subcommands accept the raw parser CSV (step 3, `-2`) or the segmented
CSV (step 5) and locate each `QualityReport` at
`reports/qc_session_<id>_<date>.json` unless `--report-path`/`--report-dir`
tells them otherwise; a missing report is a clear error pointing at
`python -m resmart quality preprocess` — the layer never re-runs QC. Tunables: `--drop-pct`,
`--min-duration-s`, `--apnea-drop-pct`, `--mask-off-minutes` (and
`--mask-off-threshold` to pin the mask-removal threshold).

### 10. All-in-one option (analyze step)

For convenience, steps 4-6 can be chained into a single command that
takes the raw step-3 export and does clean → segment → plot internally
(`python -m resmart analyze`):

    python -m resmart analyze -i out.csv --session 3        # night 3 -> PNG
    python -m resmart analyze -i out.csv --overlay 10       # last 10 nights overlapped
    python -m resmart analyze -i out.csv --overlay 10 --overlay-epap   # ... + EPAP
    python -m resmart analyze -i out.csv --show             # most recent night, on screen
    python -m resmart analyze -i out.csv -o night.png --limit-hours 6
    python -m resmart analyze -i out.csv --tidal            # tidal-volume histogram + KDE

It shares the target/`--overlay`/`--overlay-epap`/`--wave`/`-o`/`--show`
flags with the plotting CLI of step 6 and adds `--limit-hours` as a
passthrough to the segmentation; its input is the raw parser CSV (step 3).
`--tidal` (mutually exclusive with `--session`/`--overlay`/`--wave`) skips
the segmentation and plots the tidal-volume distribution of the whole
cleaned frame.
Use the step-by-step commands when you want to keep and reuse the
intermediate segmented CSV, or this single command when you only want one
plot of the full pipeline.

### 11. One-shot analysis pipeline (pipeline step)

To run the three analysis layers (quality + stats + events) for every
session in one command, use the one-shot pipeline step:

    python -m resmart pipeline --input sessions.csv --report-dir reports

It runs quality + stats + events for each detected session and materializes
the same artifacts the individual steps write: per-session
`qc_session_<id>_<date>.json` and `events_session_<id>_<date>.csv`, plus
`nightly_trend.csv` and `events_summary.csv` in `--report-dir` (default
`reports/`). Optional `--steps quality[,stats[,events]]` selects a subset of
the layers; `--output <dir>` controls the session-plot dir.

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
  `python -m resmart segment -o` as-is: `timestamp` in ISO format, the `session_id`
  column, and the data columns (no index row is written). Do not re-order
  the rows afterwards — `plot` refuses unsorted timestamps.

## CLI reference

~~~~

usage: python -m resmart parse [-h] [--info] [--f25_hz] [--f10_hz]
                               [--all_data] [--time_ymd] [--time_seconds]
                               [--quiet] [--dates DATES [DATES ...]]
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

    python -m resmart parse -o out.csv -d 2026-07-21
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