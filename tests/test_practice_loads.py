"""Checks for the Census ZIP and national procedure loads."""

from __future__ import annotations

import io
import os
import unittest
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from email.message import Message
from unittest.mock import patch

import pull_census_zip_population as census
import pull_procedure_surgeon_fees as procedures

COUNT_AND_FEE = """
\f2024 Cosmetic Surgery Procedures
 Breast augmentation (implant placement for both primary and/or revisions)**     306,196       304,181             1%
 Breast reduction**                                                               76,734        76,031             1%
 Breast reduction in males (gynecomastia surgery)**                               26,430        25,888             2%
 Liposuction                                                                     349,728       347,782             1%
 Upper arm lift (brachioplasty)**                                                 23,527        23,058             2%
 Eyelid surgery (blepharoplasty)**                                               120,755       120,747             0%
 TOTAL                                                                           1,585,878     1,575,244           1%
 Breast augmentation (implant placement for both primary and/or revisions)**       306,196      304,334        99%           0.7%
\f2024 AVERAGE SURGEON/PHYSICIAN FEES                                                                                                               2024
 Breast augmentation (implant placement for both primary and/or revisions)**                                                                                                        $4,575-8,000
 Breast augmentation with fat grafting only (no implants)**                                                                                                                         $5,500-9,500
 Breast reduction (aesthetic patients only)**                                                                                                                                      $7,000-12,500
 Breast reduction in males (gynecomastia surgery)**                                                                                                                                 $5,000-9,000
 Liposuction**                                                                                                                                                                      $4,300-7,500
 Liposuction (submental/chin)**                                                                                                                                                     $3,000-5,500
 Upper arm lift (brachioplasty)**                                                                                                                                                  $6,000-10,500
 Eyelid surgery (upper blepharoplasty)**                                                                                                                                            $3,000-5,500
 Eyelid surgery (lower blepharoplasty)**                                                                                                                                            $3,709-6,500
ASPS has updated the presentation of surgeon fee data to reflect a projected range rather than a single price.
"""

COUNTS_ONLY = """
\f2025 Cosmetic Surgery Procedures
Breast augmentation (implant placement for both primary and/or revisions)   304,234     273,302         11%
Tummy tuck (abdominoplasty)                                                 173,251     170,544         2%
Brazilian butt lift (buttock augmentation with fat grafting)                 25,662      27,059         -5%
Eyelid surgery (blepharoplasty)                                             118,978     100,185         19%
TOTAL                                                                       1,523,330   1,425,390       7%
"""


