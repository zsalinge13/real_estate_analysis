"""Load California ZIP months from Realtor.com inventory history.

The research page at https://www.realtor.com/research/data/ publishes the
Monthly Housing Inventory downloads. This script reads the Zip row's
Historical Data href and loads that CSV. It does not read values out of the
page HTML, and it does not use the current-month snapshot.

The file is already one row per ZIP per month. A row is kept when zip_name
contains ", ca" (case-insensitive, so ", CA" matches). Only the listing-price
and active-count columns below are loaded.

Each run replaces zip_inventory_month. Existing rows are deleted, then the
current California extract is inserted, so a ZIP or month that left the file
does not stay in the table. The same extract is written to
california_zip_inventory.csv, overwriting that file. The filename does not
change from run to run.
"""

from __future__ import annotations

import csv
import html
import io
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

RESEARCH_PAGE = "https://www.realtor.com/research/data/"
DATABASE_URL_ENV = "DATABASE_URL"
BATCH_ROWS = 20_000
TABLE = "zip_inventory_month"
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
LOAD_COLUMNS = KEPT_COLUMNS + ("date_pulled_yyyymmdd",)
STAGING_COLUMNS = LOAD_COLUMNS + ("load_seq",)
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
}
NOT_NULL_COLUMNS = {
    "month_date_yyyymm",
    "postal_code",
    "zip_name",
    "date_pulled_yyyymmdd",
}
UDT_FOR_NEW = {"text": "text", "integer": "int4", "numeric": "numeric"}
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


def pull_date_text(moment: datetime | None = None) -> str:
    """Calendar date of the run in America/Los_Angeles, as yyyy_mm_dd text."""
    current = moment if moment is not None else datetime.now(PACIFIC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=PACIFIC)
    else:
        current = current.astimezone(PACIFIC)
    return current.strftime("%Y_%m_%d")


def is_california_zip(zip_name: str | None) -> bool:
    return ", ca" in (zip_name or "").lower()


def require_header(fieldnames: list[str] | None) -> list[str]:
    names = [name.strip() for name in (fieldnames or [])]
    missing = [column for column in KEPT_COLUMNS if column not in names]
    if missing:
        raise SystemExit(f"ZIP history CSV is missing columns: {missing}")
    return names


def iter_california_rows(reader: csv.DictReader, pull_date: str):
    require_header(reader.fieldnames)
    for row in reader:
        zip_name = (row.get("zip_name") or "").strip()
        if not is_california_zip(zip_name):
            continue
        month = month_text((row.get("month_date_yyyymm") or "").strip())
        postal_code = (row.get("postal_code") or "").strip()
        if month is None or not postal_code:
            continue
        measures = []
        for column in MEASURE_COLUMNS:
            raw = row.get(column)
            if column == "active_listing_count":
                measures.append(parse_count(raw))
            else:
                measures.append(parse_number(raw))
        yield (month, postal_code, zip_name, *measures, pull_date)


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
    text = io.TextIOWrapper(response, encoding="utf-8-sig", newline="")
    reader = csv.DictReader(text)
    if reader.fieldnames and (reader.fieldnames[0] or "").lstrip().startswith("<"):
        response.close()
        raise SystemExit(f"Expected a CSV from {url}")
    return reader, response


def schema_sql() -> str:
    lines = []
    for name in LOAD_COLUMNS:
        null_sql = " NOT NULL" if name in NOT_NULL_COLUMNS else ""
        lines.append(f"    {name} {COLUMN_TYPES[name]}{null_sql}")
    body = ",\n".join(lines)
    return f"""
DROP TABLE IF EXISTS metric_observation;
DROP TABLE IF EXISTS region;

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
            WHERE table_schema = current_schema()
              AND table_name = %s
            """,
            (TABLE,),
        )
        found = {name: udt for name, udt in cur.fetchall()}
        for name in list(found):
            if name in LOAD_COLUMNS:
                continue
            if IDENT_RE.fullmatch(name) is None:
                raise SystemExit(f"Refusing to drop unexpected column {name!r}.")
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {name}")
            del found[name]
        for name in LOAD_COLUMNS:
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


def prepare_staging(conn) -> None:
    typed = ",\n".join(
        f"    {name} {COLUMN_TYPES[name]}" for name in LOAD_COLUMNS
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS staging_zip_inventory")
            cur.execute(
                f"""
                CREATE TEMP TABLE staging_zip_inventory (
{typed},
                    load_seq bigint NOT NULL
                )
                """
            )


def copy_batch(conn, rows: list[tuple]) -> None:
    column_list = ", ".join(STAGING_COLUMNS)
    copy_sql = f"COPY staging_zip_inventory ({column_list}) FROM STDIN"
    with conn.transaction():
        with conn.cursor() as cur:
            with cur.copy(copy_sql) as copy:
                for row in rows:
                    copy.write_row(row)


def replace_table(conn) -> int:
    """Delete every current row, insert the staged extract, and overwrite the CSV."""
    tmp = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".tmp")
    columns = ", ".join(LOAD_COLUMNS)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {TABLE}")
                cur.execute(
                    f"""
                    INSERT INTO {TABLE} ({columns})
                    SELECT DISTINCT ON (postal_code, month_date_yyyymm)
                        {columns}
                    FROM staging_zip_inventory
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


def load_history(conn, url: str, pull_date: str) -> int:
    prepare_staging(conn)
    reader, response = open_csv(url)
    staged = 0
    seq = 0
    batch: list[tuple] = []
    try:
        for row in iter_california_rows(reader, pull_date):
            seq += 1
            batch.append((*row, seq))
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
    return replace_table(conn)


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


def main() -> int:
    pull_date = pull_date_text()
    url = discover_history_url()
    print(f"ZIP history CSV: {url}")
    print(f"date_pulled_yyyymmdd: {pull_date}")
    with connect() as conn:
        with conn.transaction():
            ensure_schema(conn)
        count = load_history(conn, url, pull_date)
    if count == 0:
        print("No California ZIP rows were loaded.", file=sys.stderr)
        return 1
    print(
        f"Replaced {TABLE} with {count} California ZIP months "
        f"and overwrote {OUTPUT_CSV_NAME}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
