# DESIGN.md

Design documentation for the BMC RESmart GII parser.

- Status: reverse-engineered, unofficial, NOT for medical use.
- Targets: `resmart_parse.py` (parsing + CLI), `preprocess.py` (CSV cleanup, pandas),
  `analysis.py` (pandas helpers such as session segmentation),
  `plotting.py` + `analyze_cpap.py` (matplotlib plots and the chained CLI),
  `quality.py` (Layer 1: ingest, preprocess, signal-quality control, CLI + pytest suite),
  `stats.py` (Layer 2: descriptive per-session statistics + nightly trend, CLI + pytest suite),
  and `graph_data.py` (incomplete GUI).
- Requirements on the toolchain: Python 3, standard library for
  `resmart_parse.py`; pandas/numpy for `preprocess.py`/`analysis.py`/
  `quality.py`, matplotlib for `plotting.py`/`analyze_cpap.py`,
  pytest (dev-only) for the `tests/` suite.

## 1. Requirements

### Functional

- Given a directory containing raw SD-card files (`<serial>.<nnn>`, three-digit
  numeric extension), decode every one-second packet into CSV.
- Produce a CSV with a header row that names every column; known fields carry
  their unit (e.g. `IPAP (0.5 cmH2O)`).
- The first column is an ISO 8601 `timestamp` by default. `-y` emits separate
  year/month/day/hour/minute/second columns; `-s` emits an opaque seconds value.
- `-a` emits all 106 data words with a per-column name; `-2`/`-1` expand the
  25 Hz / 10 Hz subsample arrays (rows 25x / 10x).
- `-d` restricts output to one date or an inclusive range (1 or 2 `YYYY-MM-DD`
  values). `-o` names the output file (default `RESmart_data.csv`).
- `-i` prints a day-by-day summary (hours with data, pulse presence, session
  length) and never writes a CSV.
- Running with no arguments prints the help and exits without reading or
  writing anything, so the default output file is never destroyed by accident.
- `-q` suppresses progress messages.

### Non-functional

- The full dump (~500 MB, up to ~2 million packets) must be processed in a
  bounded amount of RAM and in seconds-to-a-couple-of-minutes, with the output
  file appearing immediately so the user sees progress.
- Output must stay byte-identical to previous versions for identical input and
  flags (regression contract, verified by hashing sample outputs).
- All user-facing text (help, errors, docs) stays in English.

## 2. Data format

From `README.md` plus reverse engineering; the code in
`packet.parse_timestamp` / `parse_data` is the source of truth.

- Each raw file is a sequence of 256-byte packets, one packet per second of
  device-on time.
- Packet layout: 106 little-endian `uint16` words (`packet.dlen`), then an
  8-byte timestamp: `uint16` year + 6 bytes (month, day, hour, minute, second,
  unknown). The unknown 7th byte is exposed as a `?` column with `-y`.
- Word 0 is expected to be `0xAAAA` (magic marker).
- Known words and their stored-value units:

  | index | name        | unit         | notes                          |
  |------:|-------------|--------------|--------------------------------|
  |     1 | usage_day   | (dimensionless)        | day-of-use counter      |
  |     2 | IPAP        | 0.5 cmH2O    | divide by 2 for cmH2O          |
  |     3 | EPAP        | 0.5 cmH2O    | divide by 2 for cmH2O          |
  |    99 | tidal_vol   | L/min        |                                |
  |   102 | spO2_pct    | %            | only valid with oximeter       |
  |   103 | HR_BPM      | bpm          | only valid with oximeter       |
  |   104 | rep_rate    | breaths/min  | 0xFFFF until pressure settles  |

- Words 4-78 are three 25 Hz measurement arrays (`resA_*`, `resB_*`, `resC_*`);
  words 79-88 a 10 Hz array (`pulse_*`). Their physical meaning is speculative.
- The `README.md` table ("120 words", addresses) is loose guesswork and does
  not match the code exactly.

### Storage quirks observed in the sample dump

- The SD card holds two time segments: the recent segment (.000…) followed by
  the older segment wrapped back to June. File `...018` is the wrap file: its
  packets go 2026-09-16 → 2026-06-16 mid-file. Per-file timestamps are
  monotonic except for that single wrap.
- Consequently the dump is not globally time-ordered; the CSV repeats the file
  order (out-of-order timestamps possible across the wrap).
