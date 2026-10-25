"""Export the current Supabase table when a load workflow fails.

The CSV filename and columns match the successful load artifact. The flood-zone
export includes geometry as GeoJSON text. Other exports still omit a geometry
column. This script does not print the connection string or row contents.
"""

from __future__ import annotations

import csv
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import load_california
import pull_nfhl_flood_zones
import pull_nri_census_tracts

NO_PRIOR_DATA = "There is no prior data to export."
EXPORT_FAILED = "Could not export the Supabase backup."
FETCH_SIZE = 5_000


@dataclass(frozen=True)
class Spec:
    table: str
    path: Path
    connect: Callable
    columns: Callable
    order_by: tuple[str, ...]

    @property
    def table_name(self) -> str:
        schema, _, name = self.table.partition(".")
        if schema != "public" or not name:
            raise SystemExit(EXPORT_FAILED)
        return name


def price_columns(_cur) -> tuple[str, ...]:
    return load_california.LOAD_COLUMNS


def flood_columns(_cur) -> tuple[str, ...]:
    return pull_nfhl_flood_zones.CSV_COLUMNS


def nri_columns(cur) -> tuple[str, ...]:
    """Identifiers, then annualized-frequency columns in table order."""
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (pull_nri_census_tracts.TABLE_NAME,),
    )
    present = [name for (name,) in cur.fetchall()]
    present_set = set(present)
    columns = [
        name for name in pull_nri_census_tracts.ID_FIELDS if name in present_set
    ]
    columns.extend(
        name
        for name in present
        if name not in pull_nri_census_tracts.ID_FIELDS and "_AFREQ" in name
    )
    return tuple(columns)


def build_specs() -> dict[str, Spec]:
    specs = (
        Spec(
            table=load_california.TABLE,
            path=load_california.OUTPUT_CSV,
            connect=load_california.connect,
            columns=price_columns,
            order_by=("postal_code", "month_date_yyyymm"),
        ),
        Spec(
            table=pull_nri_census_tracts.TABLE,
            path=pull_nri_census_tracts.OUTPUT,
            connect=pull_nri_census_tracts.connect,
            columns=nri_columns,
            order_by=("TRACTFIPS",),
        ),
        Spec(
            table=pull_nfhl_flood_zones.TABLE,
            path=pull_nfhl_flood_zones.OUTPUT_CSV,
            connect=pull_nfhl_flood_zones.connect,
            columns=flood_columns,
            order_by=pull_nfhl_flood_zones.BACKUP_ORDER,
        ),
    )
    return {spec.table_name: spec for spec in specs}


SPECS = build_specs()


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
          AND table_type = 'BASE TABLE'
        """,
        (table_name,),
    )
    return cur.fetchone() is not None


def quote_column(name: str) -> str:
    return pull_nri_census_tracts.quote_ident(name)


def selected_columns(spec: Spec, names: tuple[str, ...]) -> tuple[str, ...]:
    """Columns written to the backup CSV.

    Flood zones keep geometry so the backup matches nfhl_flood_zones.csv from
    a successful pull. Other tables still drop a geometry column.
    """
    if spec.table == pull_nfhl_flood_zones.TABLE:
        return tuple(names)
    return tuple(name for name in names if name != "geometry")


def export_connection(conn, spec: Spec) -> int:
    """Write spec.path from the current table. Read only."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            if not table_exists(cur, spec.table_name):
                raise SystemExit(NO_PRIOR_DATA)
            columns = selected_columns(spec, tuple(spec.columns(cur)))
            if not columns:
                raise SystemExit(NO_PRIOR_DATA)
            quoted = [quote_column(name) for name in columns]
            order = [
                quote_column(name) for name in spec.order_by if name in columns
            ]
            relation = "public." + quote_column(spec.table_name)
            cur.execute(f"SELECT COUNT(*) FROM {relation}")
            count = cur.fetchone()[0]
            if not count:
                raise SystemExit(NO_PRIOR_DATA)
            select_sql = f"SELECT {', '.join(quoted)} FROM {relation}"
            if order:
                select_sql += " ORDER BY " + ", ".join(order)
            cur.execute(select_sql)
            tmp = spec.path.with_name(spec.path.name + ".tmp")
            written = 0
            try:
                with tmp.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(
                        handle,
                        lineterminator="\n",
                        quoting=csv.QUOTE_MINIMAL,
                    )
                    writer.writerow(columns)
                    while True:
                        chunk = cur.fetchmany(FETCH_SIZE)
                        if not chunk:
                            break
                        for row in chunk:
                            if len(row) != len(columns):
                                raise SystemExit(EXPORT_FAILED)
                            writer.writerow(
                                [load_california.csv_value(value) for value in row]
                            )
                            written += 1
                if written != count:
                    raise SystemExit(EXPORT_FAILED)
                tmp.replace(spec.path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
    return written


def failure_message(exc: SystemExit) -> str:
    if isinstance(exc.code, str) and exc.code:
        return exc.code
    return EXPORT_FAILED


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    if len(args) != 2 or args[1] not in SPECS:
        print(EXPORT_FAILED, file=sys.stderr)
        return 1
    spec = SPECS[args[1]]
    try:
        with spec.connect() as conn:
            count = export_connection(conn, spec)
    except SystemExit as exc:
        print(failure_message(exc), file=sys.stderr)
        return 1
    except Exception:
        print(EXPORT_FAILED, file=sys.stderr)
        return 1
    print(f"Exported {count} rows from {spec.table}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
