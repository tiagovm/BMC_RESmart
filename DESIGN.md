# DESIGN.md

Design documentation for the BMC RESmart GII parser.

- Status: reverse-engineered, unofficial, NOT for medical use.
- Targets: `resmart_parse.py` (parsing + CLI) and `graph_data.py` (incomplete GUI).
- Requirements on the toolchain are minimal: Python 3, standard library only.

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
  |     1 | Reslex      | (dimensionless, 1-5) |                     |
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
- **Default ISO timestamp column** with millisecond precision for high-rate
  subsample rows; `-y`/`-s` kept for backward compatibility.
- **Runtime safety:** Python 3 enforced at the top of `main()`; invalid/extra
  `-d` inputs and empty input directories fail through `parser.error`/clean
  message with non-zero exit codes.

## 5. Constraints

- Standard library only (`struct`, `argparse`, `glob`, `datetime`); no third-party
  dependencies; no build step, test framework, or CI.
- Input files are discovered from the **current working directory**; there is no
  directory argument (`scripts` invoked by path, `cwd` = data directory).
- Python 3 only.
- `resources\` contains real, sensitive patient data and is gitignored; it must
  never be committed.
- Project and all documentation stay in English (see `AGENTS.md`).

## 6. Known problems

- `-s`/`--time_seconds` help says "since beginning of year" and the README says
  "month"; both are wrong — the value derives from the packet's date ordinal.
  Treat it as opaque.
- Word meanings beyond the seven known fields are guesses; the `resA/B/C` and
  `pulse` arrays have no confirmed physical interpretation.
- Dump timestamps are not globally sorted (wrap file); the CSV preserves file
  order. `--info` can therefore list a date twice (two time segments).
- The final 256-byte packet of each file is skipped, losing up to 1 s per file.
- spO2/HR are only present when an oximeter is attached; invalid values are
  stored as `0xFFFF` and reported as-is.
- `graph_data.py` is an unfinished placeholder GUI that plots random data; it
  does not read RESmart data yet.
- High-rate modes (`-2`/`-1`) inflate output 25x/10x, producing large CSVs.
- Event/apnea detection is not implemented (neither the device nor this code
  performs it; the BMC analysis software does).
- No automated test suite; regressions are checked manually by hashing sample
  outputs against the previous version.

## 7. Feature roadmap

- `graph_data.py`: turn the placeholder into a real viewer that reads the CSV
  (daily hour strip chart, IPAP/EPAP/flow traces, spO2 overlay).
- Optional unit conversion flag (e.g. report IPAP/EPAP in cmH2O instead of raw
  0.5-cmH2O words).
- Optional global time sorting of the output to flatten the wrap-file ordering.
- Parse the adjacent `.usr`, `.evt`, `.idx`, `.log` files produced by the BMC
  software.
- Confirm the true meaning of words 4-88 and 89-98 (and the pads).
- Add a small smoke-test suite (synthetic packets) and run it on push.
- Progress reporting with ETA for very long runs (already bounded in memory).