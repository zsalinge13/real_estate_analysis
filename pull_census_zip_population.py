"""Load ACS 5-year income, age, and sex for Los Angeles County home-value ZIPs.

The ZIP list is the postal codes in the home-value extract that also pass the
price script's Los Angeles County filter: Census ZCTA-to-county relationship
file, FIPS 06037, majority land area. ZCTAs are stored in the zip column.
This script does not read office, dermatology, or NPI locations.

A run fetches and inserts only the latest published ACS 5-year vintage.
That vintage is inserted only when it is not already stored. Older years
stay in public.census_zip_population. The CSV is still written when
DATABASE_URL is unset.

Los Angeles County ACS data queries always include California (in=state:06).
A query sent without a state is retried with California when Census reports
ambiguous geography or HTTP 500 ("error while running your query").

When CENSUS_API_KEY is set, it is sent on the first request and is never printed.
A dropped connection is retried. The request URL is not printed.
"""

from __future__ import annotations

import csv
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

import load_california

DATABASE_URL_ENV = "DATABASE_URL"
CENSUS_API_KEY_ENV = "CENSUS_API_KEY"
TABLE_NAME = "census_zip_population"
TABLE = "public.census_zip_population"
OUTPUT_CSV_NAME = "census_zip_population.csv"
OUTPUT_CSV = Path(__file__).resolve().parent / OUTPUT_CSV_NAME
USER_AGENT = load_california.USER_AGENT
BATCH_SIZE = 20
VARIABLE_CHUNK = 20
CONNECT_ATTEMPTS = 4
GEO_COLUMN = "zip code tabulation area"
CALIFORNIA_STATE = "06"
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
}
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
SEX_TOTALS = {
    "male_population": ("B01001_002E", "Estimate!!Total:!!Male:"),
    "female_population": ("B01001_026E", "Estimate!!Total:!!Female:"),
}
MEASURES = {
    "median_household_income": "B19013_001E",
    "per_capita_income": "B19301_001E",
    "median_age": "B01002_001E",
}


def female_code(male_code: str) -> str:
    number = int(male_code.split("_")[1][:3])
    return f"B01001_{number + 24:03d}E"


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


def estimate_variables() -> tuple[str, ...]:
    variables = [
        MEASURES["median_household_income"],
        MEASURES["per_capita_income"],
        MEASURES["median_age"],
        SEX_TOTALS["male_population"][0],
        SEX_TOTALS["female_population"][0],
    ]
    for codes in band_codes().values():
        variables.extend(codes)
    if len(variables) > 50:
        raise SystemExit("The ACS request has more than 50 variables.")
    return tuple(variables)


def select_zips(home_value_zips: set[str], county_zips: set[str]) -> set[str]:
    """ZIPs that are both in the home-value output and majority Los Angeles County."""
    return {zip_code for zip_code in home_value_zips if zip_code in county_zips}


def home_value_postal_codes() -> set[str]:
    path = load_california.OUTPUT_CSV
    if not path.exists():
        raise SystemExit(
            f"{path.name} is missing, so the Los Angeles County ZIP list "
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


def los_angeles_home_value_zips() -> set[str]:
    home_value = home_value_postal_codes()
    county = load_california.los_angeles_postal_codes()
    kept = select_zips(home_value, county)
    print(
        f"Home-value file ZIPs: {len(home_value)}. "
        f"Majority Los Angeles County ZIPs: {len(county)}. "
        f"Kept: {len(kept)}.",
        flush=True,
    )
    if not kept:
        raise SystemExit(
            "No home-value ZIP passed the Los Angeles County majority-land filter."
        )
    return kept


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
    state: str = CALIFORNIA_STATE,
) -> str:
    fields = {
        "get": ",".join(variables),
        "for": GEO_COLUMN + ":" + ",".join(zips),
    }
    # These ZIPs are Los Angeles County. Census resolves a ZCTA list only when
    # the query names California; omitting the state often returns HTTP 500.
    if state:
        fields["in"] = f"state:{state}"
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


def query_server_error(message: str) -> bool:
    """True when Census failed the query instead of naming a geography problem."""
    lowered = message.lower()
    return "http 500" in lowered or "error while running your query" in lowered


def geography_problem(message: str) -> str | None:
    lowered = message.lower()
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


def request_bytes(url: str) -> tuple[int, dict[str, str], bytes]:
    """GET a Census URL. Retry a dropped connection. Do not retry an HTTP status.

    The URL is not printed: it can contain CENSUS_API_KEY.
    """
    error: urllib.error.URLError | None = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            return open_census(url)
        except urllib.error.URLError as exc:
            # HTTPError is a URLError, and open_census already turned it into
            # a status. This branch is a dropped connection or a timeout.
            error = exc
            if attempt + 1 == CONNECT_ATTEMPTS:
                break
            print("The Census request could not connect. Retrying.", flush=True)
            time.sleep(2 * (attempt + 1))
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


