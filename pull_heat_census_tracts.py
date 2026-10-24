"""Combine California Heat Assessment Tool historical census-tract CSVs.

The county list comes from the download page. Each historical county file is
appended into heat_census_tracts_ca.csv. Each run overwrites that file.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COUNTY_LIST = "https://cal-heat.org/select-county.php"
HISTORICAL = "https://cal-heat.org/download-historical-csv.php"
DOWNLOAD_PAGE = "https://www.cal-heat.org/download"
OUTPUT = Path(__file__).resolve().parent / "heat_census_tracts_ca.csv"
LOCATION = ("geoid_long", "census_county", "census_city", "census_tract")
USER_AGENT = "Mozilla/5.0"


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"California Heat Assessment Tool returned HTTP {exc.code}: {url}") from exc
    return body


def county_names() -> list[str]:
    payload = json.loads(fetch_bytes(COUNTY_LIST).decode("utf-8"))
    names = []
    for item in payload:
        name = str(item.get("id") or "").strip()
        if name:
            names.append(name)
    if not names:
        raise SystemExit(f"{COUNTY_LIST} did not list any counties.")
    return names


def output_header(source_header: list[str]) -> list[str]:
    missing = [name for name in LOCATION if name not in source_header]
    if missing:
        raise SystemExit(f"Historical heat CSV is missing columns: {missing}")
    rest = [name for name in source_header if name not in LOCATION]
    return list(LOCATION) + rest


def county_rows(county: str, expected: list[str] | None) -> tuple[list[str], list[dict[str, str]]]:
    url = HISTORICAL + "?" + urllib.parse.urlencode({"county": county})
    text = fetch_bytes(url).decode("utf-8-sig", errors="replace")
    stripped = text.lstrip()
    if not stripped or stripped.startswith("<") or stripped.startswith("<?php"):
        raise SystemExit(f"Historical heat file for {county} was not a CSV.")
    reader = csv.DictReader(io.StringIO(text))
    header = [name.strip() for name in (reader.fieldnames or [])]
    if not header or header[0] != "geoid_long":
        raise SystemExit(f"Historical heat file for {county} has no geoid_long column.")
    columns = output_header(header)
    if expected is not None and columns != expected:
        raise SystemExit(
            f"Historical heat columns for {county} differ from the first county. "
            f"Expected {expected}. Found {columns}."
        )
    rows = []
    for raw in reader:
        if raw is None:
            continue
        record = {(key or "").strip(): (value if value is not None else "") for key, value in raw.items()}
        geoid = (record.get("geoid_long") or "").strip()
        if not geoid or geoid.lower().startswith("warning"):
            continue
        rows.append({name: (record.get(name) or "").strip() for name in columns})
    if not rows:
        raise SystemExit(f"Historical heat file for {county} had no data rows.")
    return columns, rows


def main() -> int:
    print(f"County list: {COUNTY_LIST}")
    print(f"Download page: {DOWNLOAD_PAGE}")
    counties = county_names()
    tmp = OUTPUT.with_name(OUTPUT.name + ".tmp")
    count = 0
    header: list[str] | None = None
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            for index, county in enumerate(counties, start=1):
                columns, rows = county_rows(county, header)
                if header is None:
                    header = columns
                    writer.writerow(header)
                for row in rows:
                    writer.writerow([row[name] for name in header])
                    count += 1
                print(f"{index}/{len(counties)} {county}: {len(rows)} rows", flush=True)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if count == 0 or header is None:
        tmp.unlink(missing_ok=True)
        print("No historical heat rows were written.", file=sys.stderr)
        return 1
    tmp.replace(OUTPUT)
    print(f"Wrote {count} rows to {OUTPUT.name}.")
    print("Columns: " + ", ".join(header))
    return 0


if __name__ == "__main__":
    sys.exit(main())
