# AGENTS.md

Python 3 tools that decode raw SD-card data dumps from a BMC RESmart GII CPAP machine. Reverse-engineered, unofficial, not for medical use. See `README.md` for the packet-format spec and `DESIGN.md` for architecture and design decisions.

## Run / verify

No build system or linting. `resmart_parse.py` uses only the standard library (struct/glob/argparse/datetime); the analysis/plotting scripts use the packages listed in `requirements.txt` (install with `pip install -r requirements.txt`): `numpy`/`pandas` for `preprocess.py`/`analysis.py`/`quality.py`/`stats.py`, `matplotlib` for `plotting.py`/`analyze_cpap.py`, `seaborn` for the tidal-volume distribution plot (`plot_tidal_volume_distribution`), plus dev-only `pytest` for the Layer-1/Layer-2 suites in `tests/`.

Run the tests with:

```
python -m pytest -q
```

(25 tests in `tests/test_quality.py` + 18 in `tests/test_stats.py`; synthetic fixtures only — nothing real touched.) Verify parser changes by running against the sample dump:

```
python resmart_parse.py -i -q        # run while cwd = dir containing the data files
```

- `resmart_parse.py` discovers input via cwd glob `*.nnn` (`*.[0-9][0-9][0-9]`); there is **no directory argument**. To test, run from `resources\<date>\` (full path to the script) or copy a couple of `.nnn` files to a temp dir.
- Reading is single-pass and streaming: the `-o` file is created immediately, rows are written as packets stream in, and `-d` skips/filters during the read. The full dump (~500 MB / ~2 M packets) takes seconds for one day (`-a -d` ~2 s) and ~15 s uncompressed default.
- Running with no arguments prints the CLI help to stderr and exits (code 2) — it reads or writes nothing. `RESmart_data.csv` (the default `-o` output file) is written only when output flags are passed; `--info` is read-only and never writes the CSV.
- The CSV always has a header row naming every column; the first column is an ISO 8601 `timestamp` (`2026-07-21T23:59:45`). Known fields carry their unit in the header (e.g. `IPAP (0.5 cmH2O)`), driven by `packet.known_units`.
- Regression contract: output must stay byte-identical for the same input/flags. After changing row/header generation, regenerate and hash the 8 mode variants (default, `-y`, `-s`, `-a`, `-2`, `-1`, `-a -y`, `-d` range) against the previous run.
- Layer-1 verification: `python quality.py preprocess <seg_csv> <session_id> [--report qc.json] --plot` (it loads/reconciles/resamples/detects/segments and writes a JSON `QualityReport`). The detectors are synthetic-fixture-tested; on the real night they are expected to report ~100 % valid (see `DESIGN.md` "Key design decisions"). Preprocess now always writes the QC JSON — by default `reports/qc_session_<id>_<date>.json` (set `--report-dir`, or `--report` for an explicit path).
- Layer-2 verification: `python stats.py stats <session_id> --input <csv> [--json out.json]` and `python stats.py trend --input <csv>` (consumes the Layer-1 persist reports via `read_quality_report`; never re-runs QC — a missing report is a clear exit code 2 error instructing `quality.py preprocess`). `reports/` holds derived per-patient artifacts and is gitignored; SessionData/QualityReport flow is: `load_session` → same resample grid as quality → `valid_mask` (report.intervals complement) → `session_summary`/`volume_distribution`/`respiratory_rate` → `nightly_trend_summary`.
- Requires Python 3 (hard-exits otherwise at module top, before arg parsing).

## Language / git

- Keep the project and all its documentation in English: code comments, CLI help and error messages, README, and this file. Do not translate existing content to another language.
- Commit and push messages (and PRs, if any) must also be written in English.

## Gotchas

- `--time_seconds` help text says "since beginning of year" and README says "month"—neither is right; it's seconds from the packet date ordinal. Treat the number as opaque.
- Field indexes: code reads 106 words (`packet.dlen`) + 8-byte timestamp; `known_fields` in `packet.setup_labels` is the source of truth. The README address table is loose guesswork and does not match the code exactly.
- `graph_data.py` is an unfinished placeholder GUI that plots random data — it does not read RESmart data yet.
- `resources\` holds a real patient data dump (large, ~500 MB) of sensitive medical data. It is gitignored — never commit it or any `*.nnn/.usr/.log/.evt/.idx` data.
- `reports\` holds derived per-patient artifacts (QC JSON, nightly trend, plots). It is gitignored — never commit it.