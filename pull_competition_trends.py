"""Competitor counts and Google search interest per practice-area ZIP and procedure.

The grain is one row per ZIP in `zip_census_redfin_join` within the practice
area (see practice.py) times one `procedure` from `procedure_surgeon_fees`.
Tableau joins this file to the procedure fee file on `zip_code` and
`procedure`.

Competition comes from the CMS NPPES NPI Registry API. A provider is an active
individual NPI (NPI-1) counted at the ZIP of its primary practice location.
A provider is in a specialty when any of its listed taxonomies matches that
specialty's codes. Med spas are not in NPPES and are not counted.

The 5-mile counts add every Census ZCTA whose internal point is within 5 miles
of this ZIP's internal point, including this ZIP, in any county and whether or
not it is in the join. Adults for a ZCTA outside the join come from the same
ACS vintage as the join. When any of those ZCTAs has no adult count, the
5-mile adults and densities are blank.

NPPES is read on every run. When NPPES or the gazetteer fails, each ZIP and
procedure keeps its last good competition values from
`public.competition_trends`, with their original `competition_pulled_at`, and
the run still writes the CSV.

Search interest comes from Google Trends through pytrends. That service is
unofficial and rate-limited. A Trends error never stops the run: the last good
values in `competition_trends_cache.json` are kept, or read back from
`public.competition_trends` when the cache file is missing. A pull newer than
six and a half days is reused without asking Google again, so the daily run
asks Google about once a week.

The CSV is `competition_trends.csv`. When DATABASE_URL is set, the rows replace
`public.competition_trends`. No other table is modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import dataset_stamp
import load_california
import practice
import pull_census_zip_population as census

ROOT = Path(__file__).resolve().parent
TABLE_NAME = "competition_trends"
TABLE = "public.competition_trends"
OUTPUT_CSV_NAME = "competition_trends.csv"
OUTPUT_CSV = ROOT / OUTPUT_CSV_NAME
CACHE_JSON_NAME = "competition_trends_cache.json"
CACHE_JSON = ROOT / CACHE_JSON_NAME
JOIN_TABLE = "public.zip_census_redfin_join"
PROCEDURE_TABLE = "public.procedure_surgeon_fees"
PROCEDURE_SEED = ROOT / "seeds" / "procedure_surgeon_fees_2024.csv"
USER_AGENT = load_california.USER_AGENT

NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"
NPPES_PAGE = 200
NPPES_MAX_SKIP = 1000
NPPES_CAP = NPPES_MAX_SKIP + NPPES_PAGE
NPPES_PAUSE_SECONDS = 0.3

RADIUS_MILES = 5.0
# A degree of latitude is 69.09 miles on practice.EARTH_RADIUS_MILES.
MILES_PER_DEGREE_LATITUDE = 69.0

TRENDS_GEO = "US-CA-803"
TRENDS_DMA_NAME = "Los Angeles CA (DMA 803)"
TRENDS_TIMEFRAME = "today 5-y"
TRENDS_ANCHOR = "liposuction"
TRENDS_BATCH = 5
TRENDS_WEEKS = 52
# Under seven days so a daily run that starts a little earlier than last
# week's still asks Google on the seventh day.
TRENDS_REUSE_AGE = timedelta(days=6, hours=12)
TRENDS_DEADLINE_SECONDS = 25 * 60
TRENDS_ATTEMPTS = 4

STATUS_NEW = "new"
STATUS_RECENT = "reused recent pull"
STATUS_KEPT = "kept last good value"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Specialty:
    key: str
    label: str
    query: str
    codes: tuple[str, ...]

    def matches(self, code: str) -> bool:
        return any(code.startswith(prefix) for prefix in self.codes)


SPECIALTIES = (
    Specialty("plastic_surgeon", "plastic surgery", "Plastic", ("2082",)),
    Specialty("dermatologist", "dermatology", "Dermatology", ("207N",)),
    Specialty(
        "facial_plastic_surgeon",
        "facial plastic surgery",
        "Plastic",
        ("207YS0123X", "207YX0905X", "207YX0007X"),
    ),
    Specialty(
        "oculoplastic_surgeon",
        "ophthalmic plastic surgery",
        "Plastic",
        ("207WX0200X",),
    ),
)
SPECIALTY_BY_KEY = {specialty.key: specialty for specialty in SPECIALTIES}
NPPES_QUERIES = tuple(dict.fromkeys(specialty.query for specialty in SPECIALTIES))

FACE = ("plastic_surgeon", "facial_plastic_surgeon")
EYE_AND_BROW = FACE + ("oculoplastic_surgeon",)
PROCEDURE_COMPETITORS = {
    "facelift": FACE,
    "neck lift": FACE,
    "rhinoplasty": FACE,
    "eyelid surgery": EYE_AND_BROW,
    "forehead lift": EYE_AND_BROW,
}
DEFAULT_COMPETITORS = ("plastic_surgeon",)

TRENDS_TERMS = {
    "breast augmentation": "breast augmentation",
    "breast lift": "breast lift",
    "breast reduction": "breast reduction",
    "liposuction": "liposuction",
    "tummy tuck": "tummy tuck",
    "arm lift": "arm lift",
    "thigh lift": "thigh lift",
    "buttock augmentation with fat grafting": "brazilian butt lift",
    "eyelid surgery": "blepharoplasty",
    "facelift": "facelift",
    "neck lift": "neck lift",
    "rhinoplasty": "rhinoplasty",
    "forehead lift": "brow lift",
}

COMPETITION_SOURCE = (
    "CMS NPPES NPI Registry API v2.1: active individual NPIs (NPI-1) at their "
    "primary practice location ZIP"
)
CENTROID_SOURCE = "Census 2024 Gazetteer ZCTA internal points"
TRENDS_SOURCE = (
    "Google Trends via pytrends (unofficial), Los Angeles CA DMA 803, "
    "web search, past 5 years"
)
COMPETITION_LIMITATIONS = (
    "Med spas, nurse injectors, and other businesses without a physician NPI "
    "are not counted. NPPES addresses are self-reported and can be out of date. "
    "Search interest is for the Los Angeles DMA 803, which includes Orange County."
)

COLUMNS: tuple[tuple[str, str], ...] = (
    ("zip_code", "text"),
    ("city", "text"),
    ("practice_zip_code", "text"),
    ("county", "text"),
    ("distance_from_practice_miles", "numeric"),
    ("procedure", "text"),
    ("plastic_surgeon_count", "integer"),
    ("dermatologist_count", "integer"),
    ("facial_plastic_surgeon_count", "integer"),
    ("oculoplastic_surgeon_count", "integer"),
    ("plastic_surgeon_count_5mi", "integer"),
    ("dermatologist_count_5mi", "integer"),
    ("facial_plastic_surgeon_count_5mi", "integer"),
    ("oculoplastic_surgeon_count_5mi", "integer"),
    ("adult_population", "integer"),
    ("adult_population_5mi", "integer"),
    ("plastic_surgeons_per_10k_adults", "numeric"),
    ("dermatologists_per_10k_adults", "numeric"),
    ("plastic_surgeons_per_10k_adults_5mi", "numeric"),
    ("dermatologists_per_10k_adults_5mi", "numeric"),
    ("procedure_competitor_specialties", "text"),
    ("procedure_competitor_count", "integer"),
    ("procedure_competitor_count_5mi", "integer"),
    ("procedure_competitors_per_10k_adults_5mi", "numeric"),
    ("competition_pulled_at", "timestamptz"),
    ("competition_status", "text"),
    ("competition_source", "text"),
    ("centroid_source", "text"),
    ("competition_limitations", "text"),
    ("trends_search_term", "text"),
    ("trends_dma", "text"),
    ("trends_dma_interest_12mo_avg", "numeric"),
    ("trends_dma_interest_prior_12mo_avg", "numeric"),
    ("trends_dma_interest_change_vs_prior_12mo", "numeric"),
    ("trends_status", "text"),
    ("trends_pulled_at", "timestamptz"),
    ("trends_city_name", "text"),
    ("trends_city_interest_5yr", "integer"),
    ("trends_city_status", "text"),
    ("trends_city_pulled_at", "timestamptz"),
    ("trends_source", "text"),
    (dataset_stamp.COLUMN, dataset_stamp.SQL_TYPE),
)
COMPETITION_COUNT_COLUMNS = (
    *(f"{specialty.key}_count" for specialty in SPECIALTIES),
    *(f"{specialty.key}_count_5mi" for specialty in SPECIALTIES),
    "adult_population_5mi",
    "plastic_surgeons_per_10k_adults",
    "dermatologists_per_10k_adults",
    "plastic_surgeons_per_10k_adults_5mi",
    "dermatologists_per_10k_adults_5mi",
    "procedure_competitor_count",
    "procedure_competitor_count_5mi",
    "procedure_competitors_per_10k_adults_5mi",
    "competition_pulled_at",
)
COLUMN_NAMES = tuple(name for name, _ in COLUMNS)
COLUMN_TYPES = dict(COLUMNS)
NOT_NULL_COLUMNS = {"zip_code", "procedure", dataset_stamp.COLUMN}


# ---------------------------------------------------------------------------
# Inputs


@dataclass(frozen=True)
class ZipArea:
    zip_code: str
    city: str
    adult_population: int | None
    county: str | None = None
    distance_from_practice_miles: Decimal | None = None
    practice_zip_code: str = practice.PRACTICE_ZIP_CODE
    acs_year: int | None = None


def split_city(zip_name: str) -> str:
    """City from the Realtor.com zip_name, such as "marina del rey, ca"."""
    name = (zip_name or "").strip()
    city, comma, _state = name.rpartition(",")
    return city.strip() if comma else name


def parse_whole(raw: object) -> int | None:
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    try:
        return int(Decimal(text))
    except Exception:
        return None


def zip_areas_from_rows(rows: Iterable[dict]) -> list[ZipArea]:
    """Join ZIPs inside the practice area. Rows outside it are left out."""
    areas: dict[str, ZipArea] = {}
    for row in rows:
        key = load_california.postal_code_key(str(row.get("zip") or ""))
        if key is None:
            continue
        fields = [row.get(name) for name in practice.FIELDS]
        if not practice.in_area(*fields):
            continue
        areas[key] = ZipArea(
            zip_code=key,
            city=split_city(str(row.get("zip_name") or "")),
            adult_population=parse_whole(row.get("adult_population")),
            county=str(fields[1]).strip(),
            distance_from_practice_miles=Decimal(str(fields[2]).strip()),
            practice_zip_code=str(fields[0]).strip(),
            acs_year=parse_whole(row.get("acs_year")),
        )
    if not areas:
        raise SystemExit(f"The ZIP join has no rows for {practice.AREA_PHRASE}.")
    return [areas[key] for key in sorted(areas)]


def join_acs_year(areas: list[ZipArea]) -> int | None:
    years = {area.acs_year for area in areas if area.acs_year is not None}
    if len(years) > 1:
        raise SystemExit(f"The ZIP join has more than one acs_year: {sorted(years)}.")
    return next(iter(years), None)


def read_csv_rows(path: Path, required: tuple[str, ...]) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"{path} is missing.")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in required if name not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path.name} is missing columns: {missing}")
        return list(reader)


JOIN_INPUT_COLUMNS = ("zip", "zip_name", "adult_population", "acs_year") + practice.FIELDS


def read_zip_areas(path: Path | None) -> list[ZipArea]:
    if path is not None:
        return zip_areas_from_rows(read_csv_rows(path, JOIN_INPUT_COLUMNS))
    if not database_url():
        raise SystemExit(
            "Pass --join zip_census_redfin_join.csv or set DATABASE_URL."
        )
    with load_california.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(JOIN_INPUT_COLUMNS)} FROM {JOIN_TABLE}")
        return zip_areas_from_rows(
            dict(zip(JOIN_INPUT_COLUMNS, values)) for values in cur.fetchall()
        )


def procedures_from_rows(rows: Iterable[dict]) -> list[str]:
    names = {
        str(row.get("procedure") or "").strip()
        for row in rows
    }
    names.discard("")
    return sorted(names)


def read_procedures(path: Path | None) -> list[str]:
    if path is not None:
        names = procedures_from_rows(read_csv_rows(path, ("procedure",)))
        if names:
            return names
    if database_url():
        try:
            with load_california.connect() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT DISTINCT procedure FROM {PROCEDURE_TABLE}")
                names = procedures_from_rows({"procedure": p} for (p,) in cur.fetchall())
            if names:
                return names
        except Exception as exc:
            print(f"Could not read {PROCEDURE_TABLE} ({type(exc).__name__}). Using the seed.")
    names = procedures_from_rows(read_csv_rows(PROCEDURE_SEED, ("procedure",)))
    if not names:
        raise SystemExit("No procedure names were found.")
    return names


def database_url() -> str:
    return os.environ.get(load_california.DATABASE_URL_ENV, "").strip()


# ---------------------------------------------------------------------------
# HTTP


def http_get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def with_retries(
    call: Callable[[], object],
    attempts: int,
    first_wait: float,
    sleep: Callable[[float], None] = time.sleep,
    what: str = "request",
):
    """Run call, waiting longer after each failure. Re-raises the last error."""
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            if attempt == attempts - 1:
                raise
            wait = first_wait * (2**attempt) + random.uniform(0, first_wait)
            print(f"{what} failed ({type(exc).__name__}); retrying in {wait:.0f} s.")
            sleep(wait)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# NPPES


@dataclass
class Provider:
    npi: str
    zip_code: str
    codes: set[str] = field(default_factory=set)

    def specialties(self) -> set[str]:
        return {
            specialty.key
            for specialty in SPECIALTIES
            if any(specialty.matches(code) for code in self.codes)
        }


def practice_zip(result: dict) -> str | None:
    for address in result.get("addresses") or []:
        if address.get("address_purpose") == "LOCATION":
            return load_california.postal_code_key(str(address.get("postal_code") or ""))
    return None


def provider_from_result(result: dict) -> Provider | None:
    """An active individual provider with a practice-location ZIP, else None."""
    if result.get("enumeration_type") != "NPI-1":
        return None
    if (result.get("basic") or {}).get("status") != "A":
        return None
    zip_code = practice_zip(result)
    npi = str(result.get("number") or "").strip()
    if zip_code is None or not npi:
        return None
    codes = {
        str(taxonomy.get("code") or "").strip()
        for taxonomy in result.get("taxonomies") or []
    }
    codes.discard("")
    return Provider(npi=npi, zip_code=zip_code, codes=codes)


def nppes_fetch_json(params: dict) -> dict:
    query = urllib.parse.urlencode({"version": "2.1", **params})
    body = with_retries(
        lambda: http_get(f"{NPPES_URL}?{query}", timeout=60),
        attempts=5,
        first_wait=4,
        what="NPPES request",
    )
    data = json.loads(body)
    if data.get("Errors"):
        raise SystemExit(f"NPPES rejected the query: {data['Errors']}")
    return data


def nppes_prefix(
    query: str,
    prefix: str,
    fetch_json: Callable[[dict], dict],
    pause: Callable[[], None],
) -> list[dict]:
    """Every result for one ZIP prefix. A prefix at the API cap is split by its next digit."""
    results: list[dict] = []
    for skip in range(0, NPPES_MAX_SKIP + 1, NPPES_PAGE):
        page = fetch_json(
            {
                "taxonomy_description": query,
                "postal_code": f"{prefix}*",
                "state": "CA",
                "address_purpose": "LOCATION",
                "enumeration_type": "NPI-1",
                "limit": NPPES_PAGE,
                "skip": skip,
            }
        ).get("results") or []
        pause()
        results.extend(page)
        if len(page) < NPPES_PAGE:
            return results
    if len(prefix) >= 5:
        raise SystemExit(f"NPPES has more than {NPPES_CAP} {query} rows in ZIP {prefix}.")
    split: list[dict] = []
    for digit in "0123456789":
        split.extend(nppes_prefix(query, prefix + digit, fetch_json, pause))
    return split


def fetch_providers(
    zip_codes: Iterable[str],
    fetch_json: Callable[[dict], dict] = nppes_fetch_json,
    pause: Callable[[], None] = lambda: time.sleep(NPPES_PAUSE_SECONDS),
) -> dict[str, Provider]:
    """Providers in the 3-digit prefixes of these ZIPs, one entry per NPI."""
    prefixes = sorted({code[:3] for code in zip_codes})
    providers: dict[str, Provider] = {}
    for query in NPPES_QUERIES:
        for prefix in prefixes:
            for result in nppes_prefix(query, prefix, fetch_json, pause):
                provider = provider_from_result(result)
                if provider is None:
                    continue
                known = providers.get(provider.npi)
                if known is None:
                    providers[provider.npi] = provider
                else:
                    known.codes |= provider.codes
    return providers


# ---------------------------------------------------------------------------
# Geography


def fetch_centroids() -> dict[str, tuple[float, float]]:
    return with_retries(
        practice.zcta_points,
        attempts=4,
        first_wait=4,
        what="Census gazetteer download",
    )


def neighbors_within(
    zip_codes: list[str],
    centroids: dict[str, tuple[float, float]],
    miles: float = RADIUS_MILES,
) -> dict[str, list[str]]:
    """Every ZCTA within `miles` of each ZIP, itself included, in any county.

    A ZIP with no point is left out.
    """
    lat_reach = miles / MILES_PER_DEGREE_LATITUDE
    points = sorted(centroids.items())
    near: dict[str, list[str]] = {}
    for code in zip_codes:
        origin = centroids.get(code)
        if origin is None:
            continue
        near[code] = [
            other
            for other, point in points
            if abs(point[0] - origin[0]) <= lat_reach
            and practice.miles_between(origin, point) <= miles
        ]
    return near


def outside_neighbors(near: dict[str, list[str]], zip_codes: Iterable[str]) -> list[str]:
    """ZCTAs within the radius of some ZIP that are not themselves in zip_codes."""
    inside = set(zip_codes)
    return sorted({other for nearby in near.values() for other in nearby} - inside)


def adults_from_records(records: Iterable[dict[str, str]]) -> dict[str, int | None]:
    bands = census.band_codes()
    adults: dict[str, int | None] = {}
    for record in records:
        zip_code = (record.get(census.GEO_COLUMN) or "").strip()
        if not zip_code:
            continue
        adults[zip_code] = census.sum_counts(
            [
                census.sum_counts(
                    [census.as_int(census.parse_estimate(record.get(code)), column) for code in codes]
                )
                for column, codes in bands.items()
            ]
        )
    return adults


def fetch_adults(
    year: int,
    zip_codes: list[str],
    fetch_piece: Callable[[int, list[str], tuple[str, ...]], list[dict[str, str]]] = census.fetch_field_piece,
    pause: Callable[[], None] = lambda: time.sleep(census.CALL_PAUSE_SECONDS),
) -> dict[str, int | None]:
    """ACS adults 18 and older per ZCTA, summed the same way as the Census pull."""
    variables = dict(census.field_groups())["age_bands"]
    adults: dict[str, int | None] = {}
    for index, batch in enumerate(census.chunks(zip_codes, census.BATCH_SIZE)):
        if index:
            pause()
        adults.update(adults_from_records(fetch_piece(year, list(batch), variables)))
    return adults


# ---------------------------------------------------------------------------
# Competition


@dataclass
class ZipCompetition:
    by_specialty: dict[str, set[str]]
    by_specialty_5mi: dict[str, set[str]] | None
    adults: int | None
    adults_5mi: int | None


def per_10k(count: int | None, adults: int | None) -> Decimal | None:
    if count is None or not adults:
        return None
    value = Decimal(count) * Decimal(10_000) / Decimal(adults)
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def competition_by_zip(
    areas: list[ZipArea],
    providers: dict[str, Provider],
    centroids: dict[str, tuple[float, float]],
    outside_adults: dict[str, int | None] | None = None,
    near: dict[str, list[str]] | None = None,
) -> dict[str, ZipCompetition]:
    """Counts per join ZIP. outside_adults holds adults for neighbors outside the join."""
    codes = [area.zip_code for area in areas]
    if near is None:
        near = neighbors_within(codes, centroids)
    adults: dict[str, int | None] = dict(outside_adults or {})
    adults.update({area.zip_code: area.adult_population for area in areas})
    counted = set(codes).union(*near.values())
    in_zip: dict[str, dict[str, set[str]]] = {
        code: {specialty.key: set() for specialty in SPECIALTIES} for code in counted
    }
    for provider in providers.values():
        sets = in_zip.get(provider.zip_code)
        if sets is None:
            continue
        for key in provider.specialties():
            sets[key].add(provider.npi)
    result: dict[str, ZipCompetition] = {}
    for code in codes:
        nearby = near.get(code)
        within: dict[str, set[str]] | None = None
        adults_5mi: int | None = None
        if nearby is not None:
            within = {
                key: set().union(*(in_zip[other][key] for other in nearby))
                for key in in_zip[code]
            }
            values = [adults.get(other) for other in nearby]
            adults_5mi = None if None in values else sum(values)
        result[code] = ZipCompetition(in_zip[code], within, adults[code], adults_5mi)
    return result


def competitors_for(procedure: str) -> tuple[str, ...]:
    return PROCEDURE_COMPETITORS.get(procedure, DEFAULT_COMPETITORS)


def stored_competition() -> dict[tuple[str, str], dict]:
    """Last good competition columns per (zip_code, procedure) from public.competition_trends."""
    if not database_url():
        return {}
    names = ", ".join(COMPETITION_COUNT_COLUMNS)
    try:
        with load_california.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (TABLE,))
            if cur.fetchone()[0] is None:
                return {}
            cur.execute(f"SELECT zip_code, procedure, {names} FROM {TABLE}")
            rows = cur.fetchall()
    except Exception as exc:
        print(f"Could not read the last competition values from {TABLE} ({type(exc).__name__}).")
        return {}
    return {
        (zip_code, procedure): dict(zip(COMPETITION_COUNT_COLUMNS, values))
        for zip_code, procedure, *values in rows
    }


def pull_competition(
    areas: list[ZipArea],
    fetch_points: Callable[[], dict[str, tuple[float, float]]] | None = None,
    fetch_nppes: Callable[[list[str]], dict[str, Provider]] | None = None,
    fetch_outside_adults: Callable[[int, list[str]], dict[str, int | None]] | None = None,
) -> dict[str, ZipCompetition] | None:
    """Counts from this run's gazetteer and NPPES pull, or None when either fails."""
    fetch_points = fetch_points or fetch_centroids
    fetch_nppes = fetch_nppes or fetch_providers
    fetch_outside_adults = fetch_outside_adults or fetch_adults
    codes = [area.zip_code for area in areas]
    try:
        centroids = fetch_points()
        near = neighbors_within(codes, centroids)
        outside = outside_neighbors(near, codes)
        providers = fetch_nppes([*codes, *outside])
    except (Exception, SystemExit) as exc:
        print(
            f"The NPPES or gazetteer pull failed ({type(exc).__name__}: {exc}). "
            "Last good competition values are kept."
        )
        return None
    print(f"NPPES: {len(providers)} active individual providers in the ZIP prefixes read.")
    outside_adults: dict[str, int | None] = {}
    year = join_acs_year(areas)
    if outside and year is None:
        print("The ZIP join has no acs_year. 5-mile adults that need ZCTAs outside the join are blank.")
    elif outside:
        try:
            outside_adults = fetch_outside_adults(year, outside)
        except (Exception, SystemExit) as exc:
            print(
                f"ACS {year} adults for ZCTAs outside the join failed "
                f"({census.public_error(exc)}). 5-mile adults that need them are blank."
            )
        print(
            f"{len(outside)} ZCTAs within {RADIUS_MILES:g} miles are outside the join; "
            f"ACS {year} adults found for "
            f"{sum(value is not None for value in outside_adults.values())}."
        )
    return competition_by_zip(areas, providers, centroids, outside_adults, near)