- Each file is a multiple of 256 bytes but the **last** 256-byte block is not
  parsed (legacy loop bound `len - PACKET_SIZE`), discarding up to 1 s per file.

## 3. Architecture

Single-pass, streaming pipeline (`resmart_parse.py`, entry point `main()`):

```
glob('*.[0-9][0-9][0-9]')  (cwd only — no directory argument)
        │
        ▼
per file: read bytes once
        │
        ├─ if -d: cheap timestamp-only pass → (min,max) date bounds
        │      skip file entirely if out of range
        ▼
iterate 256-byte packets (generator, one `packet` object alive at a time)
        │
        ├─ progress prints on first packet of each new day (unless -q)
        ├─ if -d and packet date out of range → drop
        ▼
--info ?  stream into `day_tally` groups → print summary   (read-only)
write ?   `packet_rows()` → buffered rows → flush every 4096 lines
```

Key components:

- `packet`: one packet's state. Data words + timestamp are parsed in `__init__`:
  `parse_timestamp` (one 8-byte unpack), `parse_data` (one bulk
  `struct.unpack("106H", ...)`). All labels/units live as **class-level
  constants** — they are identical for every packet.
- `make_header(args)`: CSV header built only from class constants, so it is
  written before any packet is read (the output file exists from the start).
- `packet_rows(p, args)`: pure function returning the CSV row(s) for one packet
  (1 row, or 10/25 for the high-rate modes).
- `day_tally`: O(1)-memory day aggregation backing `--info`.
- `date_bounds(databuff)`: fast timestamp-only scan used to skip whole files.

Memory stays proportional to one packet plus the write buffer, not to the size
of the dump.

### Secondary processing: session segmentation (`analysis.py`)

The parser/preprocess pipeline ends with a chronologically sorted DataFrame of
one-second packets. The device records only while powered on (nights) and
nothing during the day, so the exported history is a sequence of *sessions*
— contiguous blocks of use separated by long daytime gaps. Because the SD
card holds months of history, session boundaries cannot come from file or
date boundaries and must be derived from the timeline itself:

```
clean_and_preprocess(out.csv)  →  segment_sessions(df, limit_hours=4)  →  per-session stats/plots
```

`segment_sessions` logic:

1. `gap = df["timestamp"].diff()` — timedelta between consecutive rows (the
   first row has no predecessor).
2. A boolean marker starts a new session where `gap > limit_hours`
   (default 4 h, a full daytime off-window).
3. `session_id = marker.cumsum() + 1` — the cumulative sum of the booleans
   assigns a new integer to each run, so every row between markers shares it.
   IDs start at 1.

Small gaps — duplicate same-second rows (wrap) and seconds missing inside a
night — fall below the threshold and do not split a session. `limit_hours=0`
degenerates to one session per row (allowed, useless). The function raises a
`ValueError` if the frame is not already sorted ascending, enforcing the
pipeline order.

### Tertiary processing: plotting (`plotting.py` + `analyze_cpap.py`)

Plotting is a two-layer design: `plotting.py` holds the plot functions and a
**standalone CLI** that consumes the step-5 artifact, while `analyze_cpap.py`
is a convenience wrapper that chains steps 4-6 into one command from the raw
step-3 export.

The standalone step (input = segmented CSV written by `analysis.py -o`,
never re-cleaned/re-segmented):

```
read_segmented_csv(sessions.csv)  →  plot_pressure_curve / plot_overlapped_sessions / plot_waveform  →  save/display
```

`analyze_cpap.py` chains the whole workflow into one command:

```
read_csv → clean_and_preprocess → segment_sessions
        → filter to one session_id → plot_pressure_curve → save/display
```

- Plotting CLI (`python plotting.py -i sessions.csv ...`): flags
  `-i/--input` (required; the segmented CSV from `analysis.py -o`),
  `--session N` (default: most recent), `--overlay N` (mutually exclusive,
  overlays the last N sessions), `--overlay-epap`,
  `--wave {resA,resB,resC,pulse}`, `-o/--output` PNG (default
  `pressure_session_<id>_<date>.png` / `overlapped_sessions_last_<N>.png` /
  `wave_<channel>_session_<id>_<date>.png` next to the input), `--show`.
  It deliberately does **not** re-run cleaning/segmentation — the step-5
  artifact is the single source of truth, so the standalone plot can never
  disagree with a later `analyze_cpap.py` run on the same steps.
