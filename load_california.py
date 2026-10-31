"""Load California ZIP months from Realtor.com inventory history.

The research page at https://www.realtor.com/research/data/ publishes the
Monthly Housing Inventory downloads. This script reads the Zip row's
Historical Data href and loads that CSV. It does not read values out of the
page HTML, and it does not use the current-month snapshot.

The file is already one row per ZIP per month and covers every state. A row
is kept when its zip_name contains ", ca" (any case) or its ZIP is a
California ZCTA, so every California ZIP is stored, near the practice or not. pandas reads only KEPT_COLUMNS in chunks
and does the filtering. Every row also gets practice_zip_code, county, and
distance_from_practice_miles from practice.california_locations(). county is
Los Angeles or Orange, or blank for any other county, and the distance is
blank for a ZIP with no Census Gazetteer point. The 50-mile rule is not
applied here; the downstream datasets apply it.

A scheduled or manual run replaces public.redfin_real_estate_value. Existing
rows in that table are deleted, then the current extract is inserted, so a
ZIP or month that left the file does not stay in the table. No other table
is dropped or altered. The same extract is written to
california_zip_inventory.csv, overwriting that file. The filename does not
change from run to run.

python load_california.py --census-year YYYY keeps only months in that
calendar year. Those rows are inserted or updated. Months already stored
are not deleted, and their practice fields are brought up to date. The CSV
from that run contains that year only. The chained
Census workflow uses this so older Redfin months stay in Supabase.

Every row this run inserts or updates gets the same dataset_updated_at.
Months the census-year load does not write keep the timestamp they had.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import dataset_stamp
import practice

RESEARCH_PAGE = "https://www.realtor.com/research/data/"
DATABASE_URL_ENV = "DATABASE_URL"
BATCH_ROWS = 20_000
READ_CHUNK_ROWS = 250_000
TABLE_NAME = "redfin_real_estate_value"
TABLE = "public.redfin_real_estate_value"
# Census Bureau 2020 ZIP Code Tabulation Area to county relationship file.
# https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt
CROSSWALK_URL = practice.CROSSWALK_URL
OUTPUT_CSV_NAME = "california_zip_inventory.csv"
OUTPUT_CSV = Path(__file__).resolve().parent / OUTPUT_CSV_NAME
PACIFIC = ZoneInfo("America/Los_Angeles")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Source columns kept from the ZIP history file, in load order.
# date_pulled_yyyymmdd is added by this script; it is not a column in the CSV.
KEPT_COLUMNS = (
    "month_date_yyyymm",
    "postal_code",
    "zip_name",
    "median_listing_price",
    "median_listing_price_mm",
    "median_listing_price_yy",
    "active_listing_count",
    "median_listing_price_per_square_foot",
    "median_listing_price_per_square_foot_mm",
    "median_listing_price_per_square_foot_yy",
)
MEASURE_COLUMNS = KEPT_COLUMNS[3:]
LOAD_COLUMNS = KEPT_COLUMNS + ("date_pulled_yyyymmdd",) + practice.FIELDS
# Stored with each load. Not part of the Realtor CSV or the join's source columns.
STORED_COLUMNS = LOAD_COLUMNS + (dataset_stamp.COLUMN,)
STAGING_COLUMNS = STORED_COLUMNS + ("load_seq",)
IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*")

COLUMN_TYPES = {
    "month_date_yyyymm": "text",
    "postal_code": "text",
    "zip_name": "text",
    "median_listing_price": "numeric",
    "median_listing_price_mm": "numeric",
    "median_listing_price_yy": "numeric",
    "active_listing_count": "integer",
    "median_listing_price_per_square_foot": "numeric",
    "median_listing_price_per_square_foot_mm": "numeric",
    "median_listing_price_per_square_foot_yy": "numeric",
    "date_pulled_yyyymmdd": "text",
    "practice_zip_code": "text",
    "county": "text",
    "distance_from_practice_miles": "numeric",
    dataset_stamp.COLUMN: dataset_stamp.SQL_TYPE,
}
NOT_NULL_COLUMNS = {
    "month_date_yyyymm",
    "postal_code",
    "zip_name",
    "date_pulled_yyyymmdd",
}
UDT_FOR_NEW = {
    "text": "text",
    "integer": "int4",
    "numeric": "numeric",
    "timestamptz": "timestamptz",
}
INTEGER_UDT = {"int2", "int4", "int8"}
REAL_UDT = {"numeric", "float4", "float8", "int2", "int4", "int8"}

ZIP_ROW_RE = re.compile(r"<th>\s*Zip:\s*</th>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"""href=["']([^"']+\.csv)["']""", re.IGNORECASE)


def fetch_text(url: str, timeout: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def historical_zip_csv_url(page: str) -> str:
    """Return the Monthly Housing Inventory Zip href in the Historical Data column.

    On the research page that table's columns are Current Month Data, then
    Historical Data. The current-month file is the first Zip link. This load
    uses the second.
    """
    lower = page.lower()
    start = lower.find("monthly housing inventory")
    if start < 0:
        raise SystemExit(
            f"{RESEARCH_PAGE} has no Monthly Housing Inventory section."
        )
    end = lower.find("monthly market hotness", start)
    section = page[start : end if end > start else None]
    match = ZIP_ROW_RE.search(section)
    if match is None:
        raise SystemExit(
            "Monthly Housing Inventory has no Zip row with a CSV href."
        )
    hrefs = [html.unescape(href) for href in HREF_RE.findall(match.group(1))]
    if len(hrefs) < 2:
        raise SystemExit(
            "The Zip row did not include a Historical Data CSV href "
            f"(found {len(hrefs)} csv link(s))."
        )
    history = hrefs[1]
    if history == hrefs[0]:
        raise SystemExit(
            "The Zip Historical Data href is the same as the current-month file."
        )
    return history


def discover_history_url() -> str:
    try:
        page = fetch_text(RESEARCH_PAGE, timeout=60)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Could not read {RESEARCH_PAGE} (HTTP {exc.code}). "
            "The Zip history file is the Historical Data link on that page."
        ) from exc
    return historical_zip_csv_url(page)


def parse_number(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_count(raw: str | None) -> int | None:
    number = parse_number(raw)
    if number is None:
        return None
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


def month_text(yyyymm: str) -> str | None:
    """Turn a source month such as 202609 into text 2026_09."""
    if len(yyyymm) != 6 or not yyyymm.isdigit():
        return None
    year = int(yyyymm[:4])
    month = int(yyyymm[4:6])
    if year < 1 or not 1 <= month <= 12:
        return None
    return f"{year:04d}_{month:02d}"


def in_census_year(month: str, year: int) -> bool:
    """True when `month` is `yyyy_mm` in the Census calendar year."""
    if isinstance(year, bool) or not isinstance(year, int):
        return False
    if len(month) != 7 or month[4] != "_":
        return False
    if month[:4] != f"{year:04d}" or not month[5:].isdigit():
        return False
    return 1 <= int(month[5:]) <= 12


def parse_census_year(raw: str) -> int:
    text = raw.strip()
    if len(text) != 4 or not text.isdigit():
        raise SystemExit(
            "The Census acs_year must be a four-digit year. "
            f"Got {raw!r}."
        )
    year = int(text)
    if year < 2009 or year > 2100:
        raise SystemExit(f"The Census acs_year {year} is outside 2009-2100.")
    return year


def census_year_arg(argv: list[str]) -> int | None:
    """None means the full replace. A year means upsert that year only."""
    if len(argv) == 1:
        return None
    if len(argv) == 3 and argv[1] == "--census-year":
        return parse_census_year(argv[2])
    raise SystemExit("Usage: python load_california.py [--census-year YYYY]")


def census_year_month_pattern(year: int) -> str:
    """Postgres regex for `yyyy_mm` months in one calendar year."""
    if isinstance(year, bool) or not isinstance(year, int):
        raise SystemExit("The Census year is not an integer.")
    return f"^{year:04d}_(0[1-9]|1[0-2])$"


def census_year_upsert_sql() -> str:
    """Insert or update staged rows. The statement does not delete anything."""
    columns = ", ".join(STORED_COLUMNS)
    assignments = ", ".join(
        f"{name} = EXCLUDED.{name}"
        for name in STORED_COLUMNS
        if name not in {"postal_code", "month_date_yyyymm"}
    )
    sql = (
        f"INSERT INTO {TABLE} ({columns}) "
        "SELECT DISTINCT ON (postal_code, month_date_yyyymm) "
        f"{columns} "
        "FROM staging_redfin_real_estate_value "
        "ORDER BY postal_code, month_date_yyyymm, load_seq DESC "
        "ON CONFLICT (postal_code, month_date_yyyymm) DO UPDATE SET "
        f"{assignments}"
    )
    if "DELETE" in sql.upper():
        raise SystemExit("The census-year Redfin load must not delete rows.")
    return sql


def pull_date_text(moment: datetime | None = None) -> str:
    """Calendar date of the run in America/Los_Angeles, as yyyy_mm_dd text."""
    current = moment if moment is not None else datetime.now(PACIFIC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=PACIFIC)
    else:
        current = current.astimezone(PACIFIC)
    return current.strftime("%Y_%m_%d")


def postal_code_key(raw: str | None) -> str | None:
    """Return the five-digit ZIP or ZCTA key, without using the place name."""
    text = (raw or "").strip()
    digits: list[str] = []
    for char in text:
        if char.isdigit():
            digits.append(char)
        elif char == "-" and len(digits) == 5:
            break
        else:
            return None
    if len(digits) not in (5, 9):
        return None
    return "".join(digits[:5])


def require_header(fieldnames: list[str] | None) -> list[str]:
    names = [name.strip() for name in (fieldnames or [])]
    missing = [column for column in KEPT_COLUMNS if column not in names]
    if missing:
        raise SystemExit(f"ZIP history CSV is missing columns: {missing}")
    return names


def is_california_zip(zip_names: pd.Series) -> pd.Series:
    """True when zip_name contains ", ca" (any case), the statewide rule this table has always used."""
    return zip_names.str.contains(", ca", case=False, regex=False, na=False)


def number_text(raw: pd.Series) -> pd.Series:
    """Numeric text with commas removed, or None when the cell is blank or not a finite number."""
    text = raw.str.strip().str.replace(",", "", regex=False)
    values = pd.to_numeric(text, errors="coerce")
    return text.where(np.isfinite(values), None)


def count_values(raw: pd.Series) -> pd.Series:
    """Whole counts, rounding halves away from zero like parse_count."""
    values = pd.to_numeric(raw.str.strip().str.replace(",", "", regex=False), errors="coerce")
    rounded = np.sign(values) * np.floor(np.abs(values) + 0.5)
    return rounded.astype("Int64").astype(object).where(rounded.notna(), None)


def california_frame(
    chunk: pd.DataFrame,
    pull_date: str,
    locations: dict[str, practice.AreaZip] | pd.DataFrame,
    census_year: int | None = None,
) -> pd.DataFrame:
    """Keep California rows of one source chunk and add the practice fields, all in pandas.

    A row is California when zip_name contains ", ca" or its ZIP is a California
    ZCTA, so a California ZIP with a blank zip_name is still kept.
    """
    lookup = locations if isinstance(locations, pd.DataFrame) else location_lookup(locations)
    postal = chunk["postal_code"].str.strip().str.extract(r"^(\d{5})(?:\d{4}|-.*)?$", expand=False)
    in_state = is_california_zip(chunk["zip_name"]) | postal.isin(lookup.index)
    chunk, postal = chunk[in_state], postal[in_state]
    months = chunk["month_date_yyyymm"].str.strip().str.extract(r"^(\d{4})(\d{2})$")
    year = pd.to_numeric(months[0], errors="coerce")
    month = pd.to_numeric(months[1], errors="coerce")
    keep = postal.notna() & (year >= 1) & month.between(1, 12)
    if census_year is not None:
        keep &= year == census_year
    chunk, postal, months = chunk[keep], postal[keep], months[keep]
    frame = pd.DataFrame(
        {
            "month_date_yyyymm": months[0] + "_" + months[1],
            "postal_code": postal,
            "zip_name": chunk["zip_name"].str.strip(),
        }
    )
    for column in MEASURE_COLUMNS:
        if column == "active_listing_count":
            frame[column] = count_values(chunk[column])
        else:
            frame[column] = number_text(chunk[column])
    frame["date_pulled_yyyymmdd"] = pull_date
    fields = lookup.reindex(postal.to_numpy())
    frame["practice_zip_code"] = practice.PRACTICE_ZIP_CODE
    frame["county"] = fields["county"].to_numpy()
    frame["distance_from_practice_miles"] = fields["distance_from_practice_miles"].to_numpy()
    return frame.astype(object).where(frame.notna(), None)


def location_lookup(locations: dict[str, practice.AreaZip]) -> pd.DataFrame:
    """ZIP-indexed county and distance. A county outside COUNTIES is stored blank."""
    return pd.DataFrame(
        {
            "county": [place.county or None for place in locations.values()],
            "distance_from_practice_miles": [place.distance_from_practice_miles for place in locations.values()],
        },
        index=list(locations),
        dtype=object,
    )


def read_california_rows(
    handle,
    pull_date: str,
    locations: dict[str, practice.AreaZip],
    census_year: int | None = None,
    chunk_rows: int = READ_CHUNK_ROWS,
):
    """Yield lists of California load rows. Only KEPT_COLUMNS are read from the file."""
    chunks = pd.read_csv(
        handle,
        usecols=lambda name: name.strip() in KEPT_COLUMNS,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
        chunksize=chunk_rows,
    )
    lookup = location_lookup(locations)
    first = True
    for chunk in chunks:
        chunk.columns = [name.strip() for name in chunk.columns]
        if first:
            require_header(list(chunk.columns))
            first = False
        frame = california_frame(chunk, pull_date, lookup, census_year)
        if not frame.empty:
            yield list(frame[list(LOAD_COLUMNS)].itertuples(index=False, name=None))


def open_csv(url: str):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=600)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"ZIP history CSV returned HTTP {exc.code}: {url}"
        ) from exc
    head = response.peek(256) if hasattr(response, "peek") else b""
    if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"<"):
        response.close()
        raise SystemExit(f"Expected a CSV from {url}")
    return response


def schema_sql() -> str:
    lines = []
    for name in STORED_COLUMNS:
        null_sql = " NOT NULL" if name in NOT_NULL_COLUMNS else ""
        lines.append(f"    {name} {COLUMN_TYPES[name]}{null_sql}")
    body = ",\n".join(lines)
    return f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
{body},
    PRIMARY KEY (postal_code, month_date_yyyymm)
);
"""


def csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def alter_type(cur, column: str, sql_type: str, using: str) -> None:
    if IDENT_RE.fullmatch(column) is None:
        raise SystemExit(f"Refusing to alter unexpected column {column!r}.")
    cur.execute(
        f"ALTER TABLE {TABLE} ALTER COLUMN {column} TYPE {sql_type} USING {using}"
    )


def migrate_month(cur, udt: str) -> None:
    if udt == "date":
        alter_type(
            cur,
            "month_date_yyyymm",
            "text",
            "to_char(month_date_yyyymm, 'YYYY_MM')",
        )
        return
    if udt in REAL_UDT or udt in INTEGER_UDT:
        alter_type(
            cur,
            "month_date_yyyymm",
            "text",
            "substr(lpad(trunc(month_date_yyyymm::numeric)::text, 6, '0'), 1, 4) "
            "|| '_' || "
            "substr(lpad(trunc(month_date_yyyymm::numeric)::text, 6, '0'), 5, 2)",
        )
        return
    if udt == "varchar":
        alter_type(cur, "month_date_yyyymm", "text", "month_date_yyyymm::text")
    elif udt != "text":
        raise SystemExit(
            f"Cannot convert month_date_yyyymm from {udt} to text."
        )
    cur.execute(
        f"""
        UPDATE {TABLE}
        SET month_date_yyyymm = CASE
            WHEN char_length(month_date_yyyymm) = 6
             AND month_date_yyyymm ~ '^[0-9]{{6}}' THEN
                substr(month_date_yyyymm, 1, 4) || '_' || substr(month_date_yyyymm, 5, 2)
            WHEN char_length(month_date_yyyymm) = 10
             AND month_date_yyyymm ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN
                substr(month_date_yyyymm, 1, 4) || '_' || substr(month_date_yyyymm, 6, 2)
            ELSE month_date_yyyymm
        END
        WHERE (char_length(month_date_yyyymm) = 6 AND month_date_yyyymm ~ '^[0-9]{{6}}')
           OR (char_length(month_date_yyyymm) = 10
               AND month_date_yyyymm ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}')
        """
    )


