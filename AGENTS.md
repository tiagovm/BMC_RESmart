# AGENTS.md

Python 3 tools that decode raw SD-card data dumps from a BMC RESmart GII CPAP machine. Reverse-engineered, unofficial, not for medical use. See `README.md` for the packet-format spec.

## Run / verify

No build system, deps, tests, or linting. Standard library only (struct/glob/argparse/datetime). Verify changes by running against the sample dump:

```
python resmart_parse.py -i -q        # run while cwd = dir containing the data files
```

- `resmart_parse.py` discovers input via cwd glob `*.nnn` (`*.[0-9][0-9][0-9]`); there is **no directory argument**. To test, run from `resources\<date>\` (full path to the script) or copy a couple of `.nnn` files to a temp dir.
- Running with no arguments prints the CLI help to stderr and exits (code 2) — it reads or writes nothing. `RESmart_data.csv` (the default `-o` output file) is written only when output flags are passed; `--info` is read-only and never writes the CSV.
- The CSV always has a header row naming every column; the first column is an ISO 8601 `timestamp` (`2026-07-21T23:59:45`). Known fields carry their unit in the header (e.g. `IPAP (0.5 cmH2O)`), driven by `packet.known_units`.
- Requires Python 3 (hard-exits otherwise at module top, before arg parsing).

## Language / git

- Keep the project and all its documentation in English: code comments, CLI help and error messages, README, and this file. Do not translate existing content to another language.
- Commit and push messages (and PRs, if any) must also be written in English.

## Gotchas

- `--time_seconds` help text says "since beginning of year" and README says "month"—neither is right; it's seconds from the packet date ordinal. Treat the number as opaque.
- Field indexes: code reads 106 words (`packet.dlen`) + 8-byte timestamp; `known_fields` in `packet.setup_labels` is the source of truth. The README address table is loose guesswork and does not match the code exactly.
- `graph_data.py` is an unfinished placeholder GUI that plots random data — it does not read RESmart data yet.
- `resources\` holds a real patient data dump (large, ~500 MB) of sensitive medical data. It is gitignored — never commit it or any `*.nnn/.usr/.log/.evt/.idx` data.