- `analyze_cpap.py` (all-in-one convenience wrapper): same target/overlay/
  wave/output/show flags, `--limit-hours` added as a segmentation
  passthrough, and `-i` takes the raw step-3 export; it runs
  `clean_and_preprocess` → `segment_sessions` → the same plotting functions.
- Backend handling: pyplot is imported **lazily** inside the plot functions
  (via `_pyplot()`), so both CLIs can call `matplotlib.use("Agg")` before the
  first figure exists — calling `use()` after pyplot is imported has no
  effect. Agg is forced for file output, the default interactive backend is
  kept for `--show`.
- `read_segmented_csv(path)`: the plotting CLI's entry point. Reads the CSV
  with pandas (ISO `timestamp` parsed), strips column names defensively, and
  raises descriptive errors when `session_id` is missing (→ run step 5 /
  `analysis.py -o`) or timestamps are unsorted (→ run `clean_and_preprocess`
  first).
- `plot_pressure_curve(session_df)` converts the raw pressures to cmH2O by
  dividing by 2 — the device stores IPAP/EPAP in 0.5 cmH2O steps, so a raw
  `20` means `10.0 cmH2O` — into `IPAP_cmH2O`/`EPAP_cmH2O` columns, then plots
  both against time with axes labels, legend, grid and a session-aware
  title. Invalid reads that preprocess converted to NaN simply leave gaps.
  It returns the Matplotlib `(figure, axes)` so the caller decides how to
  save or show it.
- `plot_overlapped_sessions(df, num_sessions=10, include_epap=False)`
  overlays the last N nights. Sessions begin at different wall-clock times
  (22:45 one night, 23:10 the next), so plotting absolute timestamps would
  side-by-side them; instead it builds a per-session relative axis:
  `hours_since_start = (timestamp − groupby("session_id")["timestamp"].transform("first")) / 1 h`,
  so every curve starts at x=0 and recurring patterns after falling asleep
  can be spotted. A `tab10` colormap keeps the curves distinguishable;
  EPAP is opt-in, drawn as faint dashed lines. The pipeline takes the
  `--overlay` branch and skips the single-session filter.
- `plot_waveform(session_df, channel="resA")` plots the measured 25 Hz
  (resA/resB/resC) or 10 Hz (pulse) channels. These are only present in a
  CSV exported with `-2`/`-1`: the parser emits one row per sub-second
  sample with a millisecond timestamp, so the channel column is already a
  dense series (25/10 rows per second in order). Values are drawn as raw
  words (scaling unknown); long sessions are decimated to ≤200k points.
  The CLI selects it with `--wave <channel>`.
- `plot_tidal_volume_distribution(df)` plots a histogram with a KDE overlay
  of the parser's `tidal_vol (L/min)` word (already in L/min, no unit
  conversion). NaN values injected by preprocessing for invalid reads
  (65535) are dropped before binning — histogram/KDE break or distort on
  NaN — using seaborn's `histplot(kde=True, stat="density")`. It uses the
  whole cleaned frame (all sessions together), and the CLI selects it with
  `--tidal`, which is mutually exclusive with the other plot modes.

### Layer 1: ingest, preprocess and quality control (`quality.py`)

Layer 1 sits between segmentation and the event/statistics layers: for a
chosen session and channel it reconciles known units, resamples the signal
onto a regular grid, runs four artifact detectors, splits the night into
hourly activity blocks, and publishes results as a JSON `QualityReport`
plus (optionally) a shaded PNG. It reuses the existing loaders
(`clean_and_preprocess`, `segment_sessions`, `read_segmented_csv`) instead
of re-implementing them:

```
raw CSV (with/without session_id)  →  reconcile_units → load_session
        → resample_signal (median, measured rate / --target-hz)
        → detect_signal_quality → segment_night
        → printed summary + write_quality_report (JSON) + plot_signal_quality (PNG)
```

- `load_session(csv, session_id, channel=...)` auto-detects the input form:
  a segmented CSV (`session_id` column) is read as-is; a raw parser CSV is
  cleaned (`clean_and_preprocess`) and segmented (`segment_sessions`) first.
  Returns a `SessionData` (timestamp series, values, measured period from
  the median positive sample spacing, computed sample rate).
