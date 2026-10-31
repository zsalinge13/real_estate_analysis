"""Load ACS 5-year demographics, income, housing, and occupation for practice-area home-value ZIPs.

The practice area is in practice.py: ZIPs whose majority county by land
area is Los Angeles (06037) or Orange (06059) and that are within 50 miles
of the practice ZIP. The Census download requests the California ZCTAs
that are in that area and in the home-value extract. California ZCTAs come
from the ZCTA-to-county relationship file (any area in a county whose FIPS
starts with 06). The data query lists those ZCTAs and does not send in=state.
ZCTAs are not nested in a state, and in=state:06 is an unsupported hierarchy.
Census reports that as "unknown/unsupported geography hierarchy" (sometimes
spelled "heirarchy"). That is not a missing ZIP.

After each batch is downloaded, the rows go into a dataframe. The dataframe
keeps California ZCTAs, then the practice-area ZIP codes that are in the
home-value extract. ZCTAs are stored in the zip column. This script does not read
office, dermatology, or NPI locations.

A run fetches only the latest published ACS 5-year vintage. Income, age,
sex, age bands, race and ethnicity (B03002), education (B15003), household
income brackets (B19001), mothers and births (B23003 and B13016), tenure
(B25003 and B25007), and occupation (C24010) are separate requests, about
10 ZIP codes at a time. Sex by age reuses the B01001 age-band codes.
The script waits a couple of seconds between calls. A failure retries that
piece. Each finished batch is saved before the next batch starts. A closed
connection, a dropped connection, or a read timeout is retried more than
once, and each retry waits longer than the one before it. The read timeout
is 45 minutes. Older years stay in public.census_zip_population. A ZIP
already stored for that year is not inserted again. Each set of added
columns in BACKFILL_SETS is filled on it only when every column in that set
is blank. The CSV is still written when DATABASE_URL is unset.

When CENSUS_API_KEY is set, it is sent on the first request and is never printed.
The request URL is not printed.

A newly inserted ZIP records dataset_updated_at. Every new row in the run
gets that same timestamptz. A ZIP already stored for that year is not
rewritten, so its timestamp stays as it was, unless a blank set of added
columns is filled in. That row gets the run's timestamp.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

import dataset_stamp
import load_california
import practice

DATABASE_URL_ENV = "DATABASE_URL"
CENSUS_API_KEY_ENV = "CENSUS_API_KEY"
TABLE_NAME = "census_zip_population"
TABLE = "public.census_zip_population"
OUTPUT_CSV_NAME = "census_zip_population.csv"
OUTPUT_CSV = Path(__file__).resolve().parent / OUTPUT_CSV_NAME
USER_AGENT = load_california.USER_AGENT
# One ACS call asks for about 10 California ZCTAs.
BATCH_SIZE = 10
# Wait a couple of seconds between Census calls.
CALL_PAUSE_SECONDS = 2
VARIABLE_CHUNK = 20
# A closed connection is retried more than the previous four tries.
CONNECT_ATTEMPTS = 8
# Each later retry waits longer. The first wait is longer than the old 2 seconds.
RETRY_PAUSE_SECONDS = 10
GEO_COLUMN = "zip code tabulation area"
KEY_REQUIRED = (
    "The Census American Community Survey API requires a key. "
    "Set the CENSUS_API_KEY secret. The repository does not contain a key."
)
REJECTED_KEY = "The Census API rejected CENSUS_API_KEY."
COLUMNS = (
    "acs_year",
    "zip",
    "median_household_income",
    "per_capita_income",
    "median_age",
    "male_population",
    "female_population",
    "adult_population",
    "population_18_34",
    "population_35_44",
    "population_45_54",
    "population_55_64",
    "population_65_plus",
)
# Male B01001 age bands, then the matching female row 24 codes later.
MALE_BANDS = {
    "population_18_34": (
        ("B01001_007E", "18 and 19 years"),
        ("B01001_008E", "20 years"),
        ("B01001_009E", "21 years"),
        ("B01001_010E", "22 to 24 years"),
        ("B01001_011E", "25 to 29 years"),
        ("B01001_012E", "30 to 34 years"),
    ),
    "population_35_44": (
        ("B01001_013E", "35 to 39 years"),
        ("B01001_014E", "40 to 44 years"),
    ),
    "population_45_54": (
        ("B01001_015E", "45 to 49 years"),
        ("B01001_016E", "50 to 54 years"),
    ),
    "population_55_64": (
        ("B01001_017E", "55 to 59 years"),
        ("B01001_018E", "60 and 61 years"),
        ("B01001_019E", "62 to 64 years"),
    ),
    "population_65_plus": (
        ("B01001_020E", "65 and 66 years"),
        ("B01001_021E", "67 to 69 years"),
        ("B01001_022E", "70 to 74 years"),
        ("B01001_023E", "75 to 79 years"),
        ("B01001_024E", "80 to 84 years"),
        ("B01001_025E", "85 years and over"),
    ),
}

def female_code(male_code: str) -> str:
    number = int(male_code.split("_")[1][:3])
    return f"B01001_{number + 24:03d}E"



# B03002. Hispanic or Latino of any race is one group. The rest are
# non-Hispanic single races. American Indian and Alaska Native, Native
# Hawaiian and Other Pacific Islander, some other race, and two or more races
# are each small in practice-area ZIPs, so they share one group.
RACE_ETHNICITY_GROUPS = {
    "hispanic_or_latino_population": (("B03002_012E", "Hispanic or Latino:"),),
    "non_hispanic_white_population": (("B03002_003E", "Not Hispanic or Latino:!!White alone"),),
    "non_hispanic_black_population": (
        ("B03002_004E", "Not Hispanic or Latino:!!Black or African American alone"),
    ),
    "non_hispanic_asian_population": (("B03002_006E", "Not Hispanic or Latino:!!Asian alone"),),
    "non_hispanic_other_or_multiracial_population": (
        ("B03002_005E", "Not Hispanic or Latino:!!American Indian and Alaska Native alone"),
        (
            "B03002_007E",
            "Not Hispanic or Latino:!!Native Hawaiian and Other Pacific Islander alone",
        ),
        ("B03002_008E", "Not Hispanic or Latino:!!Some other race alone"),
        ("B03002_009E", "Not Hispanic or Latino:!!Two or more races:"),
    ),
}
# B15003 counts people 25 and older by highest level finished.
EDUCATION_GROUPS = {
    "education_less_than_high_school": (
        ("B15003_002E", "No schooling completed"),
        ("B15003_003E", "Nursery school"),
        ("B15003_004E", "Kindergarten"),
        ("B15003_005E", "1st grade"),
        ("B15003_006E", "2nd grade"),
        ("B15003_007E", "3rd grade"),
        ("B15003_008E", "4th grade"),
        ("B15003_009E", "5th grade"),
        ("B15003_010E", "6th grade"),
        ("B15003_011E", "7th grade"),
        ("B15003_012E", "8th grade"),
        ("B15003_013E", "9th grade"),
        ("B15003_014E", "10th grade"),
        ("B15003_015E", "11th grade"),
        ("B15003_016E", "12th grade, no diploma"),
    ),
    "education_high_school": (
        ("B15003_017E", "Regular high school diploma"),
        ("B15003_018E", "GED or alternative credential"),
    ),
    "education_some_college_or_associate": (
        ("B15003_019E", "Some college, less than 1 year"),
        ("B15003_020E", "Some college, 1 or more years, no degree"),
        ("B15003_021E", "Associate's degree"),
    ),
    "education_bachelors": (("B15003_022E", "Bachelor's degree"),),
    "education_graduate_or_professional": (
        ("B15003_023E", "Master's degree"),
        ("B15003_024E", "Professional school degree"),
        ("B15003_025E", "Doctorate degree"),
    ),
}
# Male and female adults in each persona band, summed from the B01001 codes
# the age_bands request already downloads.
SEX_BY_AGE_GROUPS = {
    f"{sex.lower()}_{column}": tuple(
        (code if sex == "Male" else female_code(code), f"{sex}:!!{phrase}")
        for code, phrase in pairs
    )
    for column, pairs in MALE_BANDS.items()
    for sex in ("Male", "Female")
}
# B19001 household income brackets. Each bracket's lower bound is in
# HOUSEHOLD_INCOME_BRACKETS, and its upper bound is the next lower bound.
HOUSEHOLD_INCOME_BRACKETS = (
    ("households_income_under_10000", "B19001_002E", "Less than $10,000", 0),
    ("households_income_10000_14999", "B19001_003E", "$10,000 to $14,999", 10000),
    ("households_income_15000_19999", "B19001_004E", "$15,000 to $19,999", 15000),
    ("households_income_20000_24999", "B19001_005E", "$20,000 to $24,999", 20000),
    ("households_income_25000_29999", "B19001_006E", "$25,000 to $29,999", 25000),
    ("households_income_30000_34999", "B19001_007E", "$30,000 to $34,999", 30000),
    ("households_income_35000_39999", "B19001_008E", "$35,000 to $39,999", 35000),
    ("households_income_40000_44999", "B19001_009E", "$40,000 to $44,999", 40000),
    ("households_income_45000_49999", "B19001_010E", "$45,000 to $49,999", 45000),
    ("households_income_50000_59999", "B19001_011E", "$50,000 to $59,999", 50000),
    ("households_income_60000_74999", "B19001_012E", "$60,000 to $74,999", 60000),
    ("households_income_75000_99999", "B19001_013E", "$75,000 to $99,999", 75000),
    ("households_income_100000_124999", "B19001_014E", "$100,000 to $124,999", 100000),
    ("households_income_125000_149999", "B19001_015E", "$125,000 to $149,999", 125000),
    ("households_income_150000_199999", "B19001_016E", "$150,000 to $199,999", 150000),
    ("households_income_200000_plus", "B19001_017E", "$200,000 or more", 200000),
)
HOUSEHOLD_INCOME_GROUPS = {
    "households": (("B19001_001E", ""),),
    **{column: ((code, phrase),) for column, code, phrase, _low in HOUSEHOLD_INCOME_BRACKETS},
}
# B23003 counts women 20 to 64. Children are the householder's own children.
MOTHER_GROUPS = {
    "women_20_64": (("B23003_001E", ""),),
    "mothers_with_children_under_18": (
        ("B23003_002E", "With own children of the householder under 18 years:"),
    ),
    "mothers_with_children_under_6": (
        ("B23003_003E", "With own children of the householder under 18 years:!!Under 6 years only:"),
        (
            "B23003_010E",
            "With own children of the householder under 18 years:!!Under 6 years and 6 to 17 years:",
        ),
    ),
}
# B13016 counts women 15 to 50. Its youngest band starts at 15, so the
# 18-to-34 persona band is the closest fit as 15 to 34.
BIRTH = "Women who had a birth in the past 12 months:"
BIRTH_GROUPS = {
    "women_15_50": (("B13016_001E", ""),),
    "recent_births": (("B13016_002E", BIRTH),),
    "recent_births_15_34": (
        ("B13016_003E", f"{BIRTH}!!15 to 19 years old"),
        ("B13016_004E", f"{BIRTH}!!20 to 24 years old"),
        ("B13016_005E", f"{BIRTH}!!25 to 29 years old"),
        ("B13016_006E", f"{BIRTH}!!30 to 34 years old"),
    ),
    "recent_births_35_44": (
        ("B13016_007E", f"{BIRTH}!!35 to 39 years old"),
        ("B13016_008E", f"{BIRTH}!!40 to 44 years old"),
    ),
    "recent_births_45_50": (("B13016_009E", f"{BIRTH}!!45 to 50 years old"),),
}
TENURE_GROUPS = {
    "owner_occupied_households": (("B25003_002E", "Owner occupied"),),
    "renter_occupied_households": (("B25003_003E", "Renter occupied"),),
}


def tenure_band(tenure: str, first: int, phrases: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (f"B25007_{first + index:03d}E", f"{tenure} occupied:!!Householder {phrase}")
        for index, phrase in enumerate(phrases)
    )


# B25007 householder ages. Its youngest band is 15 to 24, so the 18-to-34
# persona band is the closest fit as 15 to 34. 55 and older adds 55 to 59,
# 60 to 64, 65 to 74, 75 to 84, and 85 and over.
TENURE_BY_AGE_GROUPS = {
    f"{tenure.lower()}_occupied_householder_{band}": tenure_band(tenure, start + offset, phrases)
    for tenure, start in (("Owner", 3), ("Renter", 13))
    for band, offset, phrases in (
        ("15_34", 0, ("15 to 24 years", "25 to 34 years")),
        ("35_44", 2, ("35 to 44 years",)),
        ("45_54", 3, ("45 to 54 years",)),
        (
            "55_plus",
            4,
            ("55 to 59 years", "60 to 64 years", "65 to 74 years", "75 to 84 years", "85 years and over"),
        ),
    )
}
# C24010 civilian employed population 16 and older, female side.
FEMALE_OCCUPATION = "Female:!!Management, business, science, and arts occupations:"
OCCUPATION_GROUPS = {
    "employed_women": (("C24010_038E", "Female:"),),
    "women_management_business_science_arts": (("C24010_039E", FEMALE_OCCUPATION),),
    "women_arts_design_entertainment_sports_media": (
        (
            "C24010_051E",
            f"{FEMALE_OCCUPATION}!!Education, legal, community service, arts, and media occupations:"
            "!!Arts, design, entertainment, sports, and media occupations",
        ),
    ),
}
# (ACS table, columns as sums of codes, request that downloads the codes).
# A request of None reuses codes another request already downloads.
COUNT_GROUP_TABLES = (
    ("B03002", RACE_ETHNICITY_GROUPS, "race_ethnicity"),
    ("B15003", EDUCATION_GROUPS, "education"),
    ("B01001", SEX_BY_AGE_GROUPS, None),
    ("B19001", HOUSEHOLD_INCOME_GROUPS, "household_income"),
    ("B23003", MOTHER_GROUPS, "mothers"),
    ("B13016", BIRTH_GROUPS, "mothers"),
    ("B25003", TENURE_GROUPS, "tenure"),
    ("B25007", TENURE_BY_AGE_GROUPS, "tenure"),
    ("C24010", OCCUPATION_GROUPS, "occupation"),
)
POPULATION_25_PLUS = "population_25_plus"
RACE_EDUCATION_COLUMNS = (
    tuple(RACE_ETHNICITY_GROUPS) + (POPULATION_25_PLUS,) + tuple(EDUCATION_GROUPS)
)
PROFILE_COLUMNS = (
    tuple(SEX_BY_AGE_GROUPS)
    + tuple(HOUSEHOLD_INCOME_GROUPS)
    + tuple(MOTHER_GROUPS)
    + tuple(BIRTH_GROUPS)
    + tuple(TENURE_GROUPS)
    + tuple(TENURE_BY_AGE_GROUPS)
    + tuple(OCCUPATION_GROUPS)
)
# Each set is filled on a stored ZIP only when every column in that set is blank.
BACKFILL_SETS = (RACE_EDUCATION_COLUMNS, PROFILE_COLUMNS)
ADDED_COLUMNS = RACE_EDUCATION_COLUMNS + PROFILE_COLUMNS
BASE_COLUMNS = COLUMNS
COLUMNS = BASE_COLUMNS + ADDED_COLUMNS
INTEGER_COLUMNS = {
    "acs_year",
    "median_household_income",
    "per_capita_income",
    "male_population",
    "female_population",
    "adult_population",
    "population_18_34",
    "population_35_44",
    "population_45_54",
    "population_55_64",
    "population_65_plus",
    *ADDED_COLUMNS,
}
SEX_TOTALS = {
    "male_population": ("B01001_002E", "Estimate!!Total:!!Male:"),
    "female_population": ("B01001_026E", "Estimate!!Total:!!Female:"),
}
MEASURES = {
    "median_household_income": "B19013_001E",
    "per_capita_income": "B19301_001E",
    "median_age": "B01002_001E",
}


def expected_band_label(sex: str, phrase: str) -> str:
    return f"Estimate!!Total:!!{sex}:!!{phrase}"


def band_codes() -> dict[str, tuple[str, ...]]:
    codes: dict[str, tuple[str, ...]] = {}
    for column, pairs in MALE_BANDS.items():
        collected: list[str] = []
        for male_code, _phrase in pairs:
            collected.append(male_code)
            collected.append(female_code(male_code))
        codes[column] = tuple(collected)
    return codes


def group_codes(groups: dict[str, tuple[tuple[str, str], ...]]) -> tuple[str, ...]:
    return tuple(code for pairs in groups.values() for code, _phrase in pairs)


def request_groups() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Requests for the counted tables, in first-listed order. Tables that share a name share a call."""
    names: dict[str, list[str]] = {}
    for _table, groups, request in COUNT_GROUP_TABLES:
        if request is not None:
            names.setdefault(request, []).extend(group_codes(groups))
    return tuple((name, tuple(codes)) for name, codes in names.items())


