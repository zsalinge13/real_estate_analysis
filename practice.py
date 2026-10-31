"""The practice ZIP and the ZIP codes the pipeline covers.

PRACTICE_ZIP_CODE is the one setting for where the practice is. A ZIP is in
the practice area when two things are true. First, the Census 2020
ZCTA-to-county relationship file gives a county in COUNTIES strictly more
land area in that ZCTA than any other county, with water area breaking a
land tie. A tie on both is not a majority, and the place name is not used.
Second, its distance from the practice ZIP is MAX_PRACTICE_DISTANCE_MILES or less.

Distance is the haversine great-circle distance between the Census 2024
Gazetteer ZCTA internal points (INTPTLAT, INTPTLONG), on a sphere of
3,958.8 miles, rounded to two decimals. The cutoff is applied to the
rounded distance.
"""

from __future__ import annotations

import functools
import io
import math
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd

PRACTICE_ZIP_CODE = "90210"
# Patients rarely drive more than 50 miles to a new practice that is still
# building its reputation, so ZIPs farther away are not marketed to.
MAX_PRACTICE_DISTANCE_MILES = Decimal(50)
DISTANCE_RULE = (
    f"Only ZIP codes within {MAX_PRACTICE_DISTANCE_MILES} miles of the practice "
    f"(ZIP {PRACTICE_ZIP_CODE}) are included, because patients rarely drive more than "
    f"{MAX_PRACTICE_DISTANCE_MILES} miles to a new practice that is still building its reputation."
)
# County FIPS to the name stored in the county field.
COUNTIES = {"06037": "Los Angeles", "06059": "Orange"}
CALIFORNIA_FIPS = "06"
FIELDS = ("practice_zip_code", "county", "distance_from_practice_miles")
AREA_PHRASE = f"ZIP codes within {MAX_PRACTICE_DISTANCE_MILES} miles of the practice"

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


CROSSWALK_COLUMNS = ("GEOID_ZCTA5_20", "GEOID_COUNTY_20", "AREALAND_PART", "AREAWATER_PART")


def majority_county_fips(text: str) -> pd.Series:
    """ZCTA to the FIPS of its majority county, for every ZCTA in the crosswalk.

    The majority county has strictly more land than any other county, with water
    breaking a land tie. A tie on both is not a majority.
    """
    header = text.split("\n", 1)[0].strip().split("|")
    missing = [name for name in CROSSWALK_COLUMNS if name not in header]
    if missing:
        raise SystemExit(f"Census crosswalk is missing columns: {sorted(missing)}")
    frame = pd.read_csv(
        io.StringIO(text), sep="|", usecols=list(CROSSWALK_COLUMNS), dtype=str, keep_default_na=False
    )
    frame.columns = ["zcta", "fips", "land", "water"]
    frame["zcta"] = frame["zcta"].str.strip()
    frame["fips"] = frame["fips"].str.strip()
    frame["land"] = pd.to_numeric(frame["land"].replace("", "0"), errors="coerce")
    frame["water"] = pd.to_numeric(frame["water"].replace("", "0"), errors="coerce")
    frame = frame[
        frame["zcta"].str.fullmatch(r"\d{5}")
        & frame["fips"].str.fullmatch(r"\d{5}")
        & frame["land"].notna()
        & frame["water"].notna()
    ]
    parts = frame.groupby(["zcta", "fips"], as_index=False)[["land", "water"]].sum()
    parts = parts.sort_values(["zcta", "land", "water"], ascending=[True, False, False])
    first = parts.groupby("zcta").nth(0).set_index("zcta")
    second = parts.groupby("zcta").nth(1).set_index("zcta").reindex(first.index)
    tied = (first["land"] == second["land"]) & (first["water"] == second["water"])
    empty = (first["land"] == 0) & (first["water"] == 0)
    return first.loc[~tied & ~empty, "fips"]


def majority_counties(text: str, counties: dict[str, str] = COUNTIES) -> dict[str, str]:
    """ZCTA to county name, for ZCTAs whose majority county is in `counties`."""
    fips = majority_county_fips(text)
    kept = fips[fips.isin(list(counties))].map(counties).to_dict()
    if not kept:
        raise SystemExit(f"Census crosswalk did not identify any ZIPs in {sorted(counties)}.")
    return kept


def crosswalk_text() -> str:
    return fetch_bytes(CROSSWALK_URL, "Census ZCTA-county crosswalk").decode("utf-8-sig")


def county_postal_codes() -> dict[str, str]:
    return majority_counties(crosswalk_text())


def gazetteer_frame(text: str) -> pd.DataFrame:
    """ZCTA internal points, indexed by GEOID, with float lat and lon columns."""
    if not text.strip():
        raise SystemExit("The ZCTA gazetteer is empty.")
    frame = pd.read_csv(io.StringIO(text), sep="\t", dtype=str, keep_default_na=False)
    frame.columns = [name.strip() for name in frame.columns]
    missing = [name for name in ("GEOID", "INTPTLAT", "INTPTLONG") if name not in frame.columns]
    if missing:
        raise SystemExit(f"The ZCTA gazetteer is missing a column: {missing}")
    points = pd.DataFrame(
        {
            "lat": pd.to_numeric(frame["INTPTLAT"].str.strip(), errors="coerce"),
            "lon": pd.to_numeric(frame["INTPTLONG"].str.strip(), errors="coerce"),
        }
    )
    points.index = frame["GEOID"].str.strip()
    points = points.dropna()
    if points.empty:
        raise SystemExit("The ZCTA gazetteer has no points.")
    return points