# ---------------------------------------------------------------------------
# Google Trends


@dataclass
class TrendsPart:
    values: dict
    pulled_at: str


@dataclass
class TrendsEntry:
    term: str
    dma: TrendsPart | None = None
    city: TrendsPart | None = None
    dma_status: str = STATUS_UNAVAILABLE
    city_status: str = STATUS_UNAVAILABLE


def iso(moment: datetime) -> str:
    return dataset_stamp.stamp(moment).isoformat()


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return dataset_stamp.stamp(datetime.fromisoformat(text))
    except ValueError:
        return None


def term_for(procedure: str) -> str:
    return TRENDS_TERMS.get(procedure, procedure)


def dma_stats(series: list[float]) -> dict | None:
    """Mean of the latest 52 complete weeks, the 52 before, and the change ratio."""
    if len(series) < 2 * TRENDS_WEEKS:
        return None
    latest = series[-TRENDS_WEEKS:]
    prior = series[-2 * TRENDS_WEEKS : -TRENDS_WEEKS]
    latest_avg = sum(latest) / TRENDS_WEEKS
    prior_avg = sum(prior) / TRENDS_WEEKS
    change = (latest_avg - prior_avg) / prior_avg if prior_avg else None
    return {"latest_avg": latest_avg, "prior_avg": prior_avg, "change": change}