def migrate_count(cur, udt: str) -> None:
    if udt == "int4":
        return
    if udt in INTEGER_UDT or udt in {"numeric", "float4", "float8"}:
        using = "ROUND(active_listing_count::numeric)::integer"
    elif udt == "money":
        using = "ROUND(active_listing_count::numeric)::integer"
    elif udt in {"text", "varchar"}:
        using = """
            CASE
                WHEN active_listing_count IS NULL OR btrim(active_listing_count) = '' THEN NULL
                ELSE ROUND(replace(active_listing_count, ',', '')::numeric)::integer
            END
        """
    else:
        raise SystemExit(
            f"Cannot convert active_listing_count from {udt} to integer."
        )
    alter_type(cur, "active_listing_count", "integer", using)


def migrate_numeric(cur, column: str, udt: str) -> None:
    if udt == "numeric":
        return
    if udt in REAL_UDT or udt in INTEGER_UDT or udt == "money":
        using = f"{column}::numeric"
    elif udt in {"text", "varchar"}:
        using = f"""
            CASE
                WHEN {column} IS NULL OR btrim({column}) = '' THEN NULL
                ELSE replace({column}, ',', '')::numeric
            END
        """
    else:
        raise SystemExit(f"Cannot convert {column} from {udt} to numeric.")
    alter_type(cur, column, "numeric", using)