def field_groups() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Income, age, sex, age bands, race and ethnicity, and education are separate Census calls.

    A failure retries the piece that failed. It does not repeat the pieces
    that already succeeded.
    """
    band_variables: list[str] = []
    for codes in band_codes().values():
        band_variables.extend(codes)
    groups = (
        (
            "income",
            (
                MEASURES["median_household_income"],
                MEASURES["per_capita_income"],
            ),
        ),
        ("age", (MEASURES["median_age"],)),
        (
            "sex",
            (
                SEX_TOTALS["male_population"][0],
                SEX_TOTALS["female_population"][0],
            ),
        ),
        ("age_bands", tuple(band_variables)),
    ) + request_groups()
    for name, variables in groups:
        if not variables or len(variables) > 50:
            raise SystemExit(f"The ACS {name} request has more than 50 variables.")
    return groups


def estimate_variables() -> tuple[str, ...]:
    """Every variable the run reads. Each field group is its own request of 50 or fewer."""
    variables = [code for _name, codes in field_groups() for code in codes]
    if len(variables) != len(set(variables)):
        raise SystemExit("An ACS variable is requested twice.")
    return tuple(variables)


def select_zips(home_value_zips: set[str], county_zips) -> set[str]:
    """ZIPs that are both in the home-value output and in the practice area."""
    return {zip_code for zip_code in home_value_zips if zip_code in county_zips}


def home_value_postal_codes() -> set[str]:
    path = load_california.OUTPUT_CSV
    if not path.exists():
        raise SystemExit(
            f"{path.name} is missing, so the practice-area ZIP list "
            "cannot be read."
        )
    codes: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if "postal_code" not in (reader.fieldnames or []):
            raise SystemExit(f"{path.name} has no postal_code column.")
        for row in reader:
            code = load_california.postal_code_key(row.get("postal_code"))
            if code:
                codes.add(code)
    if not codes:
        raise SystemExit(f"{path.name} has no ZIP codes.")
    return codes


def area_home_value_zips(area: dict | None = None) -> set[str]:
    home_value = home_value_postal_codes()
    area = practice.practice_area() if area is None else area
    kept = select_zips(home_value, area)
    print(
        f"Home-value file ZIPs: {len(home_value)}. "
        f"Practice-area ZIPs within {practice.MAX_PRACTICE_DISTANCE_MILES} miles of "
        f"{practice.PRACTICE_ZIP_CODE}: {len(area)}. Kept: {len(kept)}.",
        flush=True,
    )
    if not kept:
        raise SystemExit("No home-value ZIP is in the practice area.")
    return kept


def california_zctas_from_crosswalk(text: str) -> set[str]:
    """ZCTAs with area in a California county.

    This reads the relationship file. It is not an ACS data query and does
    not send in=state.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    required = {
        "GEOID_ZCTA5_20",
        "GEOID_COUNTY_20",
        "AREALAND_PART",
        "AREAWATER_PART",
    }
    missing = required.difference(reader.fieldnames or [])
    if missing:
        raise SystemExit(f"Census crosswalk is missing columns: {sorted(missing)}")
    found: set[str] = set()
    for row in reader:
        zcta = (row.get("GEOID_ZCTA5_20") or "").strip()
        county = (row.get("GEOID_COUNTY_20") or "").strip()
        if len(zcta) != 5 or not zcta.isdigit():
            continue
        if not (len(county) == 5 and county.isdigit() and county.startswith("06")):
            continue
        try:
            land = int(row.get("AREALAND_PART") or 0)
            water = int(row.get("AREAWATER_PART") or 0)
        except ValueError:
            continue
        if land + water <= 0:
            continue
        found.add(zcta)
    if not found:
        raise SystemExit("Census crosswalk did not identify any California ZIPs.")
    return found