def batches(terms: list[str]) -> list[list[str]]:
    """Each request holds the anchor term plus up to four others."""
    others = [term for term in terms if term != TRENDS_ANCHOR]
    size = TRENDS_BATCH - 1
    groups = [others[i : i + size] for i in range(0, len(others), size)] or [[]]
    return [[TRENDS_ANCHOR, *group] for group in groups]


class TrendsSource:
    """Live Google Trends through pytrends, with pacing and back-off."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep):
        from pytrends.request import TrendReq

        self.sleep = sleep
        self.client = TrendReq(hl="en-US", tz=0, timeout=(10, 30))
        self.started = time.monotonic()
        self.calls = 0

    def _call(self, what: str, call: Callable[[], object]):
        if time.monotonic() - self.started > TRENDS_DEADLINE_SECONDS:
            raise TimeoutError("The Google Trends time budget for this run is used up.")
        if self.calls:
            self.sleep(12 + random.uniform(0, 8))
        self.calls += 1
        return with_retries(call, TRENDS_ATTEMPTS, 60, self.sleep, what)

    def weekly(self, terms: list[str]) -> dict[str, list[float]]:
        def call():
            self.client.build_payload(terms, timeframe=TRENDS_TIMEFRAME, geo=TRENDS_GEO)
            frame = self.client.interest_over_time()
            if frame is None or frame.empty:
                raise ValueError("Google Trends returned no weekly rows.")
            if "isPartial" in frame.columns:
                frame = frame[~frame["isPartial"].astype(bool)]
            return {term: [float(v) for v in frame[term].tolist()] for term in terms}

        return self._call(f"Google Trends weekly {terms}", call)

    def by_city(self, term: str) -> dict[str, int]:
        def call():
            self.client.build_payload([term], timeframe=TRENDS_TIMEFRAME, geo=TRENDS_GEO)
            frame = self.client.interest_by_region(resolution="CITY", inc_low_vol=True)
            if frame is None:
                raise ValueError("Google Trends returned no city rows.")
            return {str(name): int(value) for name, value in frame[term].items()}

        return self._call(f"Google Trends cities {term!r}", call)


def load_cache(path: Path) -> dict[str, TrendsEntry]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"{path.name} could not be read. Starting without a Trends cache.")
        return {}
    entries: dict[str, TrendsEntry] = {}
    for procedure, raw in (data.get("procedures") or {}).items():
        entry = TrendsEntry(term=str(raw.get("term") or term_for(procedure)))
        for part in ("dma", "city"):
            value = raw.get(part)
            if isinstance(value, dict) and value.get("pulled_at"):
                setattr(entry, part, TrendsPart(value.get("values") or {}, value["pulled_at"]))
        entries[procedure] = entry
    return entries


def save_cache(path: Path, entries: dict[str, TrendsEntry]) -> None:
    data = {
        "note": "Last good Google Trends values per procedure. Read by pull_competition_trends.py.",
        "procedures": {
            procedure: {
                "term": entry.term,
                "dma": None if entry.dma is None else entry.dma.__dict__,
                "city": None if entry.city is None else entry.city.__dict__,
            }
            for procedure, entry in sorted(entries.items())
        },
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def cache_from_table() -> dict[str, TrendsEntry]:
    """Rebuild the last good values from public.competition_trends."""
    if not database_url():
        return {}
    try:
        with load_california.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass(%s)", (TABLE,)
            )
            if cur.fetchone()[0] is None:
                return {}
            cur.execute(
                f"""
                SELECT procedure, trends_search_term,
                       trends_dma_interest_12mo_avg, trends_dma_interest_prior_12mo_avg,
                       trends_dma_interest_change_vs_prior_12mo, trends_pulled_at,
                       trends_city_name, trends_city_interest_5yr, trends_city_pulled_at
                FROM {TABLE}
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        print(f"Could not read the last Trends values from {TABLE} ({type(exc).__name__}).")
        return {}
    entries: dict[str, TrendsEntry] = {}
    for procedure, term, latest, prior, change, pulled, city, city_value, city_pulled in rows:
        entry = entries.setdefault(procedure, TrendsEntry(term=term or term_for(procedure)))
        if pulled is not None and latest is not None and entry.dma is None:
            entry.dma = TrendsPart(
                {
                    "latest_avg": float(latest),
                    "prior_avg": None if prior is None else float(prior),
                    "change": None if change is None else float(change),
                },
                iso(pulled),
            )
        if city_pulled is not None:
            if entry.city is None:
                entry.city = TrendsPart({}, iso(city_pulled))
            if city and city_value is not None:
                entry.city.values[city] = int(city_value)
    return entries