def migrate_timestamptz(cur, column: str, udt: str) -> None:
    if udt == "timestamptz":
        return
    if udt == "timestamp":
        alter_type(cur, column, "timestamptz", f"{column} AT TIME ZONE 'UTC'")
        return
    raise SystemExit(f"Cannot convert {column} from {udt} to timestamptz.")


def migrate_text_column(cur, column: str, udt: str) -> None:
    if udt == "text":
        return
    if udt == "varchar":
        alter_type(cur, column, "text", f"{column}::text")
        return
    if column != "date_pulled_yyyymmdd":
        raise SystemExit(f"Cannot convert {column} from {udt} to text.")
    if udt == "date":
        using = f"to_char({column}, 'YYYY_MM_DD')"
    elif udt == "timestamptz":
        using = (
            f"to_char({column} AT TIME ZONE 'America/Los_Angeles', 'YYYY_MM_DD')"
        )
    elif udt == "timestamp":
        using = f"to_char({column}, 'YYYY_MM_DD')"
    else:
        raise SystemExit(f"Cannot convert {column} from {udt} to text.")
    alter_type(cur, column, "text", using)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(schema_sql())
        cur.execute(
            """
            SELECT column_name, udt_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            (TABLE_NAME,),
        )
        found = {name: udt for name, udt in cur.fetchall()}
        for name in list(found):
            if name in STORED_COLUMNS:
                continue
            if IDENT_RE.fullmatch(name) is None:
                raise SystemExit(f"Refusing to drop unexpected column {name!r}.")
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {name}")
            del found[name]
        for name in STORED_COLUMNS:
            if name in found:
                continue
            cur.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN {name} {COLUMN_TYPES[name]}"
            )
            found[name] = UDT_FOR_NEW[COLUMN_TYPES[name]]
        migrate_month(cur, found["month_date_yyyymm"])
        migrate_count(cur, found["active_listing_count"])
        for name, sql_type in COLUMN_TYPES.items():
            if sql_type == "numeric":
                migrate_numeric(cur, name, found[name])
        for name in ("postal_code", "zip_name", "date_pulled_yyyymmdd"):
            migrate_text_column(cur, name, found[name])
        migrate_timestamptz(cur, dataset_stamp.COLUMN, found[dataset_stamp.COLUMN])


def prepare_staging(conn) -> None:
    typed = ",\n".join(
        f"    {name} {COLUMN_TYPES[name]}" for name in STORED_COLUMNS
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TEMP TABLE IF NOT EXISTS staging_redfin_real_estate_value (
{typed},
                    load_seq bigint NOT NULL
                )
                """
            )
            cur.execute("DELETE FROM staging_redfin_real_estate_value")