- `reconcile_units(df, findings)` checks known-unit headers once, never
  converts values; it flags mismatches (e.g. `tidal_vol (L/min)` — a volume
  mislabeled with a flow unit) and unknown columns, kept for provenance.
- `resample_signal(df, target_hz=None, method="median", column=...)` maps a
  possibly-irregular series onto even `target_hz` bins (default measured
  rate). Bins are computed in integer nanoseconds to avoid float drift; the
  original timestamps are preserved in `orig_timestamp`, bins without data
  stay NaN, and gaps > 5 s are reported. Downsampling uses the median of the
  covered source samples; upsample keeps NaN under coverage holes. The same
  function serves a second client: the drift detector's 1 Hz envelope.
- `detect_signal_quality(...)` returns a `QualityReport`
  (`session_id`, `channel`, `sample_rate_hz`, `total_samples`,
  `valid_samples`, `valid_pct`, `counts_by_kind`,
  `intervals` = contiguous `SuspiciousInterval`s, `params`, `warnings`).

**Detector semantics** were chosen *for oscillatory breath/flow signals like
resA at 25 Hz*, where fast transitions are legitimate signal — over-firing
on real data exposed wrong initial designs (see "Key design decisions"):

- *flatline*: rolling std over ~1 s below `1e-3 × scale` — dead/zeroed
  sensor stretches (device idle).
- *clipping*: samples pinned at **explicit** sensor saturation bounds
  (`clip_lo`/`clip_hi`). Default `None` disables the check, because the
  0.5 %/99.5 % quantiles of a breathing trace are its *legitimate* peaks and
  troughs — quantile-derived bounds flagged real breath extremes.
- *spike*: impulse noise = a sample that jumps out of the trace and back.
  Both of its bounding steps must exceed `spike_rel_factor` (25) × the
  **rolling median of the trace's own positive sample-to-sample steps**,
  with opposite signs. The local positive-step reference means a sharp legit
  peek costs the same as the edges around it and breath "holds" (0 steps)
  do not collapse the threshold; a run of consecutive large same-direction
  steps is a legitimate breath edge, never a spike.
- *drift*: slow baseline walk of the signal **envelope** (per-second median
  bins): the envelope's 300 s rolling mean leaving the session median by
  more than `drift_rel_tol` (0.5) × the envelope scale. Operates on the
  envelope so periodic breathing cannot read as drift; skipped when the
  envelope is shorter than the window.

- `segment_night` splits the part of the session inside `22:00–07:00` into
  hourly blocks and marks a block *active* when its rolling peak-to-trough
  amplitude (5 s window) stays within a physiological band (the 10th
  percentile of the session's positive amplitudes is the default floor).
- `plot_signal_quality(session, report)` shades invalid stretches on the
  signal trace (lazy matplotlib backend, mirroring `plotting.py`).
- `write_quality_report(report, path)` persists the JSON artifact.
- CLI: `python quality.py preprocess --input <csv> <session_id>
  [--report qc.json] [--plot] [-o out.png] [--show] [--channel resA]
  [--target-hz N] [--spike-rel-factor F] [--clip-lo L] [--clip-hi H]
  [--limit-hours H]`. The `--report`/`--plot` steps read and merge the
  already-computed report; `--show` needs an interactive backend.
- Test suite: `tests/test_quality.py` (22 tests, run with `pytest` from the
  repository root). Synthetic fixtures inject a known flatline, clipped
  stretch, impulse spikes, a baseline step and a fast legit ramp, and cover
  the input auto-detection, unit reconciliation, resample edge cases, JSON
  round-trip and night-segmentation boundaries.

### Layer 2: descriptive statistics per session (`stats.py`)

Layer 2 sits between Layer 1 and the future event/anomaly layers: it turns
one session's signal and its persisted `QualityReport` into descriptive
statistics and a one-row-per-night trend. It never reloads or re-checks the
signal — it *consumes* the Layer-1 artifacts:

```
SessionData (load_session) + QualityReport (read_quality_report)
        -> _analysis_frame (same resample identity grid as preprocess)
        -> valid_mask (union of report.intervals over the grid)
        -> session_summary  (usage, flow shape, tidal, RR summary)
        -> volume_distribution (histogram, modes, Hartigan BC)
        -> respiratory_rate (per-block autocorrelation estimates)
        -> nightly_trend_summary -> reports/nightly_trend.csv
```

- The per-sample valid mask is reconstructed deterministically from the
  report's `intervals` (the complement of their union on the resampled grid,
  exactly how `detect_signal_quality` derived `valid_samples`); every metric
  is computed on the valid samples only and reports the coverage it was
  computed over.