def is_recent(part: TrendsPart | None, term: str, cached_term: str, now: datetime) -> bool:
    pulled = parse_iso(part.pulled_at) if part else None
    return pulled is not None and term == cached_term and now - pulled < TRENDS_REUSE_AGE


def refresh_trends(
    procedures: list[str],
    cache: dict[str, TrendsEntry],
    source_factory: Callable[[], object] | None,
    now: datetime,
) -> dict[str, TrendsEntry]:
    """Fresh values where Google answers, else the last good ones. Never raises."""
    entries: dict[str, TrendsEntry] = {}
    for procedure in procedures:
        term = term_for(procedure)
        old = cache.get(procedure)
        entry = TrendsEntry(term=term)
        if old is not None and old.term == term:
            entry.dma, entry.city = old.dma, old.city
            entry.dma_status = STATUS_KEPT if old.dma else STATUS_UNAVAILABLE
            entry.city_status = STATUS_KEPT if old.city else STATUS_UNAVAILABLE
            if is_recent(old.dma, term, old.term, now):
                entry.dma_status = STATUS_RECENT
            if is_recent(old.city, term, old.term, now):
                entry.city_status = STATUS_RECENT
        entries[procedure] = entry

    need_dma = [p for p, e in entries.items() if e.dma_status != STATUS_RECENT]
    need_city = [p for p, e in entries.items() if e.city_status != STATUS_RECENT]
    if not need_dma and not need_city:
        print("Every Google Trends value is under six days old. Google was not asked again.")
        return entries
    if source_factory is None:
        print("Google Trends was skipped for this run. Last good values were kept.")
        return entries
    try:
        source = source_factory()
    except Exception as exc:
        print(f"Google Trends is unavailable ({type(exc).__name__}). Last good values were kept.")
        return entries

    stamp_text = iso(now)
    if need_dma:
        pull_dma(entries, need_dma, source, stamp_text)
    for procedure in need_city:
        entry = entries[procedure]
        try:
            cities = source.by_city(entry.term)
        except Exception as exc:
            print(f"Google Trends cities failed for {entry.term!r} ({type(exc).__name__}).")
            continue
        entry.city = TrendsPart({name: value for name, value in cities.items() if value > 0}, stamp_text)
        entry.city_status = STATUS_NEW
    return entries


