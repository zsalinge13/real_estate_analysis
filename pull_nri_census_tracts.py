"""Pull Los Angeles County census tracts from the FEMA National Risk Index.

Attributes only. The query sets returnGeometry to false. Each run overwrites
nri_census_tracts_ca.csv. When DATABASE_URL is set, the same rows replace
public.nri_census_tracts_ca. No other table is dropped or altered.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

LAYER = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Census_Tracts/FeatureServer/0"
)
SERVICE = LAYER + "/query"
OUTPUT = Path(__file__).resolve().parent / "nri_census_tracts_ca.csv"
PAGE_SIZE = 2000
# Los Angeles County is STCOFIPS 06037. STATEABBRV keeps the extract in California.
WHERE = "STATEABBRV = 'CA' AND STCOFIPS = '06037'"
ORDER = "TRACTFIPS"
ID_FIELDS = ("TRACTFIPS", "COUNTY", "STATEABBRV", "STCOFIPS")
TABLE_NAME = "nri_census_tracts_ca"
TABLE = "public.nri_census_tracts_ca"
STAGING = "staging_nri_census_tracts_ca"
DATABASE_URL_ENV = "DATABASE_URL"
USER_AGENT = "real-estate-analysis"
NUMERIC_UDT = {"numeric", "float4", "float8", "int2", "int4", "int8"}
TEXT_UDT = {"text", "varchar"}


def cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    return value


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    if "error" in payload:
        raise SystemExit(f"National Risk Index query failed: {payload['error']}")
    return payload


def output_fields() -> tuple[str, ...]:
    """Identifiers, then every annualized-frequency field in service order."""
    payload = fetch_json(LAYER + "?f=json")
    names = [field.get("name") for field in payload.get("fields") or []]
    missing = [name for name in ID_FIELDS if name not in names]
    if missing:
        raise SystemExit(
            "National Risk Index is missing fields: " + ", ".join(missing)
        )
    afreq = tuple(
        name for name in names if isinstance(name, str) and "_AFREQ" in name
    )
    if not afreq:
        raise SystemExit("National Risk Index returned no annualized frequency fields.")
    return ID_FIELDS + afreq


def fetch_page(fields: tuple[str, ...], offset: int) -> dict:
    params = {
        "where": WHERE,
        "outFields": ",".join(fields),
        "returnGeometry": "false",
        "f": "json",
        "orderByFields": ORDER,
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
    }
    return fetch_json(SERVICE + "?" + urllib.parse.urlencode(params))


def iter_attributes(fields: tuple[str, ...]):
    offset = 0
    seen_first: set[str] = set()
    while True:
        payload = fetch_page(fields, offset)
        features = payload.get("features") or []
        if not features:
            break
        first = str((features[0].get("attributes") or {}).get(ORDER, ""))
        if first in seen_first:
            raise SystemExit(
                "National Risk Index did not advance resultOffset. "
                f"Stopped after {offset} rows."
            )
        seen_first.add(first)
        for feature in features:
            attributes = feature.get("attributes") or {}
            if "geometry" in feature:
                raise SystemExit("National Risk Index returned geometry.")
            if (
                attributes.get("STATEABBRV") != "CA"
                or attributes.get("STCOFIPS") != "06037"
                or attributes.get("COUNTY") != "Los Angeles"
            ):
                raise SystemExit(
                    "National Risk Index row is not Los Angeles County: "
                    f"{attributes.get('TRACTFIPS')}"
                )
            yield attributes
        offset += len(features)
        if len(features) < PAGE_SIZE:
            break
        if offset > 200_000:
            raise SystemExit("National Risk Index pagination did not finish.")


def quote_ident(name: str) -> str:
    if (
        not name
        or not name.isascii()
        or not name[0].isalpha()
        or any(not (char.isalnum() or char == "_") for char in name)
    ):
        raise SystemExit(f"Refusing unexpected identifier {name!r}.")
    return '"' + name + '"'


def sql_type(name: str) -> str:
    if name in ID_FIELDS:
        return "text"
    if "_AFREQ" in name:
        return "numeric"
    raise SystemExit(f"Refusing unexpected column {name!r}.")


def db_cell(name: str, value: object) -> str | Decimal | None:
    if name in ID_FIELDS:
        text = "" if value is None else str(value).strip()
        if not text:
            raise SystemExit(f"National Risk Index row is missing {name}.")
        return text
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SystemExit(f"National Risk Index value for {name} is not numeric.")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return Decimal(text)
    return Decimal(json.dumps(value))


def database_url() -> str:
    return os.environ.get(DATABASE_URL_ENV, "").strip()


def connect():
    import psycopg

    url = database_url()
    if not url:
        raise SystemExit(f"{DATABASE_URL_ENV} is not set.")
    return psycopg.connect(
        url,
        sslmode="require",
        connect_timeout=30,
        application_name="real_estate_analysis",
    )


def alter_type(cur, column: str, sql_type_name: str, using: str) -> None:
    cur.execute(
        f"ALTER TABLE {TABLE} ALTER COLUMN {quote_ident(column)} "
        f"TYPE {sql_type_name} USING {using}"
    )


def align_text(cur, column: str, udt: str) -> None:
    if udt == "text":
        return
    if udt == "varchar":
        alter_type(cur, column, "text", f"{quote_ident(column)}::text")
        return
    raise SystemExit(f"Cannot convert {column} on {TABLE} from {udt} to text.")


def align_numeric(cur, column: str, udt: str) -> None:
    ident = quote_ident(column)
    if udt == "numeric":
        return
    if udt in NUMERIC_UDT:
        using = f"{ident}::numeric"
    elif udt in TEXT_UDT:
        using = (
            f"CASE WHEN {ident} IS NULL OR btrim({ident}) = '' THEN NULL "
            f"ELSE btrim({ident})::numeric END"
        )
    else:
        raise SystemExit(f"Cannot convert {column} on {TABLE} from {udt} to numeric.")
    alter_type(cur, column, "numeric", using)


def relation_kind(cur) -> str | None:
    cur.execute(
        """
        SELECT c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = %s
        """,
        (TABLE_NAME,),
    )
    row = cur.fetchone()
    return None if row is None else row[0]


def ensure_schema(cur, fields: tuple[str, ...]) -> None:
    """Create or align public.nri_census_tracts_ca. No other relation is changed."""
    columns = ",\n".join(
        f"    {quote_ident(name)} {sql_type(name)}"
        + (" NOT NULL" if name in ID_FIELDS else "")
        for name in fields
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
{columns},
            PRIMARY KEY ("TRACTFIPS")
        )
        """
    )
    kind = relation_kind(cur)
    if kind != "r":
        raise SystemExit(f"{TABLE} is not an ordinary table.")

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
    cur.execute(
        """
        SELECT tc.constraint_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (TABLE_NAME,),
    )
    pk_rows = cur.fetchall()
    pk_columns = [column for _, column in pk_rows]
    if pk_columns != ["TRACTFIPS"]:
        for constraint_name in dict.fromkeys(name for name, _ in pk_rows):
            cur.execute(
                f"ALTER TABLE {TABLE} DROP CONSTRAINT {quote_ident(constraint_name)}"
            )
    for name in list(found):
        if name in fields:
            continue
        cur.execute(f"ALTER TABLE {TABLE} DROP COLUMN {quote_ident(name)}")
        del found[name]
    for name in fields:
        if name in found:
            continue
        cur.execute(
            f"ALTER TABLE {TABLE} ADD COLUMN {quote_ident(name)} {sql_type(name)}"
        )
        found[name] = "text" if name in ID_FIELDS else "numeric"
    for name in ID_FIELDS:
        align_text(cur, name, found[name])
    for name in fields:
        if name in ID_FIELDS:
            continue
        align_numeric(cur, name, found[name])


def primary_key_columns(cur) -> list[str]:
    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (TABLE_NAME,),
    )
    return [column for (column,) in cur.fetchall()]


def enforce_keys(cur) -> None:
    for name in ID_FIELDS:
        cur.execute(
            f"ALTER TABLE {TABLE} ALTER COLUMN {quote_ident(name)} SET NOT NULL"
        )
    if primary_key_columns(cur) != ["TRACTFIPS"]:
        cur.execute(f'ALTER TABLE {TABLE} ADD PRIMARY KEY ("TRACTFIPS")')


def replace_table(cur, fields: tuple[str, ...], rows: list[tuple]) -> int:
    column_list = ", ".join(quote_ident(name) for name in fields)
    typed = ",\n".join(
        f"    {quote_ident(name)} {sql_type(name)}" for name in fields
    )
    cur.execute(
        f"""
        CREATE TEMP TABLE IF NOT EXISTS {STAGING} (
{typed}
        )
        """
    )
    cur.execute(f"DELETE FROM {STAGING}")
    with cur.copy(f"COPY {STAGING} ({column_list}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    cur.execute(f"DELETE FROM {TABLE}")
    cur.execute(
        f"INSERT INTO {TABLE} ({column_list}) "
        f"SELECT {column_list} FROM {STAGING}"
    )
    cur.execute(
        f"""
        SELECT COUNT(*)
        FROM {TABLE}
        WHERE "STATEABBRV" IS DISTINCT FROM 'CA'
           OR "STCOFIPS" IS DISTINCT FROM '06037'
           OR "COUNTY" IS DISTINCT FROM 'Los Angeles'
        """
    )
    outside = cur.fetchone()[0]
    if outside:
        raise SystemExit(f"{TABLE} has {outside} rows outside Los Angeles County.")
    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    count = cur.fetchone()[0]
    if count != len(rows):
        raise SystemExit(
            f"{TABLE} has {count} rows after replace; the extract has {len(rows)}."
        )
    enforce_keys(cur)
    return count


def load_table(fields: tuple[str, ...], rows: list[tuple]) -> int:
    with connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                ensure_schema(cur, fields)
                count = replace_table(cur, fields, rows)
    return count


def main() -> int:
    fields = output_fields()
    tmp = OUTPUT.with_name(OUTPUT.name + ".tmp")
    rows: list[tuple] = []
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(fields)
            for attributes in iter_attributes(fields):
                writer.writerow([cell(attributes.get(name)) for name in fields])
                rows.append(tuple(db_cell(name, attributes.get(name)) for name in fields))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if not rows:
        tmp.unlink(missing_ok=True)
        print(
            "No Los Angeles County National Risk Index tracts were returned.",
            file=sys.stderr,
        )
        return 1
    tmp.replace(OUTPUT)
    print(f"Wrote {len(rows)} rows to {OUTPUT.name}.")
    if not database_url():
        print(f"{DATABASE_URL_ENV} is not set. Left {TABLE} unchanged.")
        return 0
    count = load_table(fields, rows)
    print(f"Replaced {TABLE} with {count} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
