"""Load California rows from Zillow Research CSVs into Postgres.

The research page at https://www.zillow.com/research/data/ publishes download
links to files.zillowstatic.com. This script reads those hrefs (and the hrefs
in the page script that fills the download buttons). It does not scrape page
text for values and it does not call an unofficial API.

Each wide month-column file becomes one observation per region, month, metric,
building-type cut, and bedroom cut. A rerun upserts, so a revised month
replaces the stored value.
"""

from __future__ import annotations

import csv
import html
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

RESEARCH_PAGE = "https://www.zillow.com/research/data/"
# Href snapshot taken from that page's download catalog. Used only when the
# page itself refuses the request. Every line is a files.zillowstatic.com URL.
HREF_SNAPSHOT = Path(__file__).with_name("research_hrefs.txt")
DATABASE_URL_ENV = "DATABASE_URL"
BATCH_ROWS = 20_000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CSV_URL_RE = re.compile(
    r"https://files\.zillowstatic\.com/research/public_csvs/[A-Za-z0-9_./%-]+\.csv"
)
SCRIPT_SRC_RE = re.compile(
    r"""src=["']([^"']*pages/research/data-[^"']+\.js)["']"""
)
MONTH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BEDROOM_RE = re.compile(r"bdrmcnt_(\d+)")

