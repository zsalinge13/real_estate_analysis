"""Load national ASPS aesthetic procedure counts and surgeon fees.

This table is national. It is not a Los Angeles fee schedule and it has no
office or NPI locations. Each run fetches the latest official procedural
statistics PDF it can download, upserts that report year, and leaves every
other year in public.procedure_surgeon_fees. A checked-in seed is used only
when the official PDF for that year cannot be fetched.

Surgeon fees are the published national range for the surgeon's fee only.
Anesthesia and facility fees are not included. A year with counts and no
published fee keeps the fee columns null. Fee ranges are not copied from
one report year onto another.

Every row upserted in a run gets the same dataset_updated_at. A report
year this run does not write keeps the timestamp it already had.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import dataset_stamp

DATABASE_URL_ENV = "DATABASE_URL"
TABLE_NAME = "procedure_surgeon_fees"
TABLE = "public.procedure_surgeon_fees"
OUTPUT_CSV_NAME = "procedure_surgeon_fees.csv"
OUTPUT_CSV = Path(__file__).resolve().parent / OUTPUT_CSV_NAME
SEED_DIR = Path(__file__).resolve().parent / "seeds"
FIRST_REPORT_YEAR = 2024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
# National surgeon fee only. The same note is stored on every row, including
# a year that published counts and did not publish fees.
FEE_BASIS = (
    "National surgeon fee only, excludes anesthesia and facility. Not Los Angeles."
)
COLUMNS = (
    "report_year",
    "procedure",
    "procedure_count",
    "surgeon_fee_low",
    "surgeon_fee_high",
    "fee_basis",
    "source_url",
)
PROCEDURES = (
    "breast augmentation",
    "breast lift",
    "breast reduction",
    "liposuction",
    "tummy tuck",
    "arm lift",
    "thigh lift",
    "buttock augmentation with fat grafting",
    "eyelid surgery",
    "facelift",
    "neck lift",
    "rhinoplasty",
    "forehead lift",
)
PROCEDURE_ORDER = {name: index for index, name in enumerate(PROCEDURES)}
COUNT_LINE = re.compile(
    r"^\s*(?P<name>.+?)\s+"
    r"(?P<count>\d{1,3}(?:,\d{3})*)\s+"
    r"(?P<prior>\d{1,3}(?:,\d{3})*)\s+"
    r"(?P<pct>-?\d+%)\s*$"
)
FEE_LINE = re.compile(
    r"^\s*(?P<name>.+?)\s+"
    r"\$(?P<low>\d{1,3}(?:,\d{3})*)\s*[-–—]\s*\$?(?P<high>\d{1,3}(?:,\d{3})*)\s*$"
)


@dataclass
class ProcedureFacts:
    procedure: str
    procedure_count: int | None = None
    surgeon_fee_low: int | None = None
    surgeon_fee_high: int | None = None

    def has_fact(self) -> bool:
        return self.procedure_count is not None or self.surgeon_fee_low is not None


def report_url(year: int) -> str:
    return (
        "https://www.plasticsurgery.org/documents/news/statistics/"
        f"{year}/plastic-surgery-statistics-report-{year}.pdf"
    )


def seed_path(year: int) -> Path:
    return SEED_DIR / f"procedure_surgeon_fees_{year}.csv"


def clean_name(name: str) -> str:
    return " ".join(name.replace("*", " ").split())


def canonical_procedure(name: str) -> str | None:
    """Map one report row onto a practice procedure, or skip it.

    Upper and lower blepharoplasty are separate fee rows. They are not
    combined into one eyelid-surgery range. Male breast reduction, chin
    liposuction, and fat-grafting-only breast augmentation are different
    procedures from the ones this load keeps.
    """
    text = clean_name(name).casefold()
    if not text:
        return None
    if (
        "gynecomastia" in text
        or "in males" in text
        or text.startswith("male breast")
        or "fat grafting only" in text
        or "implant removal" in text
        or "submental" in text
        or "lipedema" in text
        or "chin liposuction" in text
        or "upper blepharoplasty" in text
        or "lower blepharoplasty" in text
        or "buttock implant" in text
        or "buttock lift" in text
        or "lower body" in text
        or "upper body" in text
    ):
        return None
    if "breast augmentation" in text:
        return "breast augmentation"
    if "breast lift" in text or "mastopexy" in text:
        return "breast lift"
    if "breast reduction" in text:
        return "breast reduction"
    if text == "liposuction" or (
        "liposuction" in text and "chin" not in text and "lipedema" not in text
    ):
        if text == "liposuction" or text.startswith("liposuction"):
            return "liposuction"
    if "tummy tuck" in text or "abdominoplasty" in text:
        return "tummy tuck"
    if "arm lift" in text or "brachioplasty" in text:
        return "arm lift"
    if "thigh lift" in text:
        return "thigh lift"
    if "buttock augmentation with fat grafting" in text or "brazilian butt lift" in text:
        return "buttock augmentation with fat grafting"
    if "eyelid surgery" in text or text == "blepharoplasty":
        return "eyelid surgery"
    if "facelift" in text or "rhytidectomy" in text:
        return "facelift"
    if "neck lift" in text:
        return "neck lift"
    if "rhinoplasty" in text or "nose reshaping" in text:
        return "rhinoplasty"
    if "forehead lift" in text:
        return "forehead lift"
    return None


def parse_int(raw: str) -> int:
    return int(raw.replace(",", ""))


def heading_line(line: str) -> str:
    return line.replace("\f", " ").strip()


def count_section(text: str, year: int) -> list[str]:
    marker = f"{year} Cosmetic Surgery Procedures"
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if heading_line(line) == marker:
            start = index + 1
            break
    if start is None:
        raise SystemExit(
            f"The {year} report has no cosmetic surgery procedure count table."
        )
    section: list[str] = []
    for line in lines[start:]:
        cleaned = heading_line(line)
        if cleaned.startswith("TOTAL"):
            break
        if "Procedural Statistics Release" in cleaned:
            break
        section.append(line)
    return section


def fee_section(text: str) -> list[str]:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if "AVERAGE SURGEON/PHYSICIAN FEES" in line:
            start = index + 1
            break
    if start is None:
        return []
    section: list[str] = []
    for line in lines[start:]:
        cleaned = heading_line(line)
        if cleaned.startswith("ASPS has updated the presentation"):
            break
        if "Procedural Statistics Release" in cleaned:
            break
        section.append(line)
    return section


def set_count(facts: dict[str, ProcedureFacts], procedure: str, count: int) -> None:
    row = facts.setdefault(procedure, ProcedureFacts(procedure))
    if row.procedure_count is not None and row.procedure_count != count:
        raise SystemExit(f"The report published two counts for {procedure}.")
    row.procedure_count = count


def set_fee(facts: dict[str, ProcedureFacts], procedure: str, low: int, high: int) -> None:
    if high < low:
        raise SystemExit(f"The report fee range for {procedure} is out of order.")
    row = facts.setdefault(procedure, ProcedureFacts(procedure))
    if row.surgeon_fee_low is not None and (
        row.surgeon_fee_low != low or row.surgeon_fee_high != high
    ):
        raise SystemExit(f"The report published two fee ranges for {procedure}.")
    row.surgeon_fee_low = low
    row.surgeon_fee_high = high


def facts_from_text(text: str, year: int) -> list[ProcedureFacts]:
    """Read counts and fees published in one report. Do not fill gaps."""
    facts: dict[str, ProcedureFacts] = {}
    for line in count_section(text, year):
        match = COUNT_LINE.match(line)
        if match is None:
            continue
        procedure = canonical_procedure(match.group("name"))
        if procedure is None:
            continue
        set_count(facts, procedure, parse_int(match.group("count")))
    for line in fee_section(text):
        match = FEE_LINE.match(line)
        if match is None:
            continue
        procedure = canonical_procedure(match.group("name"))
        if procedure is None:
            continue
        set_fee(
            facts,
            procedure,
            parse_int(match.group("low")),
            parse_int(match.group("high")),
        )
    rows = [row for row in facts.values() if row.has_fact()]
    if not rows:
        raise SystemExit(f"The {year} report did not publish a kept procedure.")
    rows.sort(key=lambda row: PROCEDURE_ORDER[row.procedure])
    return rows


def pdf_text(path: Path) -> str:
    if shutil.which("pdftotext") is None:
        raise SystemExit("pdftotext is not installed.")
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        raise SystemExit("Could not read the procedure statistics PDF.")
    return result.stdout.decode("utf-8", errors="replace")


def download_pdf(url: str) -> Path | None:
    """Save a PDF response. A missing report returns None. The URL is not printed."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=180)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return None
        raise SystemExit(
            f"The procedure statistics PDF returned HTTP {exc.code}."
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit("The procedure statistics PDF could not be downloaded.") from exc
    try:
        data = response.read()
    finally:
        response.close()
    if not data.startswith(b"%PDF"):
        return None
    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        handle.write(data)
    finally:
        handle.close()
    return Path(handle.name)


def output_row(year: int, facts: ProcedureFacts, source_url: str) -> tuple:
    return (
        year,
        facts.procedure,
        facts.procedure_count,
        facts.surgeon_fee_low,
        facts.surgeon_fee_high,
        FEE_BASIS,
        source_url,
    )


def parse_optional_int(raw: str | None) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    return int(text)


def read_seed(year: int) -> list[tuple] | None:
    path = seed_path(year)
    if not path.exists():
        return None
    rows: list[tuple] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"The {year} procedure seed is missing columns.")
        for record in reader:
            report_year = int((record.get("report_year") or "").strip())
            if report_year != year:
                raise SystemExit(f"The {year} procedure seed contains another year.")
            procedure = (record.get("procedure") or "").strip()
            if procedure not in PROCEDURE_ORDER:
                raise SystemExit(f"The {year} procedure seed has an unexpected procedure.")
            low = parse_optional_int(record.get("surgeon_fee_low"))
            high = parse_optional_int(record.get("surgeon_fee_high"))
            if (low is None) != (high is None):
                raise SystemExit(f"The {year} procedure seed has a partial fee.")
            count = parse_optional_int(record.get("procedure_count"))
            if count is None and low is None:
                raise SystemExit(f"The {year} procedure seed has an empty procedure.")
            source = (record.get("source_url") or "").strip()
            if source != report_url(year):
                raise SystemExit(f"The {year} procedure seed source is not the official report.")
            basis = (record.get("fee_basis") or "").strip()
            if basis != FEE_BASIS:
                raise SystemExit(f"The {year} procedure seed fee basis changed.")
            rows.append((report_year, procedure, count, low, high, basis, source))
    if not rows:
        raise SystemExit(f"The {year} procedure seed is empty.")
    rows.sort(key=lambda row: (row[0], PROCEDURE_ORDER[row[1]]))
    return rows