def group_labels(year: int) -> dict[str, str]:
    url = f"https://api.census.gov/data/{year}/acs/acs5/groups/B01001.json"
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
    text = str(raw).strip()
    if text in {"", "null", "None", "-", "N"}:
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
    )


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
    state: str = CALIFORNIA_STATE,
) -> list[dict[str, str]]:
    """Download one ACS table. Split a query Census rejects, and keep the API text."""
    if not zips or not variables:
        return []
    url = census_data_url(year, variables, zips, state=state)
    try:
        body = fetch_with_optional_key(url)
    except SystemExit as exc:
        if key_failure(exc):
            raise
        return recover_query(year, zips, variables, state, exit_text(exc))
    try:
        return parse_table(body)
    except SystemExit:
        return recover_query(year, zips, variables, state, census_message(200, body))


def recover_query(
    year: int,
    zips: list[str],
    variables: tuple[str, ...],
    state: str,
    message: str,
) -> list[dict[str, str]]:
    problem = geography_problem(message)
    if not state and (problem == "ambiguous" or query_server_error(message)):
        return fetch_query(year, zips, variables, state=CALIFORNIA_STATE)
    if problem == "unknown" and len(zips) == 1:
        return []
    if len(variables) > 1:
        middle = len(variables) // 2
        left = fetch_query(year, zips, variables[:middle], state)
        time.sleep(0.05)
        right = fetch_query(year, zips, variables[middle:], state)
        return merge_records(left, right)
    if len(zips) > 1:
        middle = len(zips) // 2
        left = fetch_query(year, zips[:middle], variables, state)
        time.sleep(0.05)
        right = fetch_query(year, zips[middle:], variables, state)
        return left + right
    raise SystemExit(message)


def fetch_year(year: int, zips: list[str], variables: tuple[str, ...]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for group in variable_chunks(variables):
        for batch in chunks(zips, BATCH_SIZE):
            records.extend(fetch_query(year, list(batch), group, state=CALIFORNIA_STATE))
            time.sleep(0.1)
    return merge_records(records)


def rows_for_vintage(year: int, zips: list[str]) -> list[tuple]:
    variables = estimate_variables()
    records = fetch_year(year, zips, variables)
    wanted = set(zips)
    rows: list[tuple] = []
    seen: set[str] = set()
    for record in records:
        zip_code = (record.get("zip code tabulation area") or "").strip().zfill(5)
        if zip_code not in wanted or zip_code in seen:
            continue
        seen.add(zip_code)
        estimates = {
            code: parse_estimate(record.get(code))
            for code in variables
        }
        rows.append(row_from_estimates(year, zip_code, estimates))
    print(
        f"ACS {year}: {len(rows)} of {len(zips)} Los Angeles County home-value ZIPs.",
        flush=True,
    )
    if len(rows) < max(1, len(zips) // 2):
        raise SystemExit(
            f"ACS {year} returned too few Los Angeles County home-value ZIPs."
        )
    rows.sort(key=lambda row: row[1])
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
    PRIMARY KEY (zip, acs_year)
);
"""


def stored_years(cur) -> set[int]:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
          AND table_type = 'BASE TABLE'
        """,
        (TABLE_NAME,),
    )
    if cur.fetchone() is None:
        return set()
    cur.execute(f"SELECT DISTINCT acs_year FROM {TABLE}")
    return {int(year) for (year,) in cur.fetchall()}


def insert_new_years(conn, rows: list[tuple]) -> tuple[int, int]:
    """Insert vintages that are not already stored. Do not delete older years."""
    column_list = ", ".join(COLUMNS)
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    sql = (
        f"INSERT INTO {TABLE} ({column_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (zip, acs_year) DO NOTHING"
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(schema_sql())
            already = stored_years(cur)
            pending_years = vintages_to_insert(
                sorted({int(row[0]) for row in rows}), already
            )
            pending = [row for row in rows if int(row[0]) in pending_years]
            if pending:
                cur.executemany(sql, pending)
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            total = cur.fetchone()[0]
    return len(pending), total


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
    try:
        zips = sorted(los_angeles_home_value_zips())
        vintage = latest_vintage(datetime.now(timezone.utc).year)
        print(f"ACS 5-year vintage: {vintage}.", flush=True)
        verify_labels(vintage)
        rows = rows_for_vintage(vintage, zips)
        rows.sort(key=lambda row: (row[0], row[1]))
    except SystemExit as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    write_csv(rows)
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV_NAME}.")
    if not database_url():
        print(f"{DATABASE_URL_ENV} is not set. Left {TABLE} unchanged.")
        return 0
    try:
        with connect() as conn:
            inserted, total = insert_new_years(conn, rows)
    except SystemExit as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    print(
        f"Inserted {inserted} new rows into {TABLE}. "
        f"The table has {total} rows. Older vintages were kept."
    )
    return notify_refresh_review(TABLE_NAME)


if __name__ == "__main__":
    sys.exit(main())