def pull_dma(
    entries: dict[str, TrendsEntry],
    procedures: list[str],
    source,
    stamp_text: str,
) -> None:
    """Weekly DMA interest, rescaled through the anchor term so procedures compare."""
    by_term: dict[str, list[str]] = {}
    for procedure in procedures:
        by_term.setdefault(entries[procedure].term, []).append(procedure)
    reference: float | None = None
    for group in batches(list(by_term)):
        try:
            series = source.weekly(group)
        except Exception as exc:
            print(f"Google Trends weekly failed for {group} ({type(exc).__name__}).")
            continue
        anchor = series.get(TRENDS_ANCHOR) or []
        anchor_mean = sum(anchor) / len(anchor) if anchor else 0.0
        if anchor_mean <= 0:
            print(f"Google Trends anchor {TRENDS_ANCHOR!r} was empty for {group}.")
            continue
        if reference is None:
            reference = anchor_mean
        scale = reference / anchor_mean
        for term in group:
            if term not in by_term:
                continue
            stats = dma_stats([value * scale for value in series.get(term) or []])
            if stats is None:
                print(f"Google Trends returned under two years of weeks for {term!r}.")
                continue
            for procedure in by_term[term]:
                entries[procedure].dma = TrendsPart(stats, stamp_text)
                entries[procedure].dma_status = STATUS_NEW