def rows_for_year(year: int) -> tuple[list[tuple], str]:
    """Fetch the official PDF. Use the seed only when that PDF is unavailable."""
    url = report_url(year)
    path = download_pdf(url)
    if path is not None:
        try:
            facts = facts_from_text(pdf_text(path), year)
        finally:
            path.unlink(missing_ok=True)
        print(f"{year}: parsed the official PDF.")
        return [output_row(year, row, url) for row in facts], "pdf"
    seeded = read_seed(year)
    if seeded is not None:
        print(f"{year}: the official PDF was not available. Used the checked-in seed.")
        return seeded, "seed"
    print(f"{year}: no official PDF and no seed.")
    return [], "missing"


def candidate_years(today: datetime | None = None) -> list[int]:
    current = today if today is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    year = current.year
    return list(range(year, FIRST_REPORT_YEAR - 1, -1))


def collect_rows() -> list[tuple]:
    collected: list[tuple] = []
    for year in candidate_years():
        rows, _source = rows_for_year(year)
        collected.extend(rows)
    if not collected:
        raise SystemExit("No procedure statistics were fetched or seeded.")
    collected.sort(key=lambda row: (row[0], PROCEDURE_ORDER[row[1]]))
    return collected


def csv_cell(value: object) -> object:
    if value is None:
        return ""
    return value


