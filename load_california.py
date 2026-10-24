"""Load California ZIP months from Realtor.com inventory history.

The research page at https://www.realtor.com/research/data/ publishes the
Monthly Housing Inventory downloads. This script reads the Zip row's
Historical Data href and loads that CSV. It does not read values out of the
page HTML, and it does not use the current-month snapshot.

The file is already one row per ZIP per month. A row is kept when zip_name
contains ", ca" (case-insensitive, so ", CA" matches). Only the listing-price
and active-count columns below are loaded. A rerun upserts, because
Realtor.com reissues the full history and can revise past months.
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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

RESEARCH_PAGE = "https://www.realtor.com/research/data/"
DATABASE_URL_ENV = "DATABASE_URL"
BATCH_ROWS = 20_000
TABLE = "zip_inventory_month"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Source columns kept from the ZIP history file, in load order.
# pulled_at is added by this script; it is not a column in the CSV.
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
LOAD_COLUMNS = KEPT_COLUMNS + ("pulled_at",)
IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*")

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
    section = page[start:end if end > start else None]
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


def is_california_zip(zip_name: str | None) -> bool:
    return ", ca" in (zip_name or "").lower()


def require_header(fieldnames: list[str] | None) -> list[str]:
    names = [name.strip() for name in (fieldnames or [])]
    missing = [column for column in KEPT_COLUMNS if column not in names]
    if missing:
        raise SystemExit(f"ZIP history CSV is missing columns: {missing}")
    return names


def iter_california_rows(reader: csv.DictReader, pulled_at: datetime):
    require_header(reader.fieldnames)
    for row in reader:
        zip_name = (row.get("zip_name") or "").strip()
        if not is_california_zip(zip_name):
            continue
        month_text = (row.get("month_date_yyyymm") or "").strip()
        postal_code = (row.get("postal_code") or "").strip()
        if len(month_text) != 6 or not month_text.isdigit() or not postal_code:
            continue
        measures = tuple(parse_number(row.get(column)) for column in MEASURE_COLUMNS)
        yield (int(month_text), postal_code, zip_name, *measures, pulled_at)


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
    measures = ",\n".join(f"    {name} numeric" for name in MEASURE_COLUMNS)
    return f"""
DROP TABLE IF EXISTS metric_observation;
DROP TABLE IF EXISTS region;

CREATE TABLE IF NOT EXISTS {TABLE} (
    month_date_yyyymm integer NOT NULL,
    postal_code text NOT NULL,
    zip_name text NOT NULL,
{measures},
    pulled_at timestamptz NOT NULL,
    PRIMARY KEY (postal_code, month_date_yyyymm)
);
"""


def upsert_sql() -> str:
    assignments = ",\n".join(
        f"    {name} = EXCLUDED.{name}"
        for name in ("zip_name", *MEASURE_COLUMNS, "pulled_at")
    )
    columns = ", ".join(LOAD_COLUMNS)
    return f"""
INSERT INTO {TABLE} ({columns})
SELECT DISTINCT ON (postal_code, month_date_yyyymm)
    {columns}
FROM staging_zip_inventory
ORDER BY postal_code, month_date_yyyymm, pulled_at DESC
ON CONFLICT (postal_code, month_date_yyyymm)
DO UPDATE SET
{assignments}
"""


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(schema_sql())
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
            """,
            (TABLE,),
        )
        for (name,) in cur.fetchall():
            if name in LOAD_COLUMNS:
                continue
            if IDENT_RE.fullmatch(name) is None:
                raise SystemExit(f"Refusing to drop unexpected column {name!r}.")
            cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {name}")


def write_batch(conn, rows: list[tuple]) -> None:
    if not rows:
        return
    typed = ["integer", "text", "text"] + ["numeric"] * len(MEASURE_COLUMNS) + [
        "timestamptz"
    ]
    staging_cols = ",\n".join(
        f"    {name} {sql_type}" for name, sql_type in zip(LOAD_COLUMNS, typed)
    )
    column_list = ", ".join(LOAD_COLUMNS)
    copy_sql = f"COPY staging_zip_inventory ({column_list}) FROM STDIN"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS staging_zip_inventory")
            cur.execute(
                f"""
                CREATE TEMP TABLE staging_zip_inventory (
{staging_cols}
                ) ON COMMIT DROP
                """
            )
            with cur.copy(copy_sql) as copy:
                for row in rows:
                    copy.write_row(row)
            cur.execute(upsert_sql())


def load_history(conn, url: str, pulled_at: datetime) -> int:
    reader, response = open_csv(url)
    written = 0
    batch: list[tuple] = []
    try:
        for row in iter_california_rows(reader, pulled_at):
            batch.append(row)
            if len(batch) >= BATCH_ROWS:
                write_batch(conn, batch)
                written += len(batch)
                batch = []
        if batch:
            write_batch(conn, batch)
            written += len(batch)
    finally:
        response.close()
    return written


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
    pulled_at = datetime.now(timezone.utc)
    url = discover_history_url()
    print(f"ZIP history CSV: {url}")
    with connect() as conn:
        ensure_schema(conn)
        conn.commit()
        count = load_history(conn, url, pulled_at)
    if count == 0:
        print("No California ZIP rows were loaded.", file=sys.stderr)
        return 1
    print(f"Loaded {count} California ZIP months into {TABLE}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
