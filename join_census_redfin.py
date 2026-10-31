"""Join the latest Census ZIP vintage to one Redfin month.

The Census file is the vintage just loaded: one `acs_year`. The Redfin file
for the chained run has only that calendar year. The join keeps the latest
`month_date_yyyymm` in that year. A ZIP is kept only when `postal_code`
equals `zip`. The grain is one row per ZIP.

The Redfin file covers all of California. Right after it is read (only the
load columns, in pandas), rows outside the practice area are dropped: the
Redfin row's practice_zip_code must be the practice ZIP in practice.py, its
county must be Los Angeles or Orange, and distance_from_practice_miles must
be 50 or less. A Redfin file without those fields is refused.

Prices and census values are copied from those rows. A blank cell stays
blank. This script does not fill a price from an earlier month. A Census
file from before the race, ethnicity, and education columns were added is
still read, and those columns stay blank.

The CSV is `zip_census_redfin_join.csv`. When DATABASE_URL is set, those
rows replace `public.zip_census_redfin_join`. That replace does not delete
`public.redfin_real_estate_value`. Every row in that replace gets the same
dataset_updated_at.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

import dataset_stamp
import load_california
import practice
import pull_census_zip_population as census

TABLE_NAME = "zip_census_redfin_join"
TABLE = "public.zip_census_redfin_join"
DELETE_JOIN_SQL = f"DELETE FROM {TABLE}"
OUTPUT_CSV_NAME = "zip_census_redfin_join.csv"
OUTPUT_CSV = Path(__file__).resolve().parent / OUTPUT_CSV_NAME
JOIN_COLUMNS = census.COLUMNS + load_california.LOAD_COLUMNS
INTEGER_JOIN_COLUMNS = {
    "acs_year",
    "median_household_income",
    "per_capita_income",
    "male_population",
    "female_population",
    "adult_population",
    "population_18_34",
    "population_35_44",
    "population_45_54",
    "population_55_64",
    "population_65_plus",
    *census.ADDED_COLUMNS,
    "active_listing_count",
}
TEXT_JOIN_COLUMNS = {
    "zip",
    "month_date_yyyymm",
    "postal_code",
    "zip_name",
    "date_pulled_yyyymmdd",
    "practice_zip_code",
    "county",
}
NOT_NULL_JOIN_COLUMNS = {
    "acs_year",
    "zip",
    "month_date_yyyymm",
    "postal_code",
    "zip_name",
    "date_pulled_yyyymmdd",
}

if set(census.COLUMNS) & set(load_california.LOAD_COLUMNS):
    raise SystemExit("Census and Redfin columns overlap.")
if "DELETE" in DELETE_JOIN_SQL.upper() and "redfin_real_estate_value" in DELETE_JOIN_SQL:
    raise SystemExit("The join refresh must not delete Redfin rows.")


def quote_ident(name: str) -> str:
    if load_california.IDENT_RE.fullmatch(name) is None:
        raise SystemExit(f"Refusing unexpected column {name!r}.")
    return name


def column_sql_type(name: str) -> str:
    if name in INTEGER_JOIN_COLUMNS:
        return "integer"
    if name in TEXT_JOIN_COLUMNS:
        return "text"
    return "numeric"


def read_census_csv(path: Path) -> list[dict[str, str]]:
    rows = read_csv(path, census.BASE_COLUMNS)
    for row in rows:
        for name in census.ADDED_COLUMNS:
            row.setdefault(name, "")
    return rows


def read_csv(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"{path} is missing.")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in required if name not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path.name} is missing columns: {missing}")
        rows: list[dict[str, str]] = []
        for raw in reader:
            rows.append(
                {
                    key: "" if value is None else value
                    for key, value in raw.items()
                    if key is not None
                }
            )
        return rows


def eligible_mask(frame: pd.DataFrame) -> pd.Series:
    """Rows whose stored practice fields put them inside the practice area."""
    miles = pd.to_numeric(frame["distance_from_practice_miles"].str.strip(), errors="coerce")
    return (
        (frame["practice_zip_code"].str.strip() == practice.PRACTICE_ZIP_CODE)
        & frame["county"].str.strip().isin(list(practice.COUNTIES.values()))
        & (miles <= float(practice.MAX_PRACTICE_DISTANCE_MILES))
    )


def read_redfin_csv(path: Path) -> list[dict[str, str]]:
    """Read the statewide Redfin CSV and keep only eligible rows, in pandas."""
    if not path.is_file():
        raise SystemExit(f"{path} is missing.")
    header = pd.read_csv(path, nrows=0).columns
    missing = [name for name in load_california.LOAD_COLUMNS if name not in header]
    if missing:
        raise SystemExit(f"{path.name} is missing columns: {missing}")
    frame = pd.read_csv(
        path, usecols=list(load_california.LOAD_COLUMNS), dtype=str, keep_default_na=False
    )
    keep = eligible_mask(frame)
    beyond = frame.loc[~keep, "postal_code"].nunique()
    print(
        f"Redfin file: {frame['postal_code'].nunique()} California ZIPs. Kept "
        f"{frame.loc[keep, 'postal_code'].nunique()} within {practice.MAX_PRACTICE_DISTANCE_MILES} miles "
        f"of {practice.PRACTICE_ZIP_CODE} in {' or '.join(practice.COUNTIES.values())} County; "
        f"dropped {beyond} before the join."
    )
    return frame[keep].to_dict(orient="records")


def loaded_acs_year(rows: list[dict[str, str]]) -> int:
    """The single Census vintage in the file that was just loaded."""
    if not rows:
        raise SystemExit("The Census file has no rows.")
    years: set[int] = set()
    for row in rows:
        raw = (row.get("acs_year") or "").strip()
        years.add(load_california.parse_census_year(raw))
    if len(years) != 1:
        listed = ", ".join(str(year) for year in sorted(years))
        raise SystemExit(f"The Census file has more than one acs_year: {listed}.")
    return next(iter(years))


def latest_redfin_month(rows: list[dict[str, str]], year: int) -> str:
    """Latest `yyyy_mm` in `year`. Other years are ignored."""
    months = {
        (row.get("month_date_yyyymm") or "").strip()
        for row in rows
        if load_california.in_census_year(
            (row.get("month_date_yyyymm") or "").strip(),
            year,
        )
    }
    if not months:
        raise SystemExit(f"The Redfin file has no month in Census year {year}.")
    return max(months)


def cell(row: dict[str, str], column: str) -> str:
    if column not in row:
        raise SystemExit(f"A join row is missing {column}.")
    value = row[column]
    if value is None:
        return ""
    return str(value).strip()


def join_records(
    census_rows: list[dict[str, str]],
    redfin_rows: list[dict[str, str]],
) -> tuple[int, str, list[dict[str, str]]]:
    """Inner join on ZIP for the latest Redfin month of the Census year.

    One row per ZIP. A price from an earlier month is not copied forward.
    """
    year = loaded_acs_year(census_rows)
    month = latest_redfin_month(redfin_rows, year)
    census_by_zip: dict[str, dict[str, str]] = {}
    for row in census_rows:
        if cell(row, "acs_year") != str(year):
            continue
        key = census.normalize_zip(cell(row, "zip"))
        if not key:
            raise SystemExit("The Census file has a row without a ZIP.")
        if key in census_by_zip:
            raise SystemExit(f"Census has more than one row for ZIP {key} in {year}.")
        census_by_zip[key] = row
    redfin_by_zip: dict[str, dict[str, str]] = {}
    month_rows = []
    for row in redfin_rows:
        if cell(row, "month_date_yyyymm") != month:
            continue
        if not load_california.postal_code_key(cell(row, "postal_code")):
            raise SystemExit(f"Redfin month {month} has a row without a ZIP.")
        month_rows.append(row)
    kept, far, _other = practice.split_by_area(
        month_rows, lambda row: tuple(cell(row, name) for name in practice.FIELDS)
    )
    print(
        f"Excluded {far} Redfin ZIPs more than {practice.MAX_PRACTICE_DISTANCE_MILES} miles "
        f"from the practice ZIP {practice.PRACTICE_ZIP_CODE} before the join."
    )
    for row in kept:
        key = load_california.postal_code_key(cell(row, "postal_code"))
        if key in redfin_by_zip:
            raise SystemExit(
                f"Redfin has more than one row for ZIP {key} in {month}."
            )
        redfin_by_zip[key] = row
    joined: list[dict[str, str]] = []
    for key in sorted(set(census_by_zip) & set(redfin_by_zip)):
        left = census_by_zip[key]
        right = redfin_by_zip[key]
        record = {column: cell(left, column) for column in census.COLUMNS}
        for column in load_california.LOAD_COLUMNS:
            record[column] = cell(right, column)
        record["acs_year"] = str(year)
        record["zip"] = key
        record["postal_code"] = key
        record["month_date_yyyymm"] = month
        if record["zip"] != record["postal_code"]:
            raise SystemExit("The join keys differ.")
        joined.append(record)
    if not joined:
        raise SystemExit(
            f"No ZIP was in both Census {year} and Redfin {month}."
        )
    if len(joined) != len({row["zip"] for row in joined}):
        raise SystemExit("The join has more than one row for a ZIP.")
    return year, month, joined


def write_join_csv(records: list[dict[str, str]], path: Path = OUTPUT_CSV) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=JOIN_COLUMNS,
                lineterminator="\n",
                extrasaction="raise",
            )
            writer.writeheader()
            for record in records:
                writer.writerow({column: record[column] for column in JOIN_COLUMNS})
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def db_value(column: str, raw: str):
    """Store a source cell. Blank measures stay null. Nothing is filled in."""
    if column in TEXT_JOIN_COLUMNS:
        return "" if raw is None else str(raw).strip()
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return None
    if column in INTEGER_JOIN_COLUMNS:
        if text.startswith("-"):
            number = text[1:]
            sign = -1
        else:
            number = text
            sign = 1
        if not number.isdigit():
            raise SystemExit(f"{column} is not a whole number.")
        return sign * int(number)
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise SystemExit(f"{column} is not a number.") from exc


def schema_sql() -> str:
    lines = []
    for name in JOIN_COLUMNS:
        ident = quote_ident(name)
        null_sql = " NOT NULL" if name in NOT_NULL_JOIN_COLUMNS else ""
        lines.append(f"    {ident} {column_sql_type(name)}{null_sql}")
    lines.append(f"    {quote_ident(dataset_stamp.COLUMN)} {dataset_stamp.SQL_TYPE}")
    body = ",\n".join(lines)
    return (
        f"CREATE TABLE IF NOT EXISTS {TABLE} (\n"
        f"{body},\n"
        "    PRIMARY KEY (zip)\n"
        ");\n"
    )


def replace_join_table(
    conn, records: list[dict[str, str]], updated_at: datetime | None = None
) -> int:
    """Replace the join snapshot. Do not delete Redfin months."""
    if "redfin_real_estate_value" in DELETE_JOIN_SQL:
        raise SystemExit("The join refresh must not delete Redfin rows.")
    updated_at = dataset_stamp.stamp(updated_at)
    stored = JOIN_COLUMNS + (dataset_stamp.COLUMN,)
    column_list = ", ".join(quote_ident(name) for name in stored)
    placeholders = ", ".join(["%s"] * len(stored))
    insert_sql = f"INSERT INTO {TABLE} ({column_list}) VALUES ({placeholders})"
    _updated_at, rows = dataset_stamp.stamp_rows(
        [
            tuple(db_value(name, record[name]) for name in JOIN_COLUMNS)
            for record in records
        ],
        updated_at,
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(schema_sql())
            cur.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "
                f"{quote_ident(dataset_stamp.COLUMN)} {dataset_stamp.SQL_TYPE}"
            )
            for name in census.ADDED_COLUMNS + practice.FIELDS:
                cur.execute(
                    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "
                    f"{quote_ident(name)} {column_sql_type(name)}"
                )
            cur.execute(DELETE_JOIN_SQL)
            cur.executemany(insert_sql, rows)
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            count = cur.fetchone()[0]
    if count != len(records):
        raise SystemExit(f"{TABLE} has {count} rows after loading {len(records)}.")
    return count


def database_url() -> str:
    return os.environ.get(load_california.DATABASE_URL_ENV, "").strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--print-acs-year":
        year = loaded_acs_year(read_census_csv(Path(args[1])))
        print(year)
        return 0
    if len(args) != 2:
        raise SystemExit(
            "Usage: python join_census_redfin.py CENSUS_CSV REDFIN_CSV"
        )
    census_rows = read_census_csv(Path(args[0]))
    redfin_rows = read_redfin_csv(Path(args[1]))
    year, month, joined = join_records(census_rows, redfin_rows)
    write_join_csv(joined)
    print(
        f"Join grain: one row per ZIP. "
        f"Redfin month {month} is the latest month in Census year {year}. "
        f"Wrote {len(joined)} rows to {OUTPUT_CSV_NAME}."
    )
    if not database_url():
        print(f"{load_california.DATABASE_URL_ENV} is not set. Left {TABLE} unchanged.")
        return 0
    updated_at = dataset_stamp.stamp()
    print(f"dataset_updated_at: {updated_at.isoformat()}")
    with load_california.connect() as conn:
        count = replace_join_table(conn, joined, updated_at)
    print(
        f"Replaced {TABLE} with {count} rows. "
        f"{load_california.TABLE} was not modified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