# Zillow's metro files label the region type "msa".
GEO_REGION_TYPE = {
    "zip": "zip",
    "city": "city",
    "county": "county",
    "metro": "msa",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS region (
    region_id bigint PRIMARY KEY,
    region_name text NOT NULL,
    region_type text NOT NULL,
    state_code text NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_observation (
    region_id bigint NOT NULL REFERENCES region (region_id),
    metric text NOT NULL,
    building_type text NOT NULL,
    bedrooms text NOT NULL,
    month date NOT NULL,
    value numeric NOT NULL,
    pulled_at timestamptz NOT NULL,
    PRIMARY KEY (region_id, metric, building_type, bedrooms, month)
);
"""

UPSERT_REGION_SQL = """
INSERT INTO region (region_id, region_name, region_type, state_code)
VALUES (%s, %s, %s, %s)
ON CONFLICT (region_id) DO UPDATE SET
    region_name = EXCLUDED.region_name,
    region_type = EXCLUDED.region_type,
    state_code = EXCLUDED.state_code
"""

UPSERT_OBSERVATION_SQL = """
INSERT INTO metric_observation (
    region_id, metric, building_type, bedrooms, month, value, pulled_at
)
SELECT DISTINCT ON (region_id, metric, building_type, bedrooms, month)
    region_id, metric, building_type, bedrooms, month, value, pulled_at
FROM staging_observation
ORDER BY region_id, metric, building_type, bedrooms, month, pulled_at DESC
ON CONFLICT (region_id, metric, building_type, bedrooms, month)
DO UPDATE SET
    value = EXCLUDED.value,
    pulled_at = EXCLUDED.pulled_at
"""


class SeriesNotFound(Exception):
    """The published CSV URL returned HTTP 404."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


@dataclass(frozen=True)
class Series:
    url: str
    metric: str
    building_type: str
    bedrooms: str
    geography: str

    @property
    def label(self) -> str:
        return (
            f"{self.metric} | {self.building_type} | bedrooms {self.bedrooms} "
            f"| {self.geography}"
        )


def fetch_text(url: str, timeout: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/javascript,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def resolve_script_src(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http://") or src.startswith("https://"):
        return src
    return urllib.parse.urljoin(RESEARCH_PAGE, src)


def extract_csv_urls(text: str) -> list[str]:
    """Return files.zillowstatic.com CSV hrefs in first-seen order."""
    decoded = html.unescape(text).replace("\\/", "/")
    seen: set[str] = set()
    urls: list[str] = []
    for url in CSV_URL_RE.findall(decoded):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def load_href_snapshot() -> list[str]:
    if not HREF_SNAPSHOT.is_file():
        return []
    urls: list[str] = []
    for line in HREF_SNAPSHOT.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        found = extract_csv_urls(text)
        if found:
            urls.extend(found)
        elif text.startswith("https://files.zillowstatic.com/"):
            urls.append(text)
    return list(dict.fromkeys(urls))


def discover_csv_urls() -> tuple[list[str], str]:
    """Read download hrefs from the Zillow Research data page.

    The page HTML includes the currently selected download href. The rest of
    the download hrefs live in the page script that page links to
    (pages/research/data-*.js). Both are URL strings for official CSVs.

    If that page blocks the client, use the committed href snapshot taken
    from the same catalog. URLs that later 404 are skipped.
    """
    try:
        page = fetch_text(RESEARCH_PAGE, timeout=60)
    except urllib.error.HTTPError as exc:
        snapshot = load_href_snapshot()
        if snapshot and exc.code in {401, 403, 429, 503}:
            return snapshot, (
                f"{RESEARCH_PAGE} returned HTTP {exc.code}; "
                f"using href snapshot {HREF_SNAPSHOT.name}"
            )
        raise SystemExit(
            f"Could not read {RESEARCH_PAGE} (HTTP {exc.code}). "
            "This load only uses CSV links published on that page."
        ) from exc
    urls = extract_csv_urls(page)
    script_srcs = []
    for src in SCRIPT_SRC_RE.findall(html.unescape(page)):
        resolved = resolve_script_src(src)
        if resolved not in script_srcs:
            script_srcs.append(resolved)
    for src in script_srcs:
        try:
            script = fetch_text(src, timeout=60)
        except urllib.error.HTTPError as exc:
            print(f"skip page script HTTP {exc.code} {src}", file=sys.stderr)
            continue
        urls.extend(extract_csv_urls(script))
    deduped = list(dict.fromkeys(urls))
    if deduped:
        return deduped, RESEARCH_PAGE
    snapshot = load_href_snapshot()
    if snapshot:
        return snapshot, (
            f"no CSV hrefs in {RESEARCH_PAGE}; using href snapshot {HREF_SNAPSHOT.name}"
        )
    raise SystemExit(f"No files.zillowstatic.com CSV hrefs found at {RESEARCH_PAGE}.")


def _filename(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _geography(name: str) -> str | None:
    prefix = name.split("_", 1)[0]
    return {"Zip": "zip", "City": "city", "County": "county", "Metro": "metro"}.get(
        prefix
    )


def _building_type(name: str) -> str | None:
    # Check the combined cuts before the shorter tokens they contain.
    if "uc_sfrcondomfr" in name or "uc_mfr" in name:
        return None
    if "uc_sfrcondo" in name:
        return "all homes"
    if "uc_condo" in name:
        return "condo"
    if "uc_sfr" in name:
        return "single-family"
    return None


def _bedrooms(name: str) -> str | None:
    match = BEDROOM_RE.search(name)
    if match is None:
        return "all"
    count = int(match.group(1))
    if count == 5:
        return "5+"
    if 1 <= count <= 4:
        return str(count)
    return None


def _is_edge_tier(name: str) -> bool:
    # Top tier (65th-95th) and bottom tier (5th-35th) are separate series.
    # The table key has no tier column, so only the mid-tier ZHVI is loaded.
    return "tier_0.67_1.0" in name or "tier_0.0_0.33" in name


def classify(url: str) -> Series | None:
    """Map a discovered CSV href onto a series, or skip it.

    ZHVI: smoothed, seasonally adjusted, monthly files for zip, city, county,
    and metro. Home-type cuts are all homes, single-family, and condo.
    Bedroom files keep their bedroom slice. Weekly files, raw (unsmoothed)
    ZHVI, and top/bottom value tiers are not selected.

    Median list price: smoothed monthly metro files. Median sale price:
    monthly metro files (the all-homes nowcast, and raw monthly where that
    is the monthly file). Weekly columns are not one row per month.
    """
    name = _filename(url)
    geography = _geography(name)
    building_type = _building_type(name)
    bedrooms = _bedrooms(name)
    if geography is None or building_type is None or bedrooms is None:
        return None
    if "week" in name:
        return None

    if "/zhvi/" in url and name.endswith("_sm_sa_month.csv") and not _is_edge_tier(name):
        return Series(url, "zhvi", building_type, bedrooms, geography)

    if (
        "/mlp/" in url
        and geography == "metro"
        and bedrooms == "all"
        and name.endswith("_sm_month.csv")
    ):
        return Series(url, "median_list_price", building_type, "all", geography)

    sale_name = "median_sale_price" in url and "sale_to_list" not in url
    if not sale_name or "new_con_" in url or geography != "metro" or bedrooms != "all":
        return None
    if "/median_sale_price_now/" in url and name.endswith("_month.csv"):
        return Series(url, "median_sale_price", building_type, "all", geography)
    if (
        "/median_sale_price/" in url
        and name.endswith("_month.csv")
        and "_sm_" not in name
    ):
        return Series(url, "median_sale_price", building_type, "all", geography)
    return None


def select_series(urls: list[str]) -> list[Series]:
    selected: list[Series] = []
    seen: set[str] = set()
    for url in urls:
        series = classify(url)
        if series is None or series.url in seen:
            continue
        seen.add(series.url)
        selected.append(series)
    geography_order = {"zip": 0, "city": 1, "county": 2, "metro": 3}
    building_order = {"all homes": 0, "single-family": 1, "condo": 2}
    bedroom_order = {"all": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5+": 5}
    metric_order = {"zhvi": 0, "median_list_price": 1, "median_sale_price": 2}
    selected.sort(
        key=lambda series: (
            metric_order.get(series.metric, 9),
            geography_order.get(series.geography, 9),
            building_order.get(series.building_type, 9),
            bedroom_order.get(series.bedrooms, 9),
            series.url,
        )
    )
    return selected


def unpublished_notes(urls: list[str]) -> list[str]:
    """Cuts the research page does not link, so this load does not request them."""
    notes: list[str] = []

    def listed(*needles: str, rejected: tuple[str, ...] = ()) -> bool:
        for url in urls:
            if any(needle not in url for needle in needles):
                continue
            if any(needle in url for needle in rejected):
                continue
            return True
        return False

    if not listed("/mlp/", "uc_condo", rejected=("uc_sfrcondo", "week")):
        notes.append(
            "median list price, condo: the research page has no condo href "
            "(metro list price links are all homes and single-family)"
        )
    if not listed(
        "median_sale_price",
        "uc_condo",
        rejected=("uc_sfrcondo", "sale_to_list", "new_con_", "week"),
    ):
        notes.append(
            "median sale price, condo: the research page has no condo href "
            "(metro sale price links are all homes and single-family)"
        )
    if not listed("/mlp/", "/Zip_") and not listed("/mlp/", "/City_") and not listed(
        "/mlp/", "/County_"
    ):
        notes.append(
            "median list price outside metro: the research page links metro "
            "files only"
        )
    if not listed("median_sale_price", "/Zip_", rejected=("sale_to_list", "new_con_")) and not listed(
        "median_sale_price", "/City_", rejected=("sale_to_list", "new_con_")
    ) and not listed(
        "median_sale_price", "/County_", rejected=("sale_to_list", "new_con_")
    ):
        notes.append(
            "median sale price outside metro: the research page links metro "
            "files only"
        )
    return notes


def parse_number(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_region_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text.isdigit():
        return None
    return int(text)


def is_california(row: dict[str, str | None]) -> bool:
    """Keep StateName CA, and the California state row when StateName is blank."""
    state_name = (row.get("StateName") or "").strip().upper()
    if state_name == "CA":
        return True
    if state_name:
        return False
    state = (row.get("State") or "").strip().upper()
    if state == "CA":
        return True
    region_type = (row.get("RegionType") or "").strip().lower()
    region_name = (row.get("RegionName") or "").strip().casefold()
    return region_type == "state" and region_name == "california"


def month_fields(fieldnames: list[str] | None) -> list[str]:
    if not fieldnames:
        return []
    return [name.strip() for name in fieldnames if MONTH_RE.match(name.strip())]


def iter_california_rows(reader: csv.DictReader, series: Series, pulled_at: datetime):
    fields = month_fields(reader.fieldnames)
    for row in reader:
        if not is_california(row):
            continue
        region_id = parse_region_id(row.get("RegionID"))
        region_name = (row.get("RegionName") or "").strip()
        if region_id is None or not region_name:
            continue
        region_type = (row.get("RegionType") or "").strip().lower()
        if not region_type:
            region_type = GEO_REGION_TYPE[series.geography]
        region = {
            "region_id": region_id,
            "region_name": region_name,
            "region_type": region_type,
            "state_code": "CA",
        }
        observations = []
        for field in fields:
            value = parse_number(row.get(field))
            if value is None:
                continue
            observations.append(
                (
                    region_id,
                    series.metric,
                    series.building_type,
                    series.bedrooms,
                    date.fromisoformat(field),
                    value,
                    pulled_at,
                )
            )
        if observations:
            yield region, observations


def open_csv(url: str) -> tuple[csv.DictReader, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=600)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SeriesNotFound(url) from exc
        raise
    text = io.TextIOWrapper(response, encoding="utf-8-sig", newline="")
    reader = csv.DictReader(text)
    if reader.fieldnames:
        first = (reader.fieldnames[0] or "").lstrip()
        if first.startswith("<"):
            response.close()
            raise RuntimeError(f"Expected a CSV from {url}")
        reader.fieldnames = [name.strip() for name in reader.fieldnames]
    return reader, response


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)


def write_batch(conn, regions: dict[int, dict], observations: list[tuple]) -> None:
    if not regions and not observations:
        return
    with conn.transaction():
        with conn.cursor() as cur:
            if regions:
                cur.executemany(
                    UPSERT_REGION_SQL,
                    [
                        (
                            region["region_id"],
                            region["region_name"],
                            region["region_type"],
                            region["state_code"],
                        )
                        for region in regions.values()
                    ],
                )
            if not observations:
                return
            cur.execute(
                """
                CREATE TEMP TABLE staging_observation (
                    region_id bigint,
                    metric text,
                    building_type text,
                    bedrooms text,
                    month date,
                    value numeric,
                    pulled_at timestamptz
                ) ON COMMIT DROP
                """
            )
            with cur.copy(
                """
                COPY staging_observation (
                    region_id, metric, building_type, bedrooms, month, value, pulled_at
                ) FROM STDIN
                """
            ) as copy:
                for observation in observations:
                    copy.write_row(observation)
            cur.execute(UPSERT_OBSERVATION_SQL)


def load_series(conn, series: Series, pulled_at: datetime) -> int:
    reader, response = open_csv(series.url)
    written = 0
    regions: dict[int, dict] = {}
    observations: list[tuple] = []
    try:
        for region, rows in iter_california_rows(reader, series, pulled_at):
            if observations and len(observations) >= BATCH_ROWS:
                write_batch(conn, regions, observations)
                written += len(observations)
                regions = {}
                observations = []
            regions[region["region_id"]] = region
            observations.extend(rows)
        if regions or observations:
            write_batch(conn, regions, observations)
            written += len(observations)
    finally:
        response.close()
    return written


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


def main() -> int:
    pulled_at = datetime.now(timezone.utc)
    urls, source = discover_csv_urls()
    print(f"CSV hrefs from {source}.")
    selected = select_series(urls)
    if not selected:
        print("No requested series were in the research page hrefs.", file=sys.stderr)
        return 1
    print(f"Discovered {len(urls)} CSV hrefs; loading {len(selected)} series.")
    for note in unpublished_notes(urls):
        print(f"not published: {note}")
    for series in selected:
        print(f"wired {series.label} {series.url}")

    skipped: list[Series] = []
    loaded = 0
    with connect() as conn:
        ensure_schema(conn)
        conn.commit()
        for series in selected:
            try:
                count = load_series(conn, series, pulled_at)
            except SeriesNotFound:
                skipped.append(series)
                print(f"skip 404 {series.label} {series.url}")
                continue
            loaded += 1
            print(f"loaded {count} observations | {series.label}")
    if skipped:
        print(f"Skipped {len(skipped)} series that returned HTTP 404.")
    else:
        print("No wired URL returned HTTP 404.")
    if loaded == 0:
        print("No series were loaded.", file=sys.stderr)
        return 1
    print(f"Finished {loaded} series at {pulled_at.isoformat()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
