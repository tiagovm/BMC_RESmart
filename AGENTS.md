# AGENTS.md

Python 3 tools that decode raw SD-card data dumps from a BMC RESmart GII CPAP machine. Reverse-engineered, unofficial, not for medical use. See `README.md` for the packet-format spec and `DESIGN.md` for architecture and design decisions.

## Run / verify

No build system, deps, tests, or linting. `resmart_parse.py` uses only the standard library (struct/glob/argparse/datetime); the analysis/plotting scripts use the packages listed in `requirements.txt` (install with `pip install -r requirements.txt`): `pandas` for `preprocess.py`/`analysis.py`, `matplotlib` for `plotting.py`/`analyze_cpap.py`, and `seaborn` for the tidal-volume distribution plot (`plot_tidal_volume_distribution`). Verify changes by running against the sample dump:

```
python resmart_parse.py -i -q        # run while cwd = dir containing the data files
```

- `resmart_parse.py` discovers input via cwd glob `*.nnn` (`*.[0-9][0-9][0-9]`); there is **no directory argument**. To test, run from `resources\<date>\` (full path to the script) or copy a couple of `.nnn` files to a temp dir.
- Reading is single-pass and streaming: the `-o` file is created immediately, rows are written as packets stream in, and `-d` skips/filters during the read. The full dump (~500 MB / ~2 M packets) takes seconds for one day (`-a -d` ~2 s) and ~15 s uncompressed default.
- Running with no arguments prints the CLI help to stderr and exits (code 2) — it reads or writes nothing. `RESmart_data.csv` (the default `-o` output file) is written only when output flags are passed; `--info` is read-only and never writes the CSV.
- The CSV always has a header row naming every column; the first column is an ISO 8601 `timestamp` (`2026-07-21T23:59:45`). Known fields carry their unit in the header (e.g. `IPAP (0.5 cmH2O)`), driven by `packet.known_units`.
- Regression contract: output must stay byte-identical for the same input/flags. After changing row/header generation, regenerate and hash the 8 mode variants (default, `-y`, `-s`, `-a`, `-2`, `-1`, `-a -y`, `-d` range) against the previous run.
- Requires Python 3 (hard-exits otherwise at module top, before arg parsing).

## Language / git

- Keep the project and all its documentation in English: code comments, CLI help and error messages, README, and this file. Do not translate existing content to another language.
- Commit and push messages (and PRs, if any) must also be written in English.

## Gotchas

- `--time_seconds` help text says "since beginning of year" and README says "month"—neither is right; it's seconds from the packet date ordinal. Treat the number as opaque.
- Field indexes: code reads 106 words (`packet.dlen`) + 8-byte timestamp; `known_fields` in `packet.setup_labels` is the source of truth. The README address table is loose guesswork and does not match the code exactly.
- `graph_data.py` is an unfinished placeholder GUI that plots random data — it does not read RESmart data yet.
- `resources\` holds a real patient data dump (large, ~500 MB) of sensitive medical data. It is gitignored — never commit it or any `*.nnn/.usr/.log/.evt/.idx` data.