def california_zctas() -> list[str]:
    """California ZCTAs to request from the ACS data API."""
    request = urllib.request.Request(
        load_california.CROSSWALK_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/plain,*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Census ZCTA-county crosswalk returned HTTP {exc.code}."
        ) from exc
    return sorted(california_zctas_from_crosswalk(text))


def normalize_zip(raw: object) -> str:
    text = str(raw or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit():
        return ""
    return text.zfill(5)


def records_to_frame(records: list[dict[str, str]]) -> pd.DataFrame:
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        return frame
    if GEO_COLUMN not in frame.columns:
        raise SystemExit("The Census API table has no ZCTA column.")
    return frame


def filter_downloaded_zips(
    frame: pd.DataFrame,
    california_zips: set[str],
    area_zips: set[str],
) -> pd.DataFrame:
    """Keep California ZCTAs, then the practice-area home-value set.

    The Census response is already in a dataframe. This does not change the
    request that downloaded it.
    """
    if frame.empty:
        return frame
    if GEO_COLUMN not in frame.columns:
        raise SystemExit("The Census API table has no ZCTA column.")
    normalized = frame[GEO_COLUMN].map(normalize_zip)
    california = frame.loc[normalized.isin(california_zips)].copy()
    if california.empty:
        return california
    normalized = california[GEO_COLUMN].map(normalize_zip)
    return california.loc[normalized.isin(area_zips)].copy()


def quote_census(value: str, safe: str = "", encoding: str | None = None, errors: str | None = None) -> str:
    return urllib.parse.quote(
        value,
        safe=safe + ",:",
        encoding=encoding or "utf-8",
        errors=errors or "strict",
    )


def census_data_url(
    year: int,
    variables: tuple[str, ...],
    zips: list[str],
    key: str = "",
) -> str:
    """ACS ZCTA data query. Never sends in=state.

    California is chosen by the ZCTA list and the dataframe filter. A state
    qualifier on this get query is an unsupported geography hierarchy.
    """
    fields = {
        "get": ",".join(variables),
        "for": GEO_COLUMN + ":" + ",".join(zips),
    }
    query = urllib.parse.urlencode(fields, quote_via=quote_census)
    url = f"https://api.census.gov/data/{year}/acs/acs5?{query}"
    if key:
        url += "&key=" + urllib.parse.quote(key, safe="")
    return url


def census_message(status: int, body: bytes) -> str:
    """Short API error with secrets and links removed."""
    text = body[:700].decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"(?i)\bkey=[^&\s]+", "key=[REDACTED]", text)
    text = re.sub(r"(?i)https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 200:
        text = text[:200].rstrip() + "..."
    if text:
        return f"The Census API returned HTTP {status}: {text}"
    return f"The Census API returned HTTP {status}."


def geography_problem(message: str) -> str | None:
    lowered = message.lower()
    # Check this before "unknown geography". The hierarchy error contains
    # that phrase, and Census sometimes spells it "heirarchy".
    if "geography hierarchy" in lowered or "geography heirarchy" in lowered:
        return "hierarchy"
    if "ambiguous geography" in lowered or "qualify it with state" in lowered:
        return "ambiguous"
    if (
        "unknown geography" in lowered
        or "unknown/unsupported geography" in lowered
        or "not a valid geography" in lowered
    ):
        return "unknown"
    return None


def variable_chunks(variables: tuple[str, ...], size: int = VARIABLE_CHUNK) -> list[tuple[str, ...]]:
    if size < 1:
        raise SystemExit("The ACS variable chunk size is invalid.")
    return [tuple(variables[index : index + size]) for index in range(0, len(variables), size)]


def merge_records(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for records in groups:
        for record in records:
            zip_code = (record.get(GEO_COLUMN) or "").strip()
            if not zip_code:
                continue
            current = merged.get(zip_code)
            if current is None:
                current = {}
                merged[zip_code] = current
                order.append(zip_code)
            current.update(record)
    return [merged[zip_code] for zip_code in order]


def looks_like_key_error(status: int, headers, body: bytes) -> bool:
    if headers.get("X-DataWebAPI-KeyError"):
        return True
    location = headers.get("Location") or ""
    if "missing_key" in location or "invalid_key" in location:
        return True
    if status in {401, 403}:
        return True
    sample = body[:800].lower()
    return (
        b"missing key" in sample
        or b"invalid key" in sample
        or b"valid api key" in sample
        or b"must be a valid" in sample
    )


def open_census(url: str) -> tuple[int, dict[str, str], bytes]:
    """GET one Census URL once. An HTTP status is returned, not raised."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,*/*"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=2700)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
            headers = {key: value for key, value in exc.headers.items()}
        finally:
            exc.close()
        return exc.code, headers, body
    try:
        headers = {key: value for key, value in response.headers.items()}
        status = response.status
        body = response.read()
    finally:
        response.close()
    return status, headers, body


def retry_pause_seconds(attempt: int) -> int:
    """Seconds to wait after failed try `attempt` (0 for the first failure).

    Each later retry waits longer than the previous one.
    """
    return RETRY_PAUSE_SECONDS * (attempt + 1)


def retry_notice(exc: BaseException) -> str:
    """Retry line for a closed connection, a dropped connection, or a read timeout."""
    reason = getattr(exc, "reason", None)
    text = str(exc).lower()
    timed_out = (
        isinstance(exc, TimeoutError)
        or isinstance(reason, TimeoutError)
        or "timed out" in text
    )
    if timed_out:
        return "The Census request timed out. Retrying."
    closed = (
        isinstance(exc, ConnectionError)
        or isinstance(reason, ConnectionError)
        or "closed connection" in text
        or "remote end closed" in text
    )
    if closed:
        return "The Census connection closed. Retrying."
    return "The Census request could not connect. Retrying."


def request_bytes(url: str) -> tuple[int, dict[str, str], bytes]:
    """GET a Census URL. Retry a closed connection, a drop, or a read timeout.

    Do not retry an HTTP status. Each retry waits longer than the one before it.
    The URL is not printed: it can contain CENSUS_API_KEY.
    """
    error: Exception | None = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return open_census(url)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # HTTPError is a URLError, and open_census already turned it into
            # a status. urlopen wraps a connect failure as URLError. A closed
            # connection or a timeout while reading the body is raised by
            # response.read().
            error = exc
            if attempt + 1 == CONNECT_ATTEMPTS:
                break
            print(retry_notice(exc), flush=True)
            time.sleep(retry_pause_seconds(attempt))
    if error is None:
        raise urllib.error.URLError("The Census request did not run.")
    raise error


class KeyRequired(Exception):
    pass


def fetch_bytes(url: str) -> bytes:
    """GET a Census URL. A missing or rejected key raises KeyRequired."""
    status, headers, body = request_bytes(url)
    if status in {301, 302, 303, 307, 308} or looks_like_key_error(status, headers, body):
        raise KeyRequired()
    if status != 200:
        raise SystemExit(census_message(status, body))
    return body


def with_census_key(url: str, key: str) -> str:
    """Append a Census key without printing it. Leave a URL that already has one."""
    if not key or "key=" in url.split("#", 1)[0]:
        return url
    joiner = "&" if "?" in url else "?"
    return url + joiner + "key=" + urllib.parse.quote(key, safe="")


def fetch_with_optional_key(url: str) -> bytes:
    """GET a Census URL. Send CENSUS_API_KEY on the first request when it is set."""
    key = os.environ.get(CENSUS_API_KEY_ENV, "").strip()
    try:
        return fetch_bytes(with_census_key(url, key))
    except KeyRequired:
        if not key:
            raise SystemExit(KEY_REQUIRED)
        raise SystemExit(REJECTED_KEY) from None


def public_error(exc: BaseException) -> str:
    text = str(exc)
    text = re.sub(r"(?i)\bkey=[^&\s]+", "key=[REDACTED]", text)
    text = re.sub(r"(?i)https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    lowered = text.lower()
    if text in {KEY_REQUIRED, REJECTED_KEY}:
        return text
    if (
        "://" in text
        or "postgres" in lowered
        or "key=" in lowered
        or "password" in lowered
        or CENSUS_API_KEY_ENV in text
    ):
        return "The Census ZIP load failed."
    if text:
        return text
    return "The Census ZIP load failed."


def vintage_published(year: int) -> bool:
    url = (
        f"https://api.census.gov/data/{year}/acs/acs5/variables/B19013_001E.json"
    )
    try:
        body = fetch_with_optional_key(url)
    except SystemExit as exc:
        # A rejected key is HTTP 200 HTML (invalid_key.html). That is not an
        # unpublished vintage, and it must not be reported as a missing year.
        if key_failure(exc):
            raise
        return False
    if not body.lstrip().startswith(b"{"):
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    return payload.get("name") == "B19013_001E"


def latest_vintage(this_year: int) -> int:
    """The newest published ACS 5-year vintage."""
    year = this_year
    while year >= 2009:
        if vintage_published(year):
            return year
        year -= 1
    raise SystemExit("The Census API did not list an ACS 5-year vintage.")


def group_labels(year: int, group: str = "B01001") -> dict[str, str]:
    url = f"https://api.census.gov/data/{year}/acs/acs5/groups/{group}.json"
    payload = json.loads(fetch_with_optional_key(url))
    variables = payload.get("variables") or {}
    labels = {}
    for name, meta in variables.items():
        if isinstance(meta, dict) and meta.get("label"):
            labels[name] = meta["label"]
    return labels


def variable_label(year: int, code: str) -> str:
    url = f"https://api.census.gov/data/{year}/acs/acs5/variables/{code}.json"
    payload = json.loads(fetch_with_optional_key(url))
    label = payload.get("label") or ""
    if payload.get("name") != code or not label:
        raise SystemExit(f"ACS {year} did not describe {code}.")
    return label


def verify_labels(year: int) -> None:
    labels = group_labels(year)
    for column, (code, expected) in SEX_TOTALS.items():
        if labels.get(code) != expected:
            raise SystemExit(f"ACS {year} changed the {column} age-sex label.")
    for column, pairs in MALE_BANDS.items():
        for male_code, phrase in pairs:
            female = female_code(male_code)
            if labels.get(male_code) != expected_band_label("Male", phrase):
                raise SystemExit(f"ACS {year} changed the male {column} bands.")
            if labels.get(female) != expected_band_label("Female", phrase):
                raise SystemExit(f"ACS {year} changed the female {column} bands.")
    for table, groups, _request in COUNT_GROUP_TABLES:
        table_labels = group_labels(year, table)
        for column, pairs in groups.items():
            for code, phrase in pairs:
                expected = f"Estimate!!Total:!!{phrase}" if phrase else "Estimate!!Total:"
                if table_labels.get(code) != expected:
                    raise SystemExit(f"ACS {year} changed the {table} {column} label.")
    income = variable_label(year, MEASURES["median_household_income"]).lower()
    per_capita = variable_label(year, MEASURES["per_capita_income"]).lower()
    age = variable_label(year, MEASURES["median_age"]).lower()
    if "median household income" not in income:
        raise SystemExit(f"ACS {year} median household income variable changed.")
    if "per capita income" not in per_capita:
        raise SystemExit(f"ACS {year} per capita income variable changed.")
    if "median age" not in age:
        raise SystemExit(f"ACS {year} median age variable changed.")


def parse_estimate(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        missing = bool(pd.isna(raw))
    except TypeError:
        missing = False
    if missing:
        return None
    text = str(raw).strip()
    if text.lower() in {"", "null", "none", "-", "n", "nan"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if number < 0:
        return None
    return number


def as_int(number: Decimal | None, label: str) -> int | None:
    if number is None:
        return None
    if number != number.to_integral_value():
        raise SystemExit(f"ACS returned a fractional {label}.")
    return int(number)


def sum_counts(values: list[int | None]) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(values)


def row_from_estimates(year: int, zip_code: str, estimates: dict[str, Decimal | None]) -> tuple:
    bands = band_codes()
    band_values = {
        column: sum_counts(
            [as_int(estimates.get(code), column) for code in codes]
        )
        for column, codes in bands.items()
    }
    adult = sum_counts(list(band_values.values()))
    counted = {
        column: sum_counts([as_int(estimates.get(code), column) for code, _phrase in pairs])
        for _table, groups, _request in COUNT_GROUP_TABLES
        for column, pairs in groups.items()
    }
    counted[POPULATION_25_PLUS] = sum_counts(
        [counted[column] for column in EDUCATION_GROUPS]
    )
    income = as_int(estimates.get(MEASURES["median_household_income"]), "income")
    per_capita = as_int(estimates.get(MEASURES["per_capita_income"]), "per capita income")
    age = estimates.get(MEASURES["median_age"])
    return (
        year,
        zip_code,
        income,
        per_capita,
        age,
        as_int(estimates.get(SEX_TOTALS["male_population"][0]), "male population"),
        as_int(estimates.get(SEX_TOTALS["female_population"][0]), "female population"),
        adult,
        band_values["population_18_34"],
        band_values["population_35_44"],
        band_values["population_45_54"],
        band_values["population_55_64"],
        band_values["population_65_plus"],
    ) + tuple(counted[column] for column in ADDED_COLUMNS)


def chunks(items: list[str], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def parse_table(body: bytes) -> list[dict[str, str]]:
    if not body.lstrip().startswith(b"["):
        raise SystemExit("The Census API did not return a table.")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit("The Census API did not return a table.") from exc
    if not payload or not isinstance(payload, list) or not payload[0]:
        raise SystemExit("The Census API returned an empty table.")
    header = payload[0]
    geo_name = GEO_COLUMN
    if geo_name not in header:
        raise SystemExit("The Census API table has no ZCTA column.")
    records = []
    for raw in payload[1:]:
        if len(raw) != len(header):
            raise SystemExit("The Census API table has a short row.")
        records.append(dict(zip(header, raw)))
    return records


def exit_text(exc: SystemExit) -> str:
    if isinstance(exc.code, str) and exc.code:
        return exc.code
    return str(exc) or "The Census ZIP load failed."


def key_failure(exc: SystemExit) -> bool:
    # Use the raw message. A redacted public string would no longer name the
    # key failure, and the query would be split instead of stopped.
    message = exit_text(exc)
    return message in {KEY_REQUIRED, REJECTED_KEY}


def fetch_query(
    year: int,
    zips: list[str],
    variables: tuple[str, ...],
) -> list[dict[str, str]]:
    """Download one ACS field piece. Split a query Census rejects.

    The data URL never includes in=state. California is not applied here.
    """
    if not zips or not variables:
        return []
    url = census_data_url(year, variables, zips)
    try:
        body = fetch_with_optional_key(url)
    except SystemExit as exc:
        if key_failure(exc):
            raise
        return recover_query(year, zips, variables, exit_text(exc))
    try:
        return parse_table(body)
    except SystemExit:
        return recover_query(year, zips, variables, census_message(200, body))


def recover_query(
    year: int,
    zips: list[str],
    variables: tuple[str, ...],
    message: str,
) -> list[dict[str, str]]:
    """Retry the failed piece by splitting it. Do not add a state."""
    problem = geography_problem(message)
    if problem == "hierarchy":
        raise SystemExit(message)
    # A list of ZCTAs can be called ambiguous. Split the ZIP list. Do not
    # qualify the data query with in=state:06.
    if problem in {"unknown", "ambiguous"} and len(zips) > 1:
        middle = len(zips) // 2
        left = fetch_query(year, zips[:middle], variables)
        time.sleep(CALL_PAUSE_SECONDS)
        right = fetch_query(year, zips[middle:], variables)
        return left + right
    if problem == "unknown" and len(zips) == 1:
        return []
    if problem == "ambiguous":
        raise SystemExit(message)
    if len(variables) > 1:
        middle = len(variables) // 2
        left = fetch_query(year, zips, variables[:middle])
        time.sleep(CALL_PAUSE_SECONDS)
        right = fetch_query(year, zips, variables[middle:])
        return merge_records(left, right)
    if len(zips) > 1:
        middle = len(zips) // 2
        left = fetch_query(year, zips[:middle], variables)
        time.sleep(CALL_PAUSE_SECONDS)
        right = fetch_query(year, zips[middle:], variables)
        return left + right
    raise SystemExit(message)


def fetch_field_piece(
    year: int,
    zips: list[str],
    variables: tuple[str, ...],
) -> list[dict[str, str]]:
    """Download one field group for one ZIP batch.

    A timeout, a dropped connection, or a split of a rejected query stays
    inside this piece. Earlier field groups are not requested again.
    """
    return fetch_query(year, zips, variables)


def fetch_zip_batch(year: int, zips: list[str]) -> list[dict[str, str]]:
    """Request income, then age, then sex, then age bands."""
    records: list[dict[str, str]] = []
    groups = field_groups()
    for index, (_name, variables) in enumerate(groups):
        records.extend(fetch_field_piece(year, list(zips), variables))
        if index + 1 != len(groups):
            time.sleep(CALL_PAUSE_SECONDS)
    return merge_records(records)


def rows_from_download(
    year: int,
    records: list[dict[str, str]],
    variables: tuple[str, ...],
    california_zips: set[str],
    area_zips: set[str],
    seen: set[str],
) -> list[tuple]:
    """Turn one downloaded batch into rows, then keep the practice area."""
    frame = records_to_frame(records)
    kept = filter_downloaded_zips(frame, california_zips, area_zips)
    if kept.empty:
        return []
    rows: list[tuple] = []
    for record in kept.to_dict(orient="records"):
        zip_code = normalize_zip(record.get(GEO_COLUMN))
        if zip_code not in area_zips or zip_code in seen:
            continue
        seen.add(zip_code)
        estimates = {
            code: parse_estimate(record.get(code))
            for code in variables
        }
        rows.append(row_from_estimates(year, zip_code, estimates))
    return rows


def county_pulls(
    california_zips: list[str],
    counties: dict[str, str] | None,
) -> list[tuple[str, list[str]]]:
    """One pull per practice county, in practice.COUNTIES order, so no batch mixes counties."""
    if counties is None:
        return [("California", list(california_zips))]
    by_county: dict[str, list[str]] = {name: [] for name in practice.COUNTIES.values()}
    for zip_code in california_zips:
        county = counties.get(zip_code)
        if county not in by_county:
            raise SystemExit(f"ZIP {zip_code} has no practice county.")
        by_county[county].append(zip_code)
    return [(f"{name} County", zips) for name, zips in by_county.items() if zips]


def rows_for_vintage(
    year: int,
    california_zips: list[str],
    area_zips: set[str],
    on_batch,
    counties: dict[str, str] | None = None,
    clock=time.monotonic,
) -> list[tuple]:
    """Download each county's ZCTAs in its own batches and checkpoint after each batch.

    Every batch is BATCH_SIZE ZIPs or fewer from one county, and fetch_zip_batch
    sends each field group as its own request with the usual timeout, retry,
    and split rules.
    """
    variables = estimate_variables()
    california = set(california_zips)
    rows: list[tuple] = []
    seen: set[str] = set()
    calls = 0
    for label, county_zips in county_pulls(california_zips, counties):
        started = clock()
        county_rows = 0
        batches = list(chunks(county_zips, BATCH_SIZE))
        for index, batch in enumerate(batches, start=1):
            if calls:
                time.sleep(CALL_PAUSE_SECONDS)
            calls += 1
            batch_started = clock()
            print(
                f"ACS {year} {label}: downloading batch {index} of {len(batches)}, "
                f"{len(batch)} ZIPs ({batch[0]} to {batch[-1]}).",
                flush=True,
            )
            records = fetch_zip_batch(year, list(batch))
            new_rows = rows_from_download(
                year, records, variables, california, area_zips, seen
            )
            county_rows += len(new_rows)
            rows.extend(new_rows)
            on_batch(list(rows), new_rows)
            print(
                f"ACS {year} {label}: batch {index} took {clock() - batch_started:.1f} s.",
                flush=True,
            )
        expected = len(set(county_zips) & area_zips)
        print(
            f"ACS {year} {label}: {county_rows} of {expected} ZIPs in {len(batches)} batches, "
            f"{clock() - started:.1f} s.",
            flush=True,
        )
        if county_rows < max(1, expected // 2):
            raise SystemExit(f"ACS {year} returned too few {label} ZIPs.")
    print(
        f"ACS {year}: {len(rows)} of {len(area_zips)} practice-area home-value ZIPs.",
        flush=True,
    )
    if len(rows) < max(1, len(area_zips) // 2):
        raise SystemExit(
            f"ACS {year} returned too few practice-area home-value ZIPs."
        )
    rows.sort(key=lambda row: (row[0], row[1]))
    return rows


def vintages_to_insert(fetched_years: list[int], stored_years: set[int]) -> list[int]:
    return [year for year in fetched_years if year not in stored_years]


def write_csv(rows: list[tuple]) -> None:
    tmp = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(COLUMNS)
            for row in rows:
                writer.writerow([load_california.csv_value(value) for value in row])
        tmp.replace(OUTPUT_CSV)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def database_url() -> str:
    return os.environ.get(DATABASE_URL_ENV, "").strip()


def connect():
    import psycopg

    url = database_url()
    if not url:
        raise SystemExit(f"{DATABASE_URL_ENV} is not set.")
    return psycopg.connect(
        url,
        sslmode="require",
        connect_timeout=30,
        application_name="real_estate_analysis",
    )


def added_columns_sql() -> str:
    return "\n".join(f"    {name} integer," for name in ADDED_COLUMNS)


def schema_sql() -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    acs_year integer NOT NULL,
    zip text NOT NULL,
    median_household_income integer,
    per_capita_income integer,
    median_age numeric,
    male_population integer,
    female_population integer,
    adult_population integer,
    population_18_34 integer,
    population_35_44 integer,
    population_45_54 integer,
    population_55_64 integer,
    population_65_plus integer,
{added_columns_sql()}
    {dataset_stamp.COLUMN} {dataset_stamp.SQL_TYPE},
    PRIMARY KEY (zip, acs_year)
);
"""


def stored_pairs(cur, years: list[int]) -> set[tuple[int, str]]:
    """ZIP and year pairs already stored. The table exists when this runs."""
    if not years:
        return set()
    cur.execute(
        f"SELECT acs_year, zip FROM {TABLE} WHERE acs_year = ANY(%s)",
        (years,),
    )
    return {(int(year), str(zip_code)) for year, zip_code in cur.fetchall()}


def backfill_sql(columns: tuple[str, ...]) -> str:
    """Fill one set of added columns on a stored ZIP only when every one of them is blank."""
    assignments = ", ".join(f"{name} = %s" for name in columns)
    still_blank = " AND ".join(f"{name} IS NULL" for name in columns)
    return (
        f"UPDATE {TABLE} SET {assignments}, {dataset_stamp.COLUMN} = %s "
        f"WHERE acs_year = %s AND zip = %s AND {still_blank}"
    )


def backfill_params(rows: list[tuple], columns: tuple[str, ...], updated_at: datetime) -> list[tuple]:
    indexes = [COLUMNS.index(name) for name in columns]
    params = []
    for row in rows:
        added = tuple(row[index] for index in indexes)
        if all(value is None for value in added):
            continue
        params.append(added + (updated_at, row[0], row[1]))
    return params


def insert_new_years(
    conn, rows: list[tuple], updated_at: datetime | None = None
) -> tuple[int, int, int]:
    """Insert ZIP rows that are not already stored. Do not delete older years.

    Each set in BACKFILL_SETS is filled on a stored ZIP only when every
    column in that set is blank. Returns inserted, filled, and total rows.
    """
    if not rows:
        return 0, 0, 0
    updated_at = dataset_stamp.stamp(updated_at)
    stored = COLUMNS + (dataset_stamp.COLUMN,)
    column_list = ", ".join(stored)
    placeholders = ", ".join(["%s"] * len(stored))
    sql = (
        f"INSERT INTO {TABLE} ({column_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (zip, acs_year) DO NOTHING"
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(schema_sql())
            cur.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "
                f"{dataset_stamp.COLUMN} {dataset_stamp.SQL_TYPE}"
            )
            for name in ADDED_COLUMNS:
                cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {name} integer")
            years = sorted({int(row[0]) for row in rows})
            already = stored_pairs(cur, years)
            pending = [
                row
                for row in rows
                if (int(row[0]), str(row[1])) not in already
            ]
            _updated_at, stamped = dataset_stamp.stamp_rows(pending, updated_at)
            if stamped:
                cur.executemany(sql, stamped)
            stored_rows = [row for row in rows if (int(row[0]), str(row[1])) in already]
            filled: set[tuple[int, str]] = set()
            for columns in BACKFILL_SETS:
                for params in backfill_params(stored_rows, columns, updated_at):
                    cur.execute(backfill_sql(columns), params)
                    if (cur.rowcount or 0) > 0:
                        filled.add((params[-2], params[-1]))
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            total = cur.fetchone()[0]
    return len(stamped), len(filled), total


def checkpoint_download(holder: dict, rows_so_far: list[tuple], new_rows: list[tuple]) -> None:
    """Write finished rows before the next Census batch starts."""
    ordered = sorted(rows_so_far, key=lambda row: (row[0], row[1]))
    write_csv(ordered)
    print(
        f"Saved {len(ordered)} practice-area rows to {OUTPUT_CSV_NAME}.",
        flush=True,
    )
    if not database_url() or not new_rows:
        return
    if holder.get("conn") is None:
        holder["conn"] = connect()
    added, filled, total = insert_new_years(
        holder["conn"], new_rows, holder.get("updated_at")
    )
    holder["inserted"] = holder.get("inserted", 0) + added
    holder["filled"] = holder.get("filled", 0) + filled
    holder["total"] = total


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
    updated_at = dataset_stamp.stamp()
    holder: dict = {
        "conn": None,
        "inserted": 0,
        "filled": 0,
        "total": None,
        "updated_at": updated_at,
    }
    print(f"dataset_updated_at: {updated_at.isoformat()}")
    rows: list[tuple] | None = None
    try:
        area = practice.practice_area()
        area_zips = area_home_value_zips(area)
        counties = {zip_code: zip_area.county for zip_code, zip_area in area.items()}
        california = [zip_code for zip_code in california_zctas() if zip_code in area_zips]
        print(f"California ZCTAs to request: {len(california)}.", flush=True)
        for label, zips in county_pulls(california, counties):
            print(f"{label}: {len(zips)} ZCTAs in {-(-len(zips) // BATCH_SIZE)} batches.", flush=True)
        vintage = latest_vintage(datetime.now(timezone.utc).year)
        print(f"ACS 5-year vintage: {vintage}.", flush=True)
        verify_labels(vintage)

        def on_batch(rows_so_far: list[tuple], new_rows: list[tuple]) -> None:
            checkpoint_download(holder, rows_so_far, new_rows)

        rows = rows_for_vintage(vintage, california, area_zips, on_batch, counties)
    except SystemExit as exc:
        print(public_error(exc), file=sys.stderr)
        rows = None
    except Exception as exc:
        print(public_error(exc), file=sys.stderr)
        rows = None
    finally:
        conn = holder.get("conn")
        if conn is not None:
            conn.close()
    if rows is None:
        return 1
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV_NAME}.")
    if not database_url():
        print(f"{DATABASE_URL_ENV} is not set. Left {TABLE} unchanged.")
        return 0
    inserted = holder["inserted"]
    filled = holder["filled"]
    total = holder["total"]
    if total is None:
        try:
            with connect() as conn:
                inserted, filled, total = insert_new_years(conn, rows, updated_at)
        except SystemExit as exc:
            print(public_error(exc), file=sys.stderr)
            return 1
        except Exception as exc:
            print(public_error(exc), file=sys.stderr)
            return 1
    print(
        f"Inserted {inserted} new rows into {TABLE}. "
        f"Filled blank added columns on {filled} stored rows. "
        f"The table has {total} rows. Older vintages were kept."
    )
    return notify_refresh_review(TABLE_NAME)


if __name__ == "__main__":
    sys.exit(main())