def normalize_city(name: str) -> str:
    return " ".join(name.lower().replace("-", " ").split())


def city_interest(entry: TrendsEntry, city: str) -> tuple[str | None, int | None]:
    if entry.city is None:
        return None, None
    wanted = normalize_city(city)
    for name, value in entry.city.values.items():
        if normalize_city(name) == wanted:
            return name, int(value)
    return None, None


# ---------------------------------------------------------------------------
# Rows and output


def rounded(value: float | None, places: str = "0.01") -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def competition_cells(
    comp: ZipCompetition,
    procedure: str,
    competition_pulled_at: datetime,
) -> dict:
    within = comp.by_specialty_5mi
    cells: dict = {
        "adult_population_5mi": comp.adults_5mi,
        "competition_pulled_at": competition_pulled_at,
    }
    for specialty in SPECIALTIES:
        cells[f"{specialty.key}_count"] = len(comp.by_specialty[specialty.key])
        cells[f"{specialty.key}_count_5mi"] = (
            None if within is None else len(within[specialty.key])
        )
    cells["plastic_surgeons_per_10k_adults"] = per_10k(cells["plastic_surgeon_count"], comp.adults)
    cells["dermatologists_per_10k_adults"] = per_10k(cells["dermatologist_count"], comp.adults)
    cells["plastic_surgeons_per_10k_adults_5mi"] = per_10k(
        cells["plastic_surgeon_count_5mi"], comp.adults_5mi
    )
    cells["dermatologists_per_10k_adults_5mi"] = per_10k(
        cells["dermatologist_count_5mi"], comp.adults_5mi
    )
    keys = competitors_for(procedure)
    in_zip = set().union(*(comp.by_specialty[key] for key in keys))
    near = None if within is None else set().union(*(within[key] for key in keys))
    cells["procedure_competitor_count"] = len(in_zip)
    cells["procedure_competitor_count_5mi"] = None if near is None else len(near)
    cells["procedure_competitors_per_10k_adults_5mi"] = per_10k(
        None if near is None else len(near), comp.adults_5mi
    )
    return cells


