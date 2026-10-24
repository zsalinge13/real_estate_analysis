"""Pull California census tracts from the FEMA National Risk Index.

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

SERVICE = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Census_Tracts/FeatureServer/0/query"
)
OUTPUT = Path(__file__).resolve().parent / "nri_census_tracts_ca.csv"
PAGE_SIZE = 2000
WHERE = "STATEABBRV = 'CA'"
ORDER = "TRACTFIPS"

# Tract ids, then each hazard's loss, score, rating, and annualized
# frequency. Names are the service fields: EALT is expected annual loss,
# EALS is the score, EALR is the rating, and AFREQ is annualized frequency.
# Inland flood is IFLD and coastal flood is CFLD.
FIELDS = (
    "TRACTFIPS",
    "COUNTY",
    "STATEABBRV",
    "STCOFIPS",
    "IFLD_EALT",
    "IFLD_EALS",
    "IFLD_EALR",
    "IFLD_AFREQ",
    "CFLD_EALT",
    "CFLD_EALS",
    "CFLD_EALR",
    "CFLD_AFREQ",
    "HWAV_EALT",
    "HWAV_EALS",
    "HWAV_EALR",
    "HWAV_AFREQ",
    "SWND_EALT",
    "SWND_EALS",
    "SWND_EALR",
    "SWND_AFREQ",
    "LNDS_EALT",
    "LNDS_EALS",
    "LNDS_EALR",
    "LNDS_AFREQ",
    "WFIR_EALT",
    "WFIR_EALS",
    "WFIR_EALR",
    "WFIR_AFREQ",
    "ERQK_EALT",
    "ERQK_EALS",
    "ERQK_EALR",
    "ERQK_AFREQ",
    "TSUN_EALT",
    "TSUN_EALS",
    "TSUN_EALR",
    "TSUN_AFREQ",
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
        raise SystemExit(f"National Risk Index query failed: {payload['error']}")
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
                "National Risk Index did not advance resultOffset. "
                f"Stopped after {offset} rows."
            )
        seen_first.add(first)
        for feature in features:
            attributes = feature.get("attributes") or {}
            if "geometry" in feature:
                raise SystemExit("National Risk Index returned geometry.")
            yield attributes
        offset += len(features)
        if len(features) < PAGE_SIZE:
            break
        if offset > 200_000:
            raise SystemExit("National Risk Index pagination did not finish.")


def main() -> int:
    tmp = OUTPUT.with_name(OUTPUT.name + ".tmp")
    count = 0
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(FIELDS)
            for attributes in iter_attributes():
                writer.writerow([cell(attributes.get(name)) for name in FIELDS])
                count += 1
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if count == 0:
        tmp.unlink(missing_ok=True)
        print("No California National Risk Index tracts were returned.", file=sys.stderr)
        return 1
    tmp.replace(OUTPUT)
    print(f"Wrote {count} rows to {OUTPUT.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
