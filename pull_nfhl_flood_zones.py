"""Pull Los Angeles County flood-hazard polygons from the FEMA NFHL.

Layer 28 has no county-name field. The county id is DFIRM_ID. Los Angeles
County FIPS is 06037, so the query keeps DFIRM_ID LIKE '06037%'. Geometry is
WGS84 GeoJSON. Each run overwrites nfhl_flood_zones_los_angeles.geojson.
"""

from __future__ import annotations

import json
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


def main() -> int:
    tmp = OUTPUT.with_name(OUTPUT.name + ".tmp")
    count = 0
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write('{"type":"FeatureCollection","features":[\n')
            for feature in iter_features():
                if count:
                    handle.write(",\n")
                handle.write(json.dumps(feature, separators=(",", ":")))
                count += 1
            handle.write("\n]}\n")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if count == 0:
        tmp.unlink(missing_ok=True)
        print("No Los Angeles County NFHL flood zones were returned.", file=sys.stderr)
        return 1
    tmp.replace(OUTPUT)
    print(f"Wrote {count} features to {OUTPUT.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
