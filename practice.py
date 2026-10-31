"""The practice ZIP and the ZIP codes the pipeline covers.

PRACTICE_ZIP_CODE is the one setting for where the practice is. A ZIP is in
the practice area when two things are true. First, the Census 2020
ZCTA-to-county relationship file gives a county in COUNTIES strictly more
land area in that ZCTA than any other county, with water area breaking a
land tie. A tie on both is not a majority, and the place name is not used.
Second, its distance from the practice ZIP is MAX_DISTANCE_MILES or less.

Distance is the haversine great-circle distance between the Census 2024
Gazetteer ZCTA internal points (INTPTLAT, INTPTLONG), on a sphere of
3,958.8 miles, rounded to two decimals. The cutoff is applied to the
rounded distance.
"""

from __future__ import annotations

import csv
import io
import math
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

PRACTICE_ZIP_CODE = "90210"
MAX_DISTANCE_MILES = Decimal(50)
# County FIPS to the name stored in the county field.
COUNTIES = {"06037": "Los Angeles", "06059": "Orange"}
FIELDS = ("practice_zip_code", "county", "distance_from_practice_miles")
AREA_PHRASE = f"ZIP codes within {MAX_DISTANCE_MILES} miles of the practice"

# https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt
CROSSWALK_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_county20_natl.txt"
)
GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_zcta_national.zip"
)
EARTH_RADIUS_MILES = 3958.8
DISTANCE_PLACES = Decimal("0.01")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class AreaZip:
    county: str
    distance_from_practice_miles: Decimal
    practice_zip_code: str = PRACTICE_ZIP_CODE

    def fields(self) -> tuple[str, str, Decimal]:
        return (self.practice_zip_code, self.county, self.distance_from_practice_miles)


def fetch_bytes(url: str, what: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{what} returned HTTP {exc.code}: {url}") from exc


def majority_counties(text: str, counties: dict[str, str] = COUNTIES) -> dict[str, str]:
    """ZCTA to county name, for ZCTAs whose majority county is in `counties`."""
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    required = {"GEOID_ZCTA5_20", "GEOID_COUNTY_20", "AREALAND_PART", "AREAWATER_PART"}
    missing = required.difference(reader.fieldnames or [])
    if missing:
        raise SystemExit(f"Census crosswalk is missing columns: {sorted(missing)}")
    parts: dict[str, dict[str, list[int]]] = {}
    for row in reader:
        zcta = (row.get("GEOID_ZCTA5_20") or "").strip()
        county = (row.get("GEOID_COUNTY_20") or "").strip()
        if len(zcta) != 5 or not zcta.isdigit():
            continue
        if len(county) != 5 or not county.isdigit():
            continue
        try:
            land = int(row.get("AREALAND_PART") or 0)
            water = int(row.get("AREAWATER_PART") or 0)
        except ValueError:
            continue
        area = parts.setdefault(zcta, {}).setdefault(county, [0, 0])
        area[0] += land
        area[1] += water
    kept: dict[str, str] = {}
    for zcta, by_county in parts.items():
        for fips, name in counties.items():
            area = by_county.get(fips)
            if area is None or area == [0, 0]:
                continue
            if any(other >= area for county, other in by_county.items() if county != fips):
                continue
            kept[zcta] = name
    if not kept:
        raise SystemExit(f"Census crosswalk did not identify any ZIPs in {sorted(counties)}.")
    return kept


def county_postal_codes() -> dict[str, str]:
    text = fetch_bytes(CROSSWALK_URL, "Census ZCTA-county crosswalk").decode("utf-8-sig")
    return majority_counties(text)


def parse_gazetteer(text: str) -> dict[str, tuple[float, float]]:
    lines = text.splitlines()
    if not lines:
        raise SystemExit("The ZCTA gazetteer is empty.")
    header = [name.strip() for name in lines[0].split("\t")]
    try:
        zi, lat_i, lon_i = header.index("GEOID"), header.index("INTPTLAT"), header.index("INTPTLONG")
    except ValueError as exc:
        raise SystemExit(f"The ZCTA gazetteer is missing a column: {exc}") from exc
    points: dict[str, tuple[float, float]] = {}
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split("\t")]
        if len(cells) <= max(zi, lat_i, lon_i):
            continue
        points[cells[zi]] = (float(cells[lat_i]), float(cells[lon_i]))
    if not points:
        raise SystemExit("The ZCTA gazetteer has no points.")
    return points


def zcta_points() -> dict[str, tuple[float, float]]:
    archive = zipfile.ZipFile(io.BytesIO(fetch_bytes(GAZETTEER_URL, "Census gazetteer download")))
    name = next(entry for entry in archive.namelist() if entry.endswith(".txt"))
    return parse_gazetteer(archive.read(name).decode("utf-8-sig"))


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine great-circle distance in miles."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def distance_miles(a: tuple[float, float], b: tuple[float, float]) -> Decimal:
    return Decimal(str(miles_between(a, b))).quantize(DISTANCE_PLACES, rounding=ROUND_HALF_UP)


def area_from(
    counties: dict[str, str],
    points: dict[str, tuple[float, float]],
    practice_zip: str = PRACTICE_ZIP_CODE,
    max_miles: Decimal = MAX_DISTANCE_MILES,
) -> dict[str, AreaZip]:
    """County ZIPs within the cutoff. A ZIP with no Gazetteer point is left out."""
    if practice_zip not in points:
        raise SystemExit(f"The practice ZIP {practice_zip} has no Census Gazetteer point.")
    origin = points[practice_zip]
    area: dict[str, AreaZip] = {}
    for zip_code, county in counties.items():
        if zip_code not in points:
            continue
        miles = distance_miles(origin, points[zip_code])
        if miles <= max_miles:
            area[zip_code] = AreaZip(county, miles, practice_zip)
    if practice_zip not in area:
        raise SystemExit(f"The practice ZIP {practice_zip} is not in {sorted(set(counties.values()))}.")
    return area


def practice_area() -> dict[str, AreaZip]:
    """LA and Orange County ZIPs within the cutoff of the practice ZIP."""
    return area_from(county_postal_codes(), zcta_points())


def in_area(practice_zip: object, county: object, distance: object) -> bool:
    """True when a stored row's practice fields put it inside the current practice area."""
    if str(practice_zip or "").strip() != PRACTICE_ZIP_CODE:
        return False
    if str(county or "").strip() not in COUNTIES.values():
        return False
    text = str(distance if distance is not None else "").strip()
    if not text:
        return False
    try:
        return Decimal(text) <= MAX_DISTANCE_MILES
    except Exception:
        return False
