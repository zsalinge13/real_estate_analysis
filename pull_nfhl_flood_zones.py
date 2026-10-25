"""Pull Los Angeles County flood-hazard polygons from the FEMA NFHL.

Layer 28 has no county-name field. The county id is DFIRM_ID. Los Angeles
County FIPS is 06037, so the query keeps DFIRM_ID LIKE '06037%'. Geometry is
WGS84 GeoJSON. Each run overwrites nfhl_flood_zones_los_angeles.geojson and
replaces every row in public.nfhl_flood_zones. The connection string is
DATABASE_URL in the environment.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
OUTPUT = Path(__file__).resolve().parent / "nfhl_flood_zones_los_angeles.geojson"
PAGE_SIZE = 200
WHERE = "DFIRM_ID LIKE '06037%'"
ORDER = "OBJECTID"
DATABASE_URL_ENV = "DATABASE_URL"
TABLE = "public.nfhl_flood_zones"
BATCH_ROWS = 500

# Flood-zone attributes present on layer 28. COUNTY is added in feature_json
# because the service has no county-name field.
FIELDS = (
    "DFIRM_ID",
    "FLD_ZONE",
    "ZONE_SUBTY",
    "SFHA_TF",
    "STATIC_BFE",
    "DEPTH",
)
COUNTY = "Los Angeles"
# Table columns. geometry is GeoJSON text for the polygon, not a PostGIS type.
COLUMNS = (
    "dfirm_id",
    "fld_zone",
    "zone_subty",
    "sfha_tf",
    "static_bfe",
    "depth",
    "county",
    "geometry",
)
USER_AGENT = "real-estate-analysis"
MAX_FEATURES = 100_000


def fetch_page(offset: int) -> dict:
    params = {
        "where": WHERE,
        "outFields": ",".join(FIELDS + (ORDER,)),
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
        "orderByFields": ORDER,
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
    }
    url = SERVICE + "?" + urllib.parse.urlencode(params)
    payload = None
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.load(response)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 3:
                raise SystemExit(f"NFHL query failed: {exc}") from exc
            time.sleep(2 * (attempt + 1))
    if payload is None:
        raise SystemExit(f"NFHL query failed: {last_error}")
    if "error" in payload:
        raise SystemExit(f"NFHL query failed: {payload['error']}")
    return payload


def feature_json(feature: dict) -> dict:
    properties_in = feature.get("properties") or {}
    dfirm_id = str(properties_in.get("DFIRM_ID") or "")
    if not dfirm_id.startswith("06037"):
        raise SystemExit(
            f"NFHL returned a feature outside Los Angeles County: {dfirm_id!r}"
        )
    geometry = feature.get("geometry") or {}
    if geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise SystemExit("NFHL feature was not a polygon.")
    properties = {name: properties_in.get(name) for name in FIELDS}
    properties["COUNTY"] = COUNTY
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def iter_features():
    offset = 0
    seen_first: set[str] = set()
    while True:
        payload = fetch_page(offset)
        features = payload.get("features") or []
        if not features:
            break
        first = str((features[0].get("properties") or {}).get(ORDER, ""))
        if first in seen_first:
            raise SystemExit(
                "NFHL did not advance resultOffset. "
                f"Stopped after {offset} features."
            )
        seen_first.add(first)
        for feature in features:
            yield feature_json(feature)
        offset += len(features)
        if len(features) < PAGE_SIZE and not payload.get("exceededTransferLimit"):
            break
        if offset > MAX_FEATURES:
            raise SystemExit("NFHL pagination did not finish.")


def db_row(feature: dict) -> tuple:
    properties = feature["properties"]
    return (
        properties.get("DFIRM_ID"),
        properties.get("FLD_ZONE"),
        properties.get("ZONE_SUBTY"),
        properties.get("SFHA_TF"),
        properties.get("STATIC_BFE"),
        properties.get("DEPTH"),
        properties.get("COUNTY"),
        json.dumps(feature["geometry"], separators=(",", ":")),
    )


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


def ensure_table(conn) -> None:
    """Create public.nfhl_flood_zones if it is missing. No other table is changed."""
    column_sql = ", ".join(
        f"{name} {'double precision' if name in {'static_bfe', 'depth'} else 'text'}"
        for name in COLUMNS
    )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} (\n    {column_sql}\n)"
        )


def replace_rows(conn, features: list[dict]) -> int:
    """Delete rows in public.nfhl_flood_zones and insert this extract."""
    names = ", ".join(COLUMNS)
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    insert_sql = f"INSERT INTO {TABLE} ({names}) VALUES ({placeholders})"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {TABLE}")
            batch: list[tuple] = []
            for feature in features:
                batch.append(db_row(feature))
                if len(batch) >= BATCH_ROWS:
                    cur.executemany(insert_sql, batch)
                    batch = []
            if batch:
                cur.executemany(insert_sql, batch)
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            count = cur.fetchone()[0]
    return count


def write_geojson(path: Path, features: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write('{"type":"FeatureCollection","features":[\n')
        for index, feature in enumerate(features):
            if index:
                handle.write(",\n")
            handle.write(json.dumps(feature, separators=(",", ":")))
        handle.write("\n]}\n")


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


def main() -> int:
    with connect() as conn:
        with conn.transaction():
            ensure_table(conn)
        features = list(iter_features())
        if not features:
            print(
                "No Los Angeles County NFHL flood zones were returned.",
                file=sys.stderr,
            )
            return 1
        tmp = OUTPUT.with_name(OUTPUT.name + ".tmp")
        try:
            write_geojson(tmp, features)
            count = replace_rows(conn, features)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(OUTPUT)
    print(
        f"Replaced {TABLE} with {count} features and overwrote {OUTPUT.name}."
    )
    return notify_refresh_review("nfhl_flood_zones")


if __name__ == "__main__":
    sys.exit(main())
