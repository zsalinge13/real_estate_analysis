"""Pull Los Angeles County census tracts from the FEMA National Risk Index.

Attributes only. The query sets returnGeometry to false. Each run overwrites
nri_census_tracts_ca.csv.
"""

from __future__ import annotations

import csv
import json
import sys
import urllib.parse
import urllib.request
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
USER_AGENT = "real-estate-analysis"


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


def main() -> int:
    fields = output_fields()
    tmp = OUTPUT.with_name(OUTPUT.name + ".tmp")
    count = 0
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(fields)
            for attributes in iter_attributes(fields):
                writer.writerow([cell(attributes.get(name)) for name in fields])
                count += 1
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if count == 0:
        tmp.unlink(missing_ok=True)
        print("No Los Angeles County National Risk Index tracts were returned.", file=sys.stderr)
        return 1
    tmp.replace(OUTPUT)
    print(f"Wrote {count} rows to {OUTPUT.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