def parse_gazetteer(text: str) -> dict[str, tuple[float, float]]:
    frame = gazetteer_frame(text)
    return dict(zip(frame.index, zip(frame["lat"], frame["lon"])))


def gazetteer_text() -> str:
    archive = zipfile.ZipFile(io.BytesIO(fetch_bytes(GAZETTEER_URL, "Census gazetteer download")))
    name = next(entry for entry in archive.namelist() if entry.endswith(".txt"))
    return archive.read(name).decode("utf-8-sig")


def zcta_points() -> dict[str, tuple[float, float]]:
    return parse_gazetteer(gazetteer_text())


def miles_from(origin: tuple[float, float], lats, lons) -> np.ndarray:
    """Vectorized haversine distance in miles from `origin` to each point."""
    lat1, lon1 = np.radians(origin[0]), np.radians(origin[1])
    lat2, lon2 = np.radians(np.asarray(lats, dtype=float)), np.radians(np.asarray(lons, dtype=float))
    h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(h))


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
    max_miles: Decimal = MAX_PRACTICE_DISTANCE_MILES,
) -> dict[str, AreaZip]:
    """County ZIPs within the cutoff. A ZIP with no Gazetteer point is left out."""
    located = locations_from(counties, points, practice_zip)
    area = {zip_code: place for zip_code, place in located.items() if place.distance_from_practice_miles <= max_miles}
    if practice_zip not in area:
        raise SystemExit(f"The practice ZIP {practice_zip} is not in {sorted(set(counties.values()))}.")
    return area


def locations_from(
    counties: dict[str, str],
    points: dict[str, tuple[float, float]] | pd.DataFrame,
    practice_zip: str = PRACTICE_ZIP_CODE,
) -> dict[str, AreaZip]:
    """Each ZIP in `counties` that has a Gazetteer point, with its county and distance.

    The haversine runs once over all the ZIPs as numpy arrays.
    """
    frame = points if isinstance(points, pd.DataFrame) else pd.DataFrame.from_dict(
        points, orient="index", columns=["lat", "lon"]
    )
    if practice_zip not in frame.index:
        raise SystemExit(f"The practice ZIP {practice_zip} has no Census Gazetteer point.")
    origin = tuple(frame.loc[practice_zip, ["lat", "lon"]])
    codes = [zip_code for zip_code in counties if zip_code in frame.index]
    located = frame.loc[codes]
    miles = miles_from(origin, located["lat"].to_numpy(), located["lon"].to_numpy())
    return {
        zip_code: AreaZip(
            counties[zip_code],
            Decimal(str(float(value))).quantize(DISTANCE_PLACES, rounding=ROUND_HALF_UP),
            practice_zip,
        )
        for zip_code, value in zip(codes, miles)
    }


@functools.lru_cache(maxsize=1)
def california_locations() -> dict[str, AreaZip]:
    """Every California ZCTA with a Gazetteer point, with its distance from the practice ZIP.

    Computed once per run. The county is the LA or Orange County name, or blank
    when the majority county is any other California county.
    """
    fips = majority_county_fips(crosswalk_text())
    california = fips[fips.str.startswith(CALIFORNIA_FIPS)]
    names = {zcta: COUNTIES.get(code, "") for zcta, code in california.items()}
    return locations_from(names, gazetteer_frame(gazetteer_text()))


@functools.lru_cache(maxsize=1)
def practice_area() -> dict[str, AreaZip]:
    """LA and Orange County ZIPs within the cutoff of the practice ZIP. This is the eligible set."""
    located = {
        zip_code: place for zip_code, place in california_locations().items() if place.county in COUNTIES.values()
    }
    area = {
        zip_code: place
        for zip_code, place in located.items()
        if place.distance_from_practice_miles <= MAX_PRACTICE_DISTANCE_MILES
    }
    if PRACTICE_ZIP_CODE not in area:
        raise SystemExit(f"The practice ZIP {PRACTICE_ZIP_CODE} is not in {sorted(COUNTIES.values())}.")
    print(
        f"Excluded {len(located) - len(area)} of {len(located)} {' and '.join(COUNTIES.values())} County ZIPs "
        f"for being more than {MAX_PRACTICE_DISTANCE_MILES} miles from the practice ZIP {PRACTICE_ZIP_CODE}.",
        flush=True,
    )
    return area


def beyond_distance(distance: object) -> bool:
    """True when a stored distance is blank, unreadable, or over MAX_PRACTICE_DISTANCE_MILES."""
    text = str(distance if distance is not None else "").strip()
    try:
        return not text or Decimal(text) > MAX_PRACTICE_DISTANCE_MILES
    except Exception:
        return True


def split_by_area(rows, fields) -> tuple[list, int, int]:
    """(rows in the area, rows excluded for distance, rows excluded otherwise).

    fields(row) returns that row's (practice_zip_code, county, distance).
    """
    kept: list = []
    far = other = 0
    for row in rows:
        practice_zip, county, distance = fields(row)
        if in_area(practice_zip, county, distance):
            kept.append(row)
        elif str(practice_zip or "").strip() == PRACTICE_ZIP_CODE and beyond_distance(distance):
            far += 1
        else:
            other += 1
    return kept, far, other


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
        return Decimal(text) <= MAX_PRACTICE_DISTANCE_MILES
    except Exception:
        return False