class ProcedureTests(unittest.TestCase):
    def test_2024_ranges_are_not_copied_and_split_eyelid_fees_stay_out(self):
        rows = {row.procedure: row for row in procedures.facts_from_text(COUNT_AND_FEE, 2024)}
        self.assertEqual(rows["breast augmentation"].procedure_count, 306196)
        self.assertEqual(rows["breast augmentation"].surgeon_fee_low, 4575)
        self.assertEqual(rows["breast augmentation"].surgeon_fee_high, 8000)
        self.assertEqual(rows["breast reduction"].procedure_count, 76734)
        self.assertEqual(rows["breast reduction"].surgeon_fee_low, 7000)
        self.assertNotIn("breast reduction in males", rows)
        self.assertEqual(rows["liposuction"].surgeon_fee_low, 4300)
        self.assertEqual(rows["arm lift"].procedure_count, 23527)
        self.assertEqual(rows["arm lift"].surgeon_fee_high, 10500)
        self.assertEqual(rows["eyelid surgery"].procedure_count, 120755)
        self.assertIsNone(rows["eyelid surgery"].surgeon_fee_low)
        self.assertIsNone(rows["eyelid surgery"].surgeon_fee_high)

    def test_2025_counts_do_not_receive_2024_fees(self):
        earlier = {
            row.procedure: row for row in procedures.facts_from_text(COUNT_AND_FEE, 2024)
        }
        later = {
            row.procedure: row for row in procedures.facts_from_text(COUNTS_ONLY, 2025)
        }
        self.assertEqual(later["breast augmentation"].procedure_count, 304234)
        self.assertIsNone(later["breast augmentation"].surgeon_fee_low)
        self.assertNotEqual(
            later["breast augmentation"].surgeon_fee_low,
            earlier["breast augmentation"].surgeon_fee_low,
        )
        self.assertEqual(
            later["buttock augmentation with fat grafting"].procedure_count, 25662
        )
        self.assertIsNone(later["buttock augmentation with fat grafting"].surgeon_fee_high)
        self.assertEqual(later["tummy tuck"].procedure_count, 173251)
        self.assertEqual(later["eyelid surgery"].procedure_count, 118978)
        self.assertIsNone(later["eyelid surgery"].surgeon_fee_low)

    def test_seed_matches_official_2024_url_and_leaves_eyelid_fees_empty(self):
        rows = procedures.read_seed(2024)
        self.assertIsNotNone(rows)
        by_name = {row[1]: row for row in rows}
        self.assertEqual(len(by_name), 13)
        eyelid = by_name["eyelid surgery"]
        self.assertEqual(eyelid[2], 120755)
        self.assertIsNone(eyelid[3])
        self.assertIsNone(eyelid[4])
        self.assertIn("Not Los Angeles", eyelid[5])
        self.assertTrue(all(row[6] == procedures.report_url(2024) for row in rows))
        self.assertTrue(all(row[3] is not None for name, row in by_name.items() if name != "eyelid surgery"))

    def test_candidate_years_include_2024_through_the_run_year(self):
        years = procedures.candidate_years(datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual(years, [2026, 2025, 2024])


class CensusTests(unittest.TestCase):
    def test_zip_filter_keeps_the_overlap_only(self):
        kept = census.select_zips({"90001", "94102", "90210"}, {"90001", "90210", "91301"})
        self.assertEqual(kept, {"90001", "90210"})

    def test_new_vintage_only(self):
        self.assertEqual(census.vintages_to_insert([2024], set()), [2024])
        self.assertEqual(census.vintages_to_insert([2024], {2023}), [2024])
        self.assertEqual(census.vintages_to_insert([2024], {2023, 2024}), [])

    def test_latest_vintage_is_only_the_newest_published_year(self):
        def fake_request(url: str):
            if "/2026/" in url or "/2025/" in url:
                return 404, {}, b"<html><title>HTTP Status 404 Not Found</title></html>"
            if "/2024/" in url:
                return 200, {}, b'{"name": "B19013_001E"}'
            if "/2023/" in url:
                raise AssertionError("an older vintage must not be required")
            raise AssertionError("unexpected Census year")

        original = census.request_bytes
        previous = os.environ.get(census.CENSUS_API_KEY_ENV)
        os.environ.pop(census.CENSUS_API_KEY_ENV, None)
        census.request_bytes = fake_request
        try:
            self.assertEqual(census.latest_vintage(2026), 2024)
        finally:
            census.request_bytes = original
            if previous is None:
                os.environ.pop(census.CENSUS_API_KEY_ENV, None)
            else:
                os.environ[census.CENSUS_API_KEY_ENV] = previous

    def test_invalid_key_page_is_not_an_unpublished_vintage(self):
        def fake_request(_url: str):
            return (
                200,
                {"Content-Type": "text/html"},
                b"<html><title>Invalid Key</title>invalid_key.html</html>",
            )

        original = census.request_bytes
        previous = os.environ.get(census.CENSUS_API_KEY_ENV)
        os.environ[census.CENSUS_API_KEY_ENV] = "example-key"
        census.request_bytes = fake_request
        try:
            with self.assertRaises(SystemExit) as caught:
                census.latest_vintage(2026)
        finally:
            census.request_bytes = original
            if previous is None:
                os.environ.pop(census.CENSUS_API_KEY_ENV, None)
            else:
                os.environ[census.CENSUS_API_KEY_ENV] = previous
        self.assertEqual(census.exit_text(caught.exception), census.REJECTED_KEY)
        shown = census.public_error(caught.exception)
        self.assertEqual(shown, census.REJECTED_KEY)
        self.assertNotIn("two ACS", shown)
        self.assertNotIn("example-key", shown)
        self.assertNotIn("did not list", shown)

    def test_stored_vintage_inserts_nothing_and_does_not_delete(self):
        class Cursor:
            def __init__(self, years: set[int]):
                self.years = years
                self.statements: list[str] = []
                self.inserted: list[tuple] = []
                self._rows = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=None):
                text = " ".join(sql.split())
                self.statements.append(text)
                if "information_schema.tables" in text:
                    self._rows = [(1,)]
                elif "DISTINCT acs_year" in text:
                    self._rows = [(year,) for year in sorted(self.years)]
                elif "COUNT(*)" in text:
                    self._rows = [(len(self.years) + len(self.inserted),)]
                else:
                    self._rows = None

            def executemany(self, sql, rows):
                self.statements.append(" ".join(sql.split()))
                self.inserted.extend(rows)

            def fetchone(self):
                return None if not self._rows else self._rows[0]

            def fetchall(self):
                return list(self._rows or [])

        class Connection:
            def __init__(self, years: set[int]):
                self.cursor_obj = Cursor(years)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def transaction(self):
                return self

            def cursor(self):
                return self.cursor_obj

        stored = Connection({2023, 2024})
        inserted, total = census.insert_new_years(stored, [(2024, "90001")])
        self.assertEqual(inserted, 0)
        self.assertEqual(total, 2)
        self.assertEqual(stored.cursor_obj.inserted, [])
        self.assertNotIn("delete", " ".join(stored.cursor_obj.statements).lower())

        missing = Connection({2023})
        row = (2024, "90001")
        inserted, total = census.insert_new_years(missing, [row])
        self.assertEqual(inserted, 1)
        self.assertEqual(total, 2)
        self.assertEqual(missing.cursor_obj.inserted, [row])
        self.assertNotIn("delete", " ".join(missing.cursor_obj.statements).lower())

    def test_age_bands_sum_adults_and_drop_sentinels(self):
        self.assertIsNone(census.parse_estimate("-666666666"))
        self.assertEqual(census.parse_estimate("35.4"), Decimal("35.4"))
        estimates = {}
        for column, codes in census.band_codes().items():
            for index, code in enumerate(codes):
                estimates[code] = Decimal(index + 1)
        estimates[census.SEX_TOTALS["male_population"][0]] = Decimal(10)
        estimates[census.SEX_TOTALS["female_population"][0]] = Decimal(12)
        estimates[census.MEASURES["median_household_income"]] = Decimal(80000)
        estimates[census.MEASURES["per_capita_income"]] = Decimal(40000)
        estimates[census.MEASURES["median_age"]] = Decimal("36.5")
        row = census.row_from_estimates(2024, "90001", estimates)
        self.assertEqual(row[0], 2024)
        self.assertEqual(row[1], "90001")
        self.assertEqual(row[2], 80000)
        self.assertEqual(row[4], Decimal("36.5"))
        self.assertEqual(row[5], 10)
        self.assertEqual(row[6], 12)
        band_sum = sum(row[8:])
        self.assertEqual(row[7], band_sum)
        estimates["B01001_007E"] = None
        partial = census.row_from_estimates(2024, "90001", estimates)
        self.assertIsNone(partial[8])
        self.assertIsNone(partial[7])
        self.assertEqual(partial[5], 10)

    def test_variable_list_stays_within_the_api_limit(self):
        variables = census.estimate_variables()
        self.assertLessEqual(len(variables), 50)
        self.assertIn("B19013_001E", variables)
        self.assertIn("B19301_001E", variables)
        self.assertIn("B01002_001E", variables)

    def test_key_error_page_is_detected_without_printing_a_key(self):
        headers = {"X-DataWebAPI-KeyError": "1", "Location": "https://api.census.gov/data/missing_key.html"}
        self.assertTrue(census.looks_like_key_error(302, headers, b""))
        self.assertTrue(census.looks_like_key_error(200, {}, b"<title>Missing Key</title>"))
        self.assertTrue(
            census.looks_like_key_error(
                200,
                {"Location": "https://api.census.gov/data/invalid_key.html"},
                b"<html><title>Invalid Key</title></html>",
            )
        )
        url = census.census_data_url(2024, ("B19013_001E",), ["90001"])
        self.assertIn("in=state:06", url)
        self.assertNotIn("key=", url)
        captured: list[str] = []

        def fake_request(requested: str):
            captured.append(requested)
            return 200, {}, b'[["B19013_001E","zip code tabulation area"],["1","90001"]]'

        original = census.request_bytes
        previous = os.environ.get(census.CENSUS_API_KEY_ENV)
        os.environ[census.CENSUS_API_KEY_ENV] = "example-key"
        try:
            census.request_bytes = fake_request
            census.fetch_with_optional_key(
                "https://api.census.gov/data/2024/acs/acs5?get=B19013_001E"
            )
        finally:
            census.request_bytes = original
            if previous is None:
                os.environ.pop(census.CENSUS_API_KEY_ENV, None)
            else:
                os.environ[census.CENSUS_API_KEY_ENV] = previous
        self.assertEqual(len(captured), 1)
        self.assertIn("key=example-key", captured[0])
        self.assertNotIn("example-key", url)

    def test_variable_chunks_cover_every_code(self):
        variables = tuple(f"B{index:05d}E" for index in range(43))
        chunks = census.variable_chunks(variables)
        self.assertTrue(chunks)
        self.assertTrue(all(1 <= len(chunk) <= census.VARIABLE_CHUNK for chunk in chunks))
        self.assertEqual(tuple(code for chunk in chunks for code in chunk), variables)

    def test_census_message_drops_markup_links_and_keys(self):
        body = (
            b"<html><title>Query failed</title>"
            b"<a href='https://example.com/path?key=secret-value'>docs</a></html>"
        )
        message = census.census_message(400, body)
        self.assertIn("HTTP 400", message)
        self.assertIn("Query failed", message)
        self.assertNotIn("secret-value", message)
        self.assertNotIn("example.com", message)
        self.assertNotIn("<", message)

    def test_http_error_for_one_zip_is_not_treated_as_missing(self):
        def fake(_url: str) -> bytes:
            raise SystemExit(
                "The Census API returned HTTP 500: "
                "There was an error while running your query."
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            with self.assertRaises(SystemExit) as caught:
                census.fetch_query(
                    2023, ["90001"], ("B19013_001E",), state=census.CALIFORNIA_STATE
                )
        finally:
            census.fetch_with_optional_key = original
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertNotIn("too few", str(caught.exception))

    def test_ambiguous_zcta_list_retries_in_california(self):
        def fake(url: str) -> bytes:
            if "state:06" not in url:
                raise SystemExit(
                    "The Census API returned HTTP 400: error: ambiguous geography. "
                    "You must either specify a wildcard or full qualify it with state."
                )
            return (
                b'[["B19013_001E","zip code tabulation area"],'
                b'["10","90001"],["11","90002"]]'
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            rows = census.fetch_query(
                2023, ["90001", "90002"], ("B19013_001E",), state=""
            )
        finally:
            census.fetch_with_optional_key = original
        self.assertEqual(
            [row["zip code tabulation area"] for row in rows],
            ["90001", "90002"],
        )

    def test_rejected_variable_list_is_split_and_merged(self):
        def fake(url: str) -> bytes:
            get = url.split("get=", 1)[1].split("&", 1)[0]
            codes = [code for code in get.split(",") if code]
            if len(codes) > 1:
                raise SystemExit(
                    "The Census API returned HTTP 500: "
                    "There was an error while running your query."
                )
            code = codes[0]
            return (
                f'[["{code}","zip code tabulation area"],["5","90001"]]'
            ).encode()

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            rows = census.fetch_query(
                2023,
                ["90001"],
                ("B19013_001E", "B19301_001E"),
            )
        finally:
            census.fetch_with_optional_key = original
        self.assertEqual(rows[0]["zip code tabulation area"], "90001")
        self.assertEqual(rows[0]["B19013_001E"], "5")
        self.assertEqual(rows[0]["B19301_001E"], "5")

    def test_unknown_geography_for_one_zip_is_skipped(self):
        def fake(_url: str) -> bytes:
            raise SystemExit(
                "The Census API returned HTTP 400: unknown geography "
                "zip code tabulation area:99999"
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            rows = census.fetch_query(2023, ["99999"], ("B19013_001E",))
        finally:
            census.fetch_with_optional_key = original
        self.assertEqual(rows, [])

    def test_server_error_without_state_retries_in_california(self):
        def fake(url: str) -> bytes:
            if "state:06" not in url:
                raise SystemExit(
                    "The Census API returned HTTP 500: "
                    "There was an error while running your query. "
                    "https://api.census.gov/data/developers.html"
                )
            return (
                b'[["B19013_001E","zip code tabulation area"],'
                b'["10","90001"]]'
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            rows = census.fetch_query(2023, ["90001"], ("B19013_001E",), state="")
        finally:
            census.fetch_with_optional_key = original
        self.assertEqual(rows[0]["zip code tabulation area"], "90001")
        self.assertEqual(rows[0]["B19013_001E"], "10")
        shown = census.public_error(
            SystemExit(
                "The Census API returned HTTP 500: "
                "There was an error while running your query. "
                "https://api.census.gov/data/developers.html"
            )
        )
        self.assertIn("HTTP 500", shown)
        self.assertIn("error while running your query", shown)
        self.assertNotIn("developers.html", shown)
        self.assertNotIn("://", shown)

    def test_unreachable_network_is_retried_without_printing_the_key(self):
        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b"[]"

            def close(self):
                return None

        calls = {"n": 0}
        url = "https://api.census.gov/data/2024/acs/acs5?get=NAME&key=example-key"

        def fake_urlopen(_request, timeout=0):
            calls["n"] += 1
            self.assertEqual(timeout, 2700)
            if calls["n"] == 1:
                raise urllib.error.URLError(OSError(101, "Network is unreachable"))
            return Response()

        stdout = io.StringIO()
        with (
            patch.object(census.urllib.request, "urlopen", fake_urlopen),
            patch.object(census.time, "sleep") as slept,
            patch("sys.stdout", stdout),
        ):
            status, _headers, body = census.request_bytes(url)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"[]")
        self.assertEqual(calls["n"], 2)
        slept.assert_called_once_with(2)
        shown = stdout.getvalue()
        self.assertIn("could not connect", shown)
        self.assertNotIn("example-key", shown)
        self.assertNotIn(url, shown)

    def test_unreachable_network_is_reported_after_the_last_attempt(self):
        calls = {"n": 0}
        url = "https://api.census.gov/data/2024/acs/acs5?get=NAME&key=example-key"

        def fake_urlopen(_request, timeout=0):
            calls["n"] += 1
            raise urllib.error.URLError(OSError(101, "Network is unreachable"))

        stdout = io.StringIO()
        with (
            patch.object(census.urllib.request, "urlopen", fake_urlopen),
            patch.object(census.time, "sleep") as slept,
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(urllib.error.URLError) as caught:
                census.request_bytes(url)
        self.assertEqual(calls["n"], census.CONNECT_ATTEMPTS)
        self.assertEqual(slept.call_count, census.CONNECT_ATTEMPTS - 1)
        self.assertIn("Network is unreachable", str(caught.exception))
        self.assertNotIn("example-key", str(caught.exception))
        shown = stdout.getvalue()
        self.assertEqual(shown.count("Retrying."), census.CONNECT_ATTEMPTS - 1)
        self.assertNotIn("example-key", shown)
        self.assertNotIn(url, shown)
        self.assertEqual(
            census.public_error(caught.exception),
            "<urlopen error [Errno 101] Network is unreachable>",
        )

    def test_http_status_is_not_retried(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=0):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                request.full_url,
                500,
                "Error",
                Message(),
                io.BytesIO(b"There was an error while running your query."),
            )

        with (
            patch.object(census.urllib.request, "urlopen", fake_urlopen),
            patch.object(census.time, "sleep") as slept,
        ):
            status, _headers, body = census.request_bytes(
                "https://api.census.gov/data/2024/acs/acs5"
            )
        self.assertEqual(calls["n"], 1)
        slept.assert_not_called()
        self.assertEqual(status, 500)
        self.assertIn(b"error while running your query", body)


if __name__ == "__main__":
    unittest.main()