def build_rows(
    areas: list[ZipArea],
    procedures: list[str],
    competition: dict[str, ZipCompetition] | None,
    trends: dict[str, TrendsEntry],
    competition_pulled_at: datetime,
    updated_at: datetime,
    stored: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """competition is None when this run's NPPES pull failed; stored rows fill in."""
    rows: list[dict] = []
    for area in areas:
        comp = None if competition is None else competition[area.zip_code]
        base = {
            "zip_code": area.zip_code,
            "city": area.city,
            "practice_zip_code": area.practice_zip_code,
            "county": area.county,
            "distance_from_practice_miles": area.distance_from_practice_miles,
            "adult_population": area.adult_population if comp is None else comp.adults,
            "competition_source": COMPETITION_SOURCE,
            "centroid_source": CENTROID_SOURCE,
            "competition_limitations": COMPETITION_LIMITATIONS,
            "trends_dma": TRENDS_DMA_NAME,
            "trends_source": TRENDS_SOURCE,
            dataset_stamp.COLUMN: updated_at,
        }
        for procedure in procedures:
            keys = competitors_for(procedure)
            entry = trends.get(procedure) or TrendsEntry(term=term_for(procedure))
            dma = entry.dma.values if entry.dma else {}
            city_name, city_value = city_interest(entry, area.city)
            row = dict(base)
            if comp is not None:
                row.update(competition_cells(comp, procedure, competition_pulled_at))
                row["competition_status"] = STATUS_NEW
            else:
                last = (stored or {}).get((area.zip_code, procedure))
                row.update(last or {})
                row["competition_status"] = STATUS_KEPT if last else STATUS_UNAVAILABLE
            row.update(
                {
                    "procedure": procedure,
                    "procedure_competitor_specialties": ", ".join(
                        SPECIALTY_BY_KEY[key].label for key in keys
                    ),
                    "trends_search_term": entry.term,
                    "trends_dma_interest_12mo_avg": rounded(dma.get("latest_avg")),
                    "trends_dma_interest_prior_12mo_avg": rounded(dma.get("prior_avg")),
                    "trends_dma_interest_change_vs_prior_12mo": rounded(
                        dma.get("change"), "0.0001"
                    ),
                    "trends_status": entry.dma_status,
                    "trends_pulled_at": parse_iso(entry.dma.pulled_at) if entry.dma else None,
                    "trends_city_name": city_name,
                    "trends_city_interest_5yr": city_value,
                    "trends_city_status": entry.city_status,
                    "trends_city_pulled_at": (
                        parse_iso(entry.city.pulled_at) if entry.city else None
                    ),
                }
            )
            rows.append({name: row.get(name) for name in COLUMN_NAMES})
    return rows


def csv_cell(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return load_california.csv_value(value)


def write_csv(rows: list[dict], path: Path = OUTPUT_CSV) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(COLUMN_NAMES)
            for row in rows:
                writer.writerow([csv_cell(row[name]) for name in COLUMN_NAMES])
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def schema_sql() -> str:
    lines = [
        f"    {name} {sql_type}{' NOT NULL' if name in NOT_NULL_COLUMNS else ''}"
        for name, sql_type in COLUMNS
    ]
    return (
        f"CREATE TABLE IF NOT EXISTS {TABLE} (\n"
        + ",\n".join(lines)
        + ",\n    PRIMARY KEY (zip_code, procedure)\n);\n"
    )


def replace_table(conn, rows: list[dict]) -> int:
    """Replace every row in public.competition_trends. No other table is touched."""
    names = ", ".join(COLUMN_NAMES)
    placeholders = ", ".join(["%s"] * len(COLUMN_NAMES))
    insert_sql = f"INSERT INTO {TABLE} ({names}) VALUES ({placeholders})"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(schema_sql())
            for name, sql_type in COLUMNS:
                cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {name} {sql_type}")
            cur.execute(f"DELETE FROM {TABLE}")
            cur.executemany(insert_sql, [tuple(row[n] for n in COLUMN_NAMES) for row in rows])
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            count = cur.fetchone()[0]
    if count != len(rows):
        raise SystemExit(f"{TABLE} has {count} rows after loading {len(rows)}.")
    return count


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--join", type=Path, help="zip_census_redfin_join.csv")
    parser.add_argument("--procedures", type=Path, help="procedure_surgeon_fees.csv")
    parser.add_argument("--cache", type=Path, default=CACHE_JSON)
    parser.add_argument("--output", type=Path, default=OUTPUT_CSV)
    parser.add_argument(
        "--skip-trends",
        action="store_true",
        help="Do not ask Google. Keep the last good Trends values.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    areas = read_zip_areas(args.join)
    procedures = read_procedures(args.procedures)
    print(f"{len(areas)} ZIPs x {len(procedures)} procedures.")

    competition = pull_competition(areas)
    competition_pulled_at = dataset_stamp.stamp()
    stored = stored_competition() if competition is None else {}

    cache = load_cache(args.cache) or cache_from_table()
    trends = refresh_trends(
        procedures,
        cache,
        None if args.skip_trends else TrendsSource,
        dataset_stamp.stamp(),
    )
    save_cache(args.cache, trends)
    for procedure in procedures:
        entry = trends[procedure]
        print(f"Trends {procedure!r}: weekly {entry.dma_status}; cities {entry.city_status}.")

    updated_at = dataset_stamp.stamp()
    rows = build_rows(
        areas, procedures, competition, trends, competition_pulled_at, updated_at, stored
    )
    if competition is None:
        kept = sum(row["competition_status"] == STATUS_KEPT for row in rows)
        print(f"Competition: {kept} of {len(rows)} rows kept their last good values.")
    write_csv(rows, args.output)
    print(f"Wrote {len(rows)} rows to {args.output.name}.")
    if not database_url():
        print(f"{load_california.DATABASE_URL_ENV} is not set. Left {TABLE} unchanged.")
        return 0
    with load_california.connect() as conn:
        count = replace_table(conn, rows)
    print(f"Replaced {TABLE} with {count} rows. No other table was modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
