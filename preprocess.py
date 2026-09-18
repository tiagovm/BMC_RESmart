"""Preprocess a RESmart CSV export into a clean, analysis-ready DataFrame.

Turns the raw parser output (see resmart_parse.py) into a chronologically
ordered table: ISO 8601 timestamps become datetimes, the SD-card wrap-around
ordering is repaired, and invalid sensor reads (0xFFFF / 65535) become NaN.

Requires pandas. Not for medical use.
"""

import sys

import numpy as np
import pandas as pd

INVALID_VALUE = 65535


def clean_and_preprocess(caminho_csv):
    """Read, clean and return a RESmart CSV as a DataFrame.

    Steps:
      1. load the CSV as-is (the parser always writes a header row);
      2. strip the leading space the parser leaves on column names;
      3. parse the ISO 8601 ``timestamp`` column as datetime, dropping rows
         that fail to parse and reporting how many were dropped;
      4. sort ascending by ``timestamp`` (stable sort keeps same-second rows
         in file order) to repair the out-of-chronological-order output that
         results from the device's circular SD-card storage;
      5. replace the invalid-read sentinel 65535 (0xFFFF) with NaN in every
         numeric column, so invalid measurements do not skew stats or plots;
      6. reset the index and return the cleaned DataFrame.

    Duplicate timestamps (same second) are kept, as they can occur where the
    circular wrap overlaps. Timestamp with day precision is expected; higher
    sample-rate modes (-2 / -1) carry millisecond timestamps and also work.
    """
    df = pd.read_csv(caminho_csv, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]

    df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", errors="coerce")

    dropped = int(df["timestamp"].isna().sum())
    if dropped:
        df = df[df["timestamp"].notna()]

    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    numeric_cols = df.select_dtypes("number").columns
    df[numeric_cols] = df[numeric_cols].replace(INVALID_VALUE, np.nan)

    if dropped:
        print(f"dropped {dropped} row(s) with unparseable timestamps", file=sys.stderr)

    return df


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        print("usage: python preprocess.py out.csv", file=sys.stderr)
        return 2
    df = clean_and_preprocess(argv[0])
    print(f"{len(df):,} rows x {len(df.columns)} columns")
    print(df.info())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())