- **Usage** = `n_valid × measured period` vs. the session's total duration;
  unrealistic usage drives the later adherence/anomaly layers.
- **Tidal volume** is the integral of each inspiratory phase = a *positive
  excursion above a slow rolling-median baseline* (`BASELINE_S`, ~45 s — many
  breath cycles) whose height reaches `MIN_PEAK_FACTOR` (12) × the signal's
  noise scale (median positive sample-to-sample step). Both constants were
  calibrated on the real night: a 5 s baseline tracked the breath waveform
  itself, chopping breaths into ~9.5k micro-lobes and a modal volume of 1.6;
  the long baseline + peak floor recovers ~6.9k breaths vs. the ~6.7k the
  autocorrelation RR (16.3 bpm) implies, with a sane mode ≈ 21. Values stay
  in raw units (`resA` scaling unconfirmed) and are never converted to
  liters.
- **Volume distribution** uses Freedman–Diaconis bins floored at the √n
  rule (FD alone collapses bimodal spreads to a few bins, hiding the modes);
  reports skew, kurtosis and Hartigan's bimodality coefficient
  `BC = (s²+1)/(k + 3(n-1)²/((n-2)(n-3)))`. `bimodal = True` only when
  `BC > 5/9` *and* a valley-separated secondary mode exists (BC alone
  false-positives on narrow clusters and flat-topped humps).
- **Respiratory rate** is estimated per 5-min block from the FFT
  autocorrelation of the detrended flow (first lag peak in 2-10 s → 6-30
  bpm); a block is valid only when ≥ 50 % of its samples are QC-valid and a
  strong (> 0.2) periodic peak exists. This is a flow-derived estimate, not
  a clinical rate — AHI/events are explicitly out of scope (BMC software
  only, see README).
- `nightly_trend_summary` raises a descriptive error naming the sessions
  missing a `QualityReport` rather than silently re-running QC; the CLI
  fails with exit code 2.
- CLI: `python stats.py stats <session_id> --input <csv> [--json out.json]
  [--plot] [-o out.png] [--show] [--channel resA] [--report-path p]
  [--report-dir reports] [--block-minutes 5]` and `python stats.py trend
  --input <csv> [--all] [--output reports/nightly_trend.csv]
  [--report-dir reports]`. Both consume reports named
  `reports/qc_session_<id>_<date>.json` (the Layer-1 default) via
  `read_quality_report`.
- Test suite: `tests/test_stats.py` (12 tests, `pytest` from the repository
  root). Synthetic breathing-like (sine) fixtures verify usage gating by QC
  intervals, the raw-units warning contract, modal volume ≈ the analytical
  sine-lobe integral, unimodal-vs-bimodal separation, RR recovery at a known
  15 bpm, invalid flat blocks, trend aggregation, and both CLIs' JSON/CSV
  outputs and fail-fast report handling.

## 4. Key design decisions

- **Streaming over accumulate-then-write.** The original implementation parsed
  every packet into memory and only opened the output after reading all files;
  a full-duum involved ~2M objects, minutes of runtime, and no visible output.
  Streaming bounds memory to O(1) packets and lets the CSV appear instantly.
- **Class-level label constants.** `setup_labels()` rebuilt identical label
  lists/dicts (~85 `format()` calls) per packet — measured ~18 s per 16 MB
  file, i.e. ~10 min for the dump. Moving that to class attributes cut parsing
  to ~1-2 s per second of total work and is the single biggest win.
- **Bulk `struct.unpack`.** One `"106H"` unpack replaces 106 per-word unpacks.
- **Date filtering while reading.** `-d` drops packets during the pass and skips
  whole files using exact min/max bounds (correct across the wrap file) instead
  of naive first/last endpoint checks.
- **`-o/--output` instead of a positional argument.** The previous positional
  output file was silently swallowed by `--dates` (greedy `nargs='+'`); moving
  it to an option eliminates the ambiguity.
