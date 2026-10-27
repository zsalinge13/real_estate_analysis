"""One timestamptz for every row a single Supabase load writes.

Callers capture one timezone-aware instant and store that same value on
each row that load inserts or updates. Rows the loader does not rewrite
are left alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

COLUMN = "dataset_updated_at"
SQL_TYPE = "timestamptz"


def stamp(moment: datetime | None = None) -> datetime:
    """Return one timestamptz for a load. A naive value is treated as UTC."""
    current = moment if moment is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def stamp_rows(
    rows: list[tuple], moment: datetime | None = None
) -> tuple[datetime, list[tuple]]:
    """Append the same timestamp to every row in this load."""
    updated_at = stamp(moment)
    return updated_at, [tuple(row) + (updated_at,) for row in rows]