def write_csv(rows: list[tuple]) -> None:
    tmp = OUTPUT_CSV.with_name(OUTPUT_CSV.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(COLUMNS)
            for row in rows:
                writer.writerow([csv_cell(value) for value in row])
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
    report_year integer NOT NULL,
    procedure text NOT NULL,
    procedure_count integer,
    surgeon_fee_low integer,
    surgeon_fee_high integer,
    fee_basis text NOT NULL,
    source_url text NOT NULL,
    {dataset_stamp.COLUMN} {dataset_stamp.SQL_TYPE},
    PRIMARY KEY (procedure, report_year)
);
"""


def upsert_rows(conn, rows: list[tuple], updated_at: datetime | None = None) -> int:
    stored = COLUMNS + (dataset_stamp.COLUMN,)
    placeholders = ", ".join(["%s"] * len(stored))
    column_list = ", ".join(stored)
    updates = ", ".join(
        f"{name} = EXCLUDED.{name}"
        for name in stored
        if name not in {"procedure", "report_year"}
    )
    sql = (
        f"INSERT INTO {TABLE} ({column_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (procedure, report_year) DO UPDATE SET {updates}"
    )
    _updated_at, stamped = dataset_stamp.stamp_rows(rows, updated_at)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(schema_sql())
            cur.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS "
                f"{dataset_stamp.COLUMN} {dataset_stamp.SQL_TYPE}"
            )
            cur.executemany(sql, stamped)
            cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
            return cur.fetchone()[0]


def public_error(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if "://" in text or "postgres" in lowered or "key=" in lowered or "password" in lowered:
        return "Could not load procedure_surgeon_fees."
    if text:
        return text
    return "Could not load procedure_surgeon_fees."


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


def summarize(rows: list[tuple]) -> None:
    years = sorted({row[0] for row in rows})
    for year in years:
        group = [row for row in rows if row[0] == year]
        with_fee = sum(1 for row in group if row[3] is not None)
        counts_only = sum(
            1 for row in group if row[2] is not None and row[3] is None
        )
        print(
            f"{year}: {len(group)} procedures, "
            f"{with_fee} with a surgeon fee range, "
            f"{counts_only} with a count and no fee."
        )


def main() -> int:
    try:
        rows = collect_rows()
    except SystemExit as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    write_csv(rows)
    summarize(rows)
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV_NAME}.")
    if not database_url():
        print(f"{DATABASE_URL_ENV} is not set. Left {TABLE} unchanged.")
        return 0
    updated_at = dataset_stamp.stamp()
    print(f"dataset_updated_at: {updated_at.isoformat()}")
    try:
        with connect() as conn:
            count = upsert_rows(conn, rows, updated_at)
    except SystemExit as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    print(f"Upserted {len(rows)} rows into {TABLE}. The table has {count} rows.")
    return notify_refresh_review(TABLE_NAME)


if __name__ == "__main__":
    sys.exit(main())