- **Raw words are not converted.** Units are documented in the header (e.g.
  `0.5 cmH2O`) instead of rescaling values, keeping output faithful to the
  device data.
- **Detectors calibrated on the real signal's statistics, not on synthetic
  plausibility.** The initial spike detector (pointwise z against a rolling
  amplitude MAD), quantile-derived clipping bounds and a 60 s amplitude-mean
  drift test all over-fired on the real 25 Hz resA night (46 % valid, 30k
  drift minutes): the channel's sharp transitions are *legitimate* flow
  ramps (edges reach ~20-40 words/sample over several samples) and its
  extreme values are real breath peaks. The reworked detectors therefore
  reason in the transition domain with local references:
  spike = isolated out-and-back step above the trace's *own* positive-step
  median; clipping = explicit sensor bounds only; drift = the 1 Hz median
  *envelope*. The same night then reports **100 % valid, 0 artifacts** —
  a defensible negative (the channel is genuinely clean), with the synthetic
  fixtures still proving each detector fires on its own artifact class.
- **Statistics calibrated on the real night too.** Layer 2's tidal-volume
  segmentation went through the same cycle: a short (5 s) baseline chased the
  breath waveform and the modal breath came out at 1.6 raw units with ~9.5k
  fragments; the long baseline (45 s) + a peak floor at 12 × the noise step
  recovers ~6.9k breaths matching the ~6.7k the autocorrelation RR implies
  (mode ≈ 21, p90 ≈ 33). The mode/bimodality machinery (FD-floored-at-√n
  bins, valley-separated secondary modes, BC + structure gating) is likewise
  tuned against the raw night's unimodal distribution rather than assumed.
- **Always persist the QC report.** Layer 1 writes its `QualityReport` by
  default (`--report-dir reports/`), so Layer 2 has a stable, reproducible
  artifact to consume and never silently re-runs detection with possibly
  different settings.
- **Default ISO timestamp column** with millisecond precision for high-rate
  subsample rows; `-y`/`-s` kept for backward compatibility.
- **Runtime safety:** Python 3 enforced at the top of `main()`; invalid/extra
  `-d` inputs and empty input directories fail through `parser.error`/clean
  message with non-zero exit codes.

## 5. Constraints

- `resmart_parse.py` uses the standard library only (`struct`, `argparse`, `glob`,
  `datetime`); the analysis/plotting scripts use the packages declared in
  `requirements.txt`: `numpy`/`pandas` (preprocess/analysis/stats),
  `matplotlib` (plotting), `seaborn` (tidal-volume KDE; its histogram/KDE
  relies on scipy underneath), and `pytest` (dev-only, for the `tests/`
  suite). No build step, test framework in CI, or linting.
- Input files are discovered from the **current working directory**; there is no
  directory argument (`scripts` invoked by path, `cwd` = data directory).