def copy_batch(conn, rows: list[tuple]) -> None:
    column_list = ", ".join(STAGING_COLUMNS)
    copy_sql = f"COPY staging_redfin_real_estate_value ({column_list}) FROM STDIN"
    with conn.transaction():
        with conn.cursor() as cur:
            with cur.copy(copy_sql) as copy:
                for row in rows:
                    copy.write_row(row)


def replace_table(conn) -> int:
    """Delete every current row, insert the staged extract, and overwrite the CSV."""
    tmp = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".tmp")
    stored = ", ".join(STORED_COLUMNS)
    columns = ", ".join(LOAD_COLUMNS)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {TABLE}")
                cur.execute(
                    f"""
                    INSERT INTO {TABLE} ({stored})
                    SELECT DISTINCT ON (postal_code, month_date_yyyymm)
                        {stored}
                    FROM staging_redfin_real_estate_value
                    ORDER BY postal_code, month_date_yyyymm, load_seq DESC
                    """
                )
                cur.execute(
                    f"""
                    ALTER TABLE {TABLE}
                    ALTER COLUMN date_pulled_yyyymmdd SET NOT NULL
                    """
                )
            with tmp.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(LOAD_COLUMNS)
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT {columns}
                        FROM {TABLE}
                        ORDER BY postal_code, month_date_yyyymm
                        """
                    )
                    while True:
                        chunk = cur.fetchmany(BATCH_ROWS)
                        if not chunk:
                            break
                        for row in chunk:
                            writer.writerow([csv_value(value) for value in row])
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
                count = cur.fetchone()[0]
        os.replace(tmp, OUTPUT_CSV)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return count


def refresh_practice_fields(cur, locations: dict[str, practice.AreaZip], updated_at: datetime) -> int:
    """Bring the practice fields of every stored month up to date. Nothing is deleted.

    One UPDATE covers every ZIP: known ZIPs get their county and distance, and a
    stored ZIP with no Gazetteer point gets a blank county and distance.
    """
    codes = sorted(locations)
    counties = [locations[code].county or None for code in codes]
    miles = [locations[code].distance_from_practice_miles for code in codes]
    cur.execute(
        f"""
        UPDATE {TABLE} AS stored
        SET practice_zip_code = %s, county = fields.county,
            distance_from_practice_miles = fields.miles, {dataset_stamp.COLUMN} = %s
        FROM (
            SELECT DISTINCT stored_zip.postal_code, known.county, known.miles
            FROM {TABLE} AS stored_zip
            LEFT JOIN unnest(%s::text[], %s::text[], %s::numeric[]) AS known(postal_code, county, miles)
                ON known.postal_code = stored_zip.postal_code
        ) AS fields
        WHERE stored.postal_code = fields.postal_code
          AND (stored.practice_zip_code IS DISTINCT FROM %s
               OR stored.county IS DISTINCT FROM fields.county
               OR stored.distance_from_practice_miles IS DISTINCT FROM fields.miles)
        """,
        (practice.PRACTICE_ZIP_CODE, updated_at, codes, counties, miles, practice.PRACTICE_ZIP_CODE),
    )
    return max(cur.rowcount or 0, 0)


class CensusYearRejected(Exception):
    """The census-year load stopped before changing stored Redfin months."""


def upsert_census_year(
    conn,
    year: int,
    locations: dict[str, practice.AreaZip] | None = None,
    updated_at: datetime | None = None,
) -> int:
    """Insert or update one census calendar year and write that year to the CSV.

    Months already stored stay in the table, for every California ZIP. Their
    practice fields are brought up to date.
    """
    pattern = census_year_month_pattern(year)
    sql = census_year_upsert_sql()
    columns = ", ".join(LOAD_COLUMNS)
    select_sql = (
        f"SELECT {columns} FROM ("
        "SELECT DISTINCT ON (postal_code, month_date_yyyymm) "
        f"{columns}, load_seq "
        "FROM staging_redfin_real_estate_value "
        "ORDER BY postal_code, month_date_yyyymm, load_seq DESC"
        ") AS census_year_rows "
        "ORDER BY postal_code, month_date_yyyymm"
    )
    tmp = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".tmp")
    count = 0
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM staging_redfin_real_estate_value
                    WHERE month_date_yyyymm !~ %s
                    """,
                    (pattern,),
                )
                outside = cur.fetchone()[0]
                if outside:
                    raise CensusYearRejected(
                        f"Refusing to load {outside} Redfin rows outside {year}."
                    )
                cur.execute(sql)
                if locations is not None:
                    refreshed = refresh_practice_fields(
                        cur, locations, dataset_stamp.stamp(updated_at)
                    )
                    print(f"Updated practice fields on {refreshed} stored months. No month was deleted.")
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT DISTINCT postal_code, month_date_yyyymm
                        FROM staging_redfin_real_estate_value
                    ) AS keyed
                    """
                )
                count = cur.fetchone()[0]
            with tmp.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(LOAD_COLUMNS)
                written = 0
                with conn.cursor() as cur:
                    cur.execute(select_sql)
                    while True:
                        chunk = cur.fetchmany(BATCH_ROWS)
                        if not chunk:
                            break
                        for row in chunk:
                            writer.writerow([csv_value(value) for value in row])
                            written += 1
            if written != count:
                raise CensusYearRejected(
                    f"The Census-year CSV has {written} rows and the load has {count}."
                )
        os.replace(tmp, OUTPUT_CSV)
    except CensusYearRejected as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit(str(exc)) from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return count


def load_history(
    conn,
    url: str,
    pull_date: str,
    census_year: int | None = None,
    updated_at: datetime | None = None,
) -> int:
    updated_at = dataset_stamp.stamp(updated_at)
    locations = practice.california_locations()
    eligible = practice.practice_area()
    print(
        f"California ZCTAs with a distance from {practice.PRACTICE_ZIP_CODE}: {len(locations)}. "
        f"Eligible for the downstream datasets (within {practice.MAX_PRACTICE_DISTANCE_MILES} miles, "
        f"LA or Orange County): {len(eligible)}. Every California ZIP is stored here."
    )
    if census_year is not None:
        print(
            f"Loading Redfin months in {census_year} only. "
            "Other years are not pulled. Stored months are not deleted."
        )
    prepare_staging(conn)
    response = open_csv(url)
    staged = 0
    seq = 0
    batch: list[tuple] = []
    try:
        for rows in read_california_rows(response, pull_date, locations, census_year):
            for row in rows:
                seq += 1
                batch.append((*row, updated_at, seq))
            if len(batch) >= BATCH_ROWS:
                copy_batch(conn, batch)
                staged += len(batch)
                batch = []
        if batch:
            copy_batch(conn, batch)
            staged += len(batch)
    finally:
        response.close()
    if staged == 0:
        return 0
    if census_year is None:
        return replace_table(conn)
    return upsert_census_year(conn, census_year, locations, updated_at)


def connect():
    import psycopg

    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not database_url:
        raise SystemExit(
            f"{DATABASE_URL_ENV} is not set. Use the Supabase session pooler "
            "connection string (port 5432). SSL is required."
        )
    return psycopg.connect(
        database_url,
        sslmode="require",
        connect_timeout=30,
        application_name="real_estate_analysis",
    )


def notify_refresh_review(table: str) -> int:
    """POST a success notice after the load. Never print the webhook URL or key."""
    url = os.environ.get("REFRESH_REVIEW_WEBHOOK_URL", "").strip()
    key = os.environ.get("REFRESH_REVIEW_WEBHOOK_KEY", "").strip()
    if not url or not key:
        print("The refresh review webhook is not configured.")
        return 0
    workflow = os.environ.get("GITHUB_WORKFLOW", "").strip() or "local"
    body = json.dumps(
        {"workflow": workflow, "table": table, "conclusion": "success"}
    ).encode()
    try:
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except Exception:
        print("The data load succeeded but the review trigger failed.")
        return 1
    if status < 200 or status >= 300:
        print("The data load succeeded but the review trigger failed.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    census_year = census_year_arg(list(sys.argv if argv is None else argv))
    pull_date = pull_date_text()
    updated_at = dataset_stamp.stamp()
    url = discover_history_url()
    print(f"ZIP history CSV: {url}")
    print(f"date_pulled_yyyymmdd: {pull_date}")
    print(f"dataset_updated_at: {updated_at.isoformat()}")
    if census_year is None:
        print(f"Replacing {TABLE} with every California month in the file.")
    else:
        print(
            f"Census year {census_year}: upserting that Redfin year only. "
            f"Older months already in {TABLE} are kept."
        )
    with connect() as conn:
        with conn.transaction():
            ensure_schema(conn)
        count = load_history(conn, url, pull_date, census_year, updated_at)
    if count == 0:
        if census_year is None:
            print("No California ZIP rows were loaded.", file=sys.stderr)
        else:
            print(
                f"No California ZIP rows were loaded for {census_year}.",
                file=sys.stderr,
            )
        return 1
    if census_year is None:
        print(
            f"Replaced {TABLE} with {count} California ZIP months "
            f"and overwrote {OUTPUT_CSV_NAME}."
        )
    else:
        print(
            f"Upserted {count} California ZIP months for {census_year} "
            f"into {TABLE}. Other months already stored were kept. "
            f"Overwrote {OUTPUT_CSV_NAME} with {census_year} only."
        )
    return notify_refresh_review(TABLE_NAME)


if __name__ == "__main__":
    sys.exit(main())
