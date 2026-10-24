"""Pull California census tracts from the FEMA P-366 earthquake layer.

Attributes only. The query sets returnGeometry to false. Each run overwrites
earthquake_tracts_ca.csv.
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/ArcGIS/rest/services/"
    "FEMA_P366_3_14_23_gdb/FeatureServer/1/query"
)
OUTPUT = Path(__file__).resolve().parent / "earthquake_tracts_ca.csv"
PAGE_SIZE = 2000
WHERE = "ST_ABR = 'CA'"
ORDER = "Tract"
FIELDS = (
    "Tract",
    "CountyName",
    "ST_ABR",
    "StateFips",
    "CountyFips",
    "AEL_1mil",
)
USER_AGENT = "real-estate-analysis"


def cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return json.dumps(value)
    return value


def fetch_page(offset: int) -> dict:
    params = {
        "where": WHERE,
        "outFields": ",".join(FIELDS),
        "returnGeometry": "false",
        "f": "json",
        "orderByFields": ORDER,
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
    }
    url = SERVICE + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    if "error" in payload:
        raise SystemExit(f"FEMA P-366 query failed: {payload['error']}")
    return payload


def iter_attributes():
    offset = 0
    seen_first: set[str] = set()
    while True:
        payload = fetch_page(offset)
        features = payload.get("features") or []
        if not features:
            break
        first = str((features[0].get("attributes") or {}).get(ORDER, ""))
        if first in seen_first:
            raise SystemExit(
                "FEMA P-366 did not advance resultOffset. "
                f"Stopped after {offset} rows."
            )
        seen_first.add(first)
        for feature in features:
            attributes = feature.get("attributes") or {}
            if "geometry" in feature:
                raise SystemExit("FEMA P-366 returned geometry.")
            yield attributes
        offset += len(features)
        if len(features) < PAGE_SIZE:
            break
        if offset > 200_000:
            raise SystemExit("FEMA P-366 pagination did not finish.")


def main() -> int:
    tmp = OUTPUT.with_name(OUTPUT.name + ".tmp")
    count = 0
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(FIELDS)
            for attributes in iter_attributes():
                state = attributes.get("ST_ABR")
                if state not in (None, "CA"):
                    raise SystemExit(f"Unexpected state abbreviation {state!r}.")
                writer.writerow([cell(attributes.get(name)) for name in FIELDS])
                count += 1
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if count == 0:
        tmp.unlink(missing_ok=True)
        print("No California earthquake tracts were returned.", file=sys.stderr)
        return 1
    tmp.replace(OUTPUT)
    print(f"Wrote {count} rows to {OUTPUT.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