- Python 3 only.
- `resources\` contains real, sensitive patient data and is gitignored; it must
  never be committed. Derived per-patient artifacts (`reports\`) are likewise
  gitignored but stay local for stitching sessions and trends.
- Project and all documentation stay in English (see `AGENTS.md`).

## 6. Known problems

- `-s`/`--time_seconds` help says "since beginning of year" and the README says
  "month"; both are wrong — the value derives from the packet's date ordinal.
  Treat it as opaque.
- Word meanings beyond the seven known fields are guesses; the `resA/B/C` and
  `pulse` arrays have no confirmed physical interpretation.
- Word 1 is **not** Reslex (the device stores no such 1-5 softness setting
  here): it is a counter that increments ~1/day of use (320 -> 406 across
  this 91-day dump), with edge transitions at power-on and around midnight.
  It is surfaced in the CSV as `usage_day`; its exact tick semantics are
  unresolved.
- Words 2/3 (IPAP/EPAP) are the machine's *configured* pressures, not
  measured instantaneous pressure: in fixed CPAP mode both are constant and
  identical (raw 13 = 6.5 cmH2O in 100% of this dump's packets). The
  per-breath measured signals are the 25 Hz arrays (words 4-78).
- Dump timestamps are not globally sorted (wrap file); the CSV preserves file
  order. `--info` can therefore list a date twice (two time segments).
- The final 256-byte packet of each file is skipped, losing up to 1 s per file.
- spO2/HR are only present when an oximeter is attached; invalid values are
  stored as `0xFFFF` and reported as-is.
- `graph_data.py` is an unfinished placeholder GUI that plots random data; it
  does not read RESmart data yet.
- High-rate modes (`-2`/`-1`) inflate output 25x/10x, producing large CSVs.
- Raw CSV column names carry a leading space (rows are glued with `", "`);
  `preprocess.py` strips them.
- The sample dump contains no `65535` values, so the invalid-to-NaN path in
  `preprocess.py` is latent (only exercised by synthetic data / SpO2-less dumps).
- Event/apnea detection is not implemented (neither the device nor this code
  performs it; the BMC analysis software does).
- The Layer-1/2 test suites cover `quality.py`/`stats.py` with synthetic
  fixtures; the parser/preprocess/analysis/plotting code itself is checked
  only by hashing sample outputs against the previous version (the regression
  contract).
- The `resA/B/C` 25 Hz channels carry no unit/scaling confirmation and are
  analyzed in raw words; the QC thresholds and Layer-2 tidal-volume/peak
  constants are documented defaults calibrated on one night's data and may
  need tuning for other devices.
- Layer-2 tidal volumes are baseline-relative integrals in raw units (never
  converted to liters) and its `rr` is an autocorrelation estimate from the
  flow channel — not a clinical respiratory rate; apnea/hypopnea events are
  not (and cannot be) derived here.

## 7. Feature roadmap

- [done] `preprocess.py`: `clean_and_preprocess` — pandas pipeline that parses
  ISO timestamps, sorts chronologically (wrap repair), maps 65535 to NaN,
  strips column names and resets the index. Consumers rely on the "Data
  contract for downstream tools" section in `README.md`.
- [done] `analysis.py`: `segment_sessions` — assigns each row a 1-based
  `session_id` for its night of use from the gap between consecutive rows
  (see "Secondary processing" in the architecture section). The standalone
  `main` now also persists the segmented frame via `-o` so a later step can
  plot it from the saved artifact.
- [done] `plotting.py` + `analyze_cpap.py` — `plot_pressure_curve` derives
  `IPAP_cmH2O`/`EPAP_cmH2O` (raw / 2) and plots a session's pressure curves;
  `plot_overlapped_sessions` overlays the last N nights on a relative
  hours-since-start axis; `plot_waveform` plots a high-rate channel
  (resA/B/C at 25 Hz, pulse at 10 Hz) from a `-2`/`-1` export. `plotting.py`
  is now also a standalone CLI consuming the step-5 CSV (`--session` /
  `--overlay` / `--wave` / `-o` / `--show`); `analyze_cpap.py` remains the
  all-in-one wrapper that chains clean → segment → plot in one command.
  In both CLIs `--tidal` selects `plot_tidal_volume_distribution` (a
  histogram + KDE of tidal volume over the whole frame; seaborn).
- [done] `quality.py` — Layer 1: `reconcile_units`/`load_session`/
  `resample_signal`/`detect_signal_quality`/`segment_night`, the JSON
  `QualityReport` (written by default into `reports/`),
  the shaded plot, the `preprocess` CLI and the pytest suite
  (`tests/test_quality.py`).
- [done] `stats.py` — Layer 2: `session_summary`/`volume_distribution`/
  `respiratory_rate`/`nightly_trend_summary` consuming the persisted
  `QualityReport`, the `stats`/`trend` CLI, the annotated volume plot and the
  pytest suite (`tests/test_stats.py`). Next step: time-series/anomaly
  detection across nights on `reports/nightly_trend.csv`, then AHI-style
  event analysis (scope permitting).
- `graph_data.py`: turn the placeholder into a real viewer that reads the
  cleaned CSV (daily hour strip chart, IPAP/EPAP/flow traces, spO2 overlay).
- Optional unit conversion flag (e.g. report IPAP/EPAP in cmH2O instead of raw
  0.5-cmH2O words).
- Optional global time sorting of the output to flatten the wrap-file ordering.
- Parse the adjacent `.usr`, `.evt`, `.idx`, `.log` files produced by the BMC
  software.
- Confirm the true meaning of words 4-88 and 89-98 (and the pads).
- Add a small smoke-test suite (synthetic packets) and run it on push.
- Progress reporting with ETA for very long runs (already bounded in memory).