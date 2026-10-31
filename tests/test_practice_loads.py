"""Checks for the Census ZIP and national procedure loads."""

from __future__ import annotations

import http.client
import inspect
import io
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import dataset_stamp
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


class DatasetStampTests(unittest.TestCase):
    def test_one_timestamp_is_appended_to_every_row(self):
        moment = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
        used, rows = dataset_stamp.stamp_rows([(1, "a"), (2, "b")], moment)
        self.assertEqual(used, moment)
        self.assertEqual(rows, [(1, "a", moment), (2, "b", moment)])
        self.assertIs(rows[0][-1], rows[1][-1])
        self.assertEqual(dataset_stamp.COLUMN, "dataset_updated_at")
        self.assertEqual(dataset_stamp.SQL_TYPE, "timestamptz")

    def test_procedure_upsert_stamps_without_deleting_other_years(self):
        schema = procedures.schema_sql()
        self.assertIn("dataset_updated_at timestamptz", schema)
        self.assertIn("marketing_bullets text", schema)
        self.assertNotIn("DROP", schema.upper())
        source = inspect.getsource(procedures.upsert_rows)
        self.assertIn("dataset_stamp.COLUMN", source)
        self.assertIn("ensure_marketing_bullets(cur)", source)
        self.assertIn("ON CONFLICT (procedure, report_year) DO UPDATE SET {updates}", source)
        self.assertNotIn("DELETE", source.upper())
        self.assertNotIn("MARKETING_BULLETS", source)
        column_sql = inspect.getsource(procedures.ensure_marketing_bullets)
        self.assertEqual(procedures.MARKETING_BULLETS, "marketing_bullets")
        self.assertIn("ADD COLUMN IF NOT EXISTS {MARKETING_BULLETS} text", column_sql)
        self.assertNotIn("DROP", column_sql.upper())

        class Cursor:
            def __init__(self):
                self.statements: list[str] = []
                self.rows: list[tuple] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=None):
                self.statements.append(" ".join(sql.split()))

            def executemany(self, sql, rows):
                self.statements.append(" ".join(sql.split()))
                self.rows.extend(rows)

            def fetchone(self):
                return (4,)

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def transaction(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return self.cursor_obj

        moment = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
        conn = Connection()
        first = (2024, "eyelid surgery", 1, None, None, "basis", "https://example.test")
        second = (2025, "facelift", 2, 10, 20, "basis", "https://example.test")
        procedures.upsert_rows(conn, [first, second], moment)
        sql = " ".join(conn.cursor_obj.statements)
        self.assertIn("dataset_updated_at = EXCLUDED.dataset_updated_at", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS marketing_bullets text", sql)
        self.assertNotIn("DELETE", sql.upper())
        update_sql = next(
            statement
            for statement in conn.cursor_obj.statements
            if "DO UPDATE" in statement
        )
        self.assertNotIn("marketing_bullets", update_sql)
        self.assertEqual(
            conn.cursor_obj.rows,
            [first + (moment,), second + (moment,)],
        )
        self.assertIs(conn.cursor_obj.rows[0][-1], conn.cursor_obj.rows[1][-1])


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
            def __init__(self, pairs: set[tuple[int, str]]):
                self.pairs = set(pairs)
                self.statements: list[str] = []
                self.inserted: list[tuple] = []
                self.updates: list[tuple] = []
                self.blank_added: set[tuple[int, str]] = set()
                self.rowcount = 0
                self._rows = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                self.statements.append(text)
                self.rowcount = 0
                if text.startswith("UPDATE"):
                    key = (params[-2], params[-1])
                    if key in self.blank_added:
                        self.blank_added.discard(key)
                        self.updates.append(tuple(params))
                        self.rowcount = 1
                    self._rows = None
                elif "SELECT acs_year, zip" in text:
                    self._rows = list(self.pairs)
                elif "COUNT(*)" in text:
                    self._rows = [(len(self.pairs),)]
                else:
                    self._rows = None

            def executemany(self, sql, rows):
                self.statements.append(" ".join(sql.split()))
                self.inserted.extend(rows)
                self.pairs.update((int(row[0]), str(row[1])) for row in rows)

            def fetchone(self):
                return None if not self._rows else self._rows[0]

            def fetchall(self):
                return list(self._rows or [])

        class Connection:
            def __init__(self, pairs: set[tuple[int, str]]):
                self.cursor_obj = Cursor(pairs)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def transaction(self):
                return self

            def cursor(self):
                return self.cursor_obj

        stamp = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
        pad = (None,) * (len(census.COLUMNS) - 2)
        stored = Connection({(2023, "90001"), (2024, "90001")})
        inserted, _filled, total = census.insert_new_years(stored, [(2024, "90001") + pad], stamp)
        self.assertEqual(inserted, 0)
        self.assertEqual(total, 2)
        self.assertEqual(stored.cursor_obj.inserted, [])
        stored_sql = " ".join(stored.cursor_obj.statements)
        self.assertNotIn("delete", stored_sql.lower())
        self.assertIn("dataset_updated_at timestamptz", stored_sql)
        self.assertNotIn("DO UPDATE", stored_sql)

        missing = Connection({(2023, "90001")})
        row = (2024, "90001") + pad
        other = (2024, "90211") + pad
        inserted, _filled, total = census.insert_new_years(missing, [row, other], stamp)
        self.assertEqual(inserted, 2)
        self.assertEqual(total, 3)
        self.assertEqual(
            missing.cursor_obj.inserted,
            [row + (stamp,), other + (stamp,)],
        )
        self.assertIs(missing.cursor_obj.inserted[0][-1], missing.cursor_obj.inserted[1][-1])
        missing_sql = " ".join(missing.cursor_obj.statements)
        self.assertNotIn("delete", missing_sql.lower())
        self.assertIn("ON CONFLICT (zip, acs_year) DO NOTHING", missing_sql)
        self.assertNotIn("DO UPDATE", missing_sql)

        partial = Connection({(2024, "90001")})
        added = (2024, "90210") + pad
        inserted, _filled, total = census.insert_new_years(partial, [added], stamp)
        self.assertEqual(inserted, 1)
        self.assertEqual(partial.cursor_obj.inserted, [added + (stamp,)])
        self.assertNotIn("delete", " ".join(partial.cursor_obj.statements).lower())

    def test_stored_zip_gets_only_its_blank_race_and_education_filled(self):
        class Cursor:
            def __init__(self):
                self.statements: list[tuple[str, tuple | None]] = []
                self.inserted: list[tuple] = []
                self.blank = {(2024, "90001")}
                self.rowcount = 0
                self._rows = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                self.statements.append((text, params))
                self.rowcount = 0
                self._rows = None
                if text.startswith("UPDATE"):
                    key = (params[-2], params[-1])
                    if key in self.blank:
                        self.blank.discard(key)
                        self.rowcount = 1
                elif "SELECT acs_year, zip" in text:
                    self._rows = [(2024, "90001"), (2024, "90002")]
                elif "COUNT(*)" in text:
                    self._rows = [(3,)]

            def executemany(self, sql, rows):
                self.inserted.extend(rows)

            def fetchone(self):
                return self._rows[0]

            def fetchall(self):
                return list(self._rows)

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def transaction(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return self.cursor_obj

        stamp = datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)
        base = (None,) * (len(census.BASE_COLUMNS) - 2)
        race = tuple(range(1, len(census.RACE_EDUCATION_COLUMNS) + 1))
        profile = tuple(range(100, 100 + len(census.PROFILE_COLUMNS)))
        stored = (2024, "90001") + base + race + profile
        already_filled = (2024, "90002") + base + race + profile
        blank = (2024, "90003") + base + (None,) * len(census.ADDED_COLUMNS)
        conn = Connection()
        inserted, filled, total = census.insert_new_years(
            conn, [stored, already_filled, blank], stamp
        )
        self.assertEqual((inserted, filled, total), (1, 1, 3))
        self.assertEqual(conn.cursor_obj.inserted, [blank + (stamp,)])
        updates = [(sql, params) for sql, params in conn.cursor_obj.statements if sql.startswith("UPDATE")]
        self.assertEqual(updates[0][1], race + (stamp, 2024, "90001"))
        self.assertEqual(len(updates), 4)
        self.assertEqual(updates[1][1], race + (stamp, 2024, "90002"))
        self.assertEqual(updates[2][1], profile + (stamp, 2024, "90001"))
        sql = updates[0][0]
        for name in census.RACE_EDUCATION_COLUMNS:
            self.assertIn(f"{name} IS NULL", sql)
        for name in census.PROFILE_COLUMNS:
            self.assertNotIn(f"{name} IS NULL", sql)
            self.assertIn(f"{name} IS NULL", updates[2][0])
        for name in census.BASE_COLUMNS[2:]:
            self.assertNotIn(f"{name} =", sql)
        added_sql = " ".join(text for text, _params in conn.cursor_obj.statements)
        self.assertIn("ADD COLUMN IF NOT EXISTS education_bachelors integer", added_sql)
        self.assertNotIn("delete", added_sql.lower())

    def test_profile_set_fills_rows_that_already_have_race_and_education(self):
        class Cursor:
            def __init__(self):
                self.updates = []
                self.rowcount = 0
                self._rows = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                self.rowcount = 0
                self._rows = None
                if text.startswith("UPDATE"):
                    self.updates.append(text)
                    self.rowcount = 1 if "households IS NULL" in text else 0
                elif "SELECT acs_year, zip" in text:
                    self._rows = [(2024, "90001")]
                elif "COUNT(*)" in text:
                    self._rows = [(1,)]

            def executemany(self, sql, rows):
                raise AssertionError("A stored ZIP is not inserted again.")

            def fetchone(self):
                return self._rows[0]

            def fetchall(self):
                return list(self._rows)

        class Connection:
            def __init__(self):
                self.cursor_obj = Cursor()

            def transaction(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return self.cursor_obj

        row = (2024, "90001") + (None,) * (len(census.BASE_COLUMNS) - 2) + (1,) * len(census.ADDED_COLUMNS)
        conn = Connection()
        stamp = datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(census.insert_new_years(conn, [row], stamp), (0, 1, 1))
        self.assertEqual(len(conn.cursor_obj.updates), 2)

    def test_profile_groups_sum_their_codes(self):
        estimates = {code: Decimal(1) for _name, codes in census.field_groups() for code in codes}
        estimates[census.MEASURES["median_age"]] = Decimal("36.5")
        row = dict(zip(census.COLUMNS, census.row_from_estimates(2024, "90001", estimates)))
        self.assertEqual(row["female_population_18_34"], 6)
        self.assertEqual(row["male_population_65_plus"], 6)
        self.assertEqual(row["female_population_55_64"], 3)
        self.assertEqual(row["households"], 1)
        self.assertEqual(row["households_income_200000_plus"], 1)
        self.assertEqual(row["mothers_with_children_under_6"], 2)
        self.assertEqual(row["recent_births_15_34"], 4)
        self.assertEqual(row["recent_births_35_44"], 2)
        self.assertEqual(row["owner_occupied_householder_15_34"], 2)
        self.assertEqual(row["renter_occupied_householder_55_plus"], 5)
        self.assertEqual(row["women_arts_design_entertainment_sports_media"], 1)
        bounds = [low for _column, _code, _phrase, low in census.HOUSEHOLD_INCOME_BRACKETS]
        self.assertEqual(bounds, sorted(bounds))
        self.assertEqual(len(bounds), 16)
        self.assertIn("B01001_037E", dict(census.SEX_BY_AGE_GROUPS["female_population_35_44"]))
        self.assertTrue(all(len(codes) <= 50 for _name, codes in census.field_groups()))
        self.assertEqual(census.ADDED_COLUMNS, census.RACE_EDUCATION_COLUMNS + census.PROFILE_COLUMNS)

    def test_race_and_education_groups_sum_their_codes(self):
        estimates = {code: Decimal(1) for _name, codes in census.field_groups() for code in codes}
        estimates[census.MEASURES["median_age"]] = Decimal("36.5")
        row = dict(zip(census.COLUMNS, census.row_from_estimates(2024, "90001", estimates)))
        self.assertEqual(row["hispanic_or_latino_population"], 1)
        self.assertEqual(row["non_hispanic_white_population"], 1)
        self.assertEqual(row["non_hispanic_other_or_multiracial_population"], 4)
        self.assertEqual(row["education_less_than_high_school"], 15)
        self.assertEqual(row["education_high_school"], 2)
        self.assertEqual(row["education_some_college_or_associate"], 3)
        self.assertEqual(row["education_bachelors"], 1)
        self.assertEqual(row["education_graduate_or_professional"], 3)
        self.assertEqual(row["population_25_plus"], 24)
        codes = census.group_codes(census.RACE_ETHNICITY_GROUPS) + census.group_codes(census.EDUCATION_GROUPS)
        self.assertNotIn("B03002_001E", codes)
        self.assertNotIn("B03002_002E", codes)
        self.assertNotIn("B03002_010E", codes)
        self.assertEqual(len(codes), len(set(codes)))
        estimates["B15003_022E"] = None
        estimates["B03002_008E"] = None
        partial = dict(zip(census.COLUMNS, census.row_from_estimates(2024, "90001", estimates)))
        self.assertIsNone(partial["education_bachelors"])
        self.assertIsNone(partial["population_25_plus"])
        self.assertIsNone(partial["non_hispanic_other_or_multiracial_population"])
        self.assertEqual(partial["education_high_school"], 2)
        self.assertEqual(census.COLUMNS[: len(census.BASE_COLUMNS)], census.BASE_COLUMNS)
        self.assertTrue(set(census.ADDED_COLUMNS) <= census.INTEGER_COLUMNS)
        schema = census.schema_sql()
        for name in census.ADDED_COLUMNS:
            self.assertIn(f"{name} integer,", schema)

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
        band_sum = sum(row[8:13])
        self.assertEqual(row[7], band_sum)
        estimates["B01001_007E"] = None
        partial = census.row_from_estimates(2024, "90001", estimates)
        self.assertIsNone(partial[8])
        self.assertIsNone(partial[7])
        self.assertEqual(partial[5], 10)

    def test_variable_list_stays_within_the_api_limit(self):
        variables = census.estimate_variables()
        self.assertTrue(all(len(codes) <= 50 for _name, codes in census.field_groups()))
        self.assertIn("B03002_012E", variables)
        self.assertIn("B15003_025E", variables)
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
        self.assertNotIn("in=", url)
        self.assertNotIn("state", url)
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
                census.fetch_query(2023, ["90001"], ("B19013_001E",))
        finally:
            census.fetch_with_optional_key = original
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertNotIn("too few", str(caught.exception))

    def test_ambiguous_zcta_list_splits_without_a_state(self):
        def fake(url: str) -> bytes:
            self.assertNotIn("state:06", url)
            self.assertNotIn("in=", url)
            for_part = url.split("for=", 1)[1].split("&", 1)[0]
            zips = for_part.split(":", 1)[1].split(",")
            if len(zips) > 1:
                raise SystemExit(
                    "The Census API returned HTTP 400: error: ambiguous geography. "
                    "You must either specify a wildcard or full qualify it with state."
                )
            zip_code = zips[0]
            return (
                f'[["B19013_001E","zip code tabulation area"],["10","{zip_code}"]]'
            ).encode()

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            rows = census.fetch_query(2023, ["90001", "90002"], ("B19013_001E",))
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

    def test_server_error_does_not_add_a_state(self):
        seen: list[str] = []

        def fake(url: str) -> bytes:
            seen.append(url)
            raise SystemExit(
                "The Census API returned HTTP 500: "
                "There was an error while running your query. "
                "https://api.census.gov/data/developers.html"
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            with self.assertRaises(SystemExit) as caught:
                census.fetch_query(2024, ["90001"], ("B19013_001E",))
        finally:
            census.fetch_with_optional_key = original
        self.assertEqual(len(seen), 1)
        self.assertNotIn("state:06", seen[0])
        self.assertIn("HTTP 500", str(caught.exception))
        shown = census.public_error(caught.exception)
        self.assertIn("HTTP 500", shown)
        self.assertIn("error while running your query", shown)
        self.assertNotIn("developers.html", shown)
        self.assertNotIn("://", shown)

    def test_zcta_url_omits_the_state(self):
        url = census.census_data_url(2024, ("B19013_001E",), ["90001", "90210"])
        self.assertIn("zip%20code%20tabulation%20area:90001,90210", url)
        self.assertNotIn("in=", url)
        self.assertNotIn("state", url)

    def test_hierarchy_error_is_not_a_missing_zip(self):
        self.assertEqual(
            census.geography_problem(
                "The Census API returned HTTP 400: "
                "error: unknown/unsupported geography hierarchy"
            ),
            "hierarchy",
        )
        self.assertEqual(
            census.geography_problem(
                "error: unknown/unsupported geography heirarchy"
            ),
            "hierarchy",
        )
        self.assertEqual(
            census.geography_problem(
                "The Census API returned HTTP 400: unknown geography "
                "zip code tabulation area:99999"
            ),
            "unknown",
        )

        def fake(_url: str) -> bytes:
            raise SystemExit(
                "The Census API returned HTTP 400: "
                "error: unknown/unsupported geography hierarchy"
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            with self.assertRaises(SystemExit) as caught:
                census.fetch_query(2024, ["90001"], ("B19013_001E",))
        finally:
            census.fetch_with_optional_key = original
        self.assertIn("hierarchy", str(caught.exception))
        self.assertNotIn("too few", str(caught.exception))

    def test_hierarchy_error_does_not_add_a_state(self):
        seen: list[str] = []

        def fake(url: str) -> bytes:
            seen.append(url)
            raise SystemExit(
                "The Census API returned HTTP 400: "
                "error: unknown/unsupported geography hierarchy"
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            with self.assertRaises(SystemExit) as caught:
                census.fetch_query(2024, ["90001", "90002"], ("B19013_001E",))
        finally:
            census.fetch_with_optional_key = original
        self.assertEqual(len(seen), 1)
        self.assertNotIn("state:06", seen[0])
        self.assertNotIn("in=", seen[0])
        self.assertIn("hierarchy", str(caught.exception))

    def test_unknown_geography_list_splits_zips_before_variables(self):
        calls: list[str] = []

        def fake(url: str) -> bytes:
            calls.append(url)
            for_part = url.split("for=", 1)[1].split("&", 1)[0]
            zips = for_part.split(":", 1)[1].split(",")
            if zips == ["99999"]:
                raise SystemExit(
                    "The Census API returned HTTP 400: unknown geography "
                    "zip code tabulation area:99999"
                )
            if len(zips) > 1:
                raise SystemExit(
                    "The Census API returned HTTP 400: unknown geography "
                    "zip code tabulation area"
                )
            return (
                b'[["B19013_001E","B19301_001E","zip code tabulation area"],'
                b'["5","6","90001"]]'
            )

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            rows = census.fetch_query(
                2024, ["90001", "99999"], ("B19013_001E", "B19301_001E")
            )
        finally:
            census.fetch_with_optional_key = original
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["zip code tabulation area"], "90001")
        self.assertEqual(rows[0]["B19301_001E"], "6")
        self.assertEqual(len(calls), 3)
        for url in calls:
            get = url.split("get=", 1)[1].split("&", 1)[0]
            self.assertIn("B19013_001E", get)
            self.assertIn("B19301_001E", get)

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
        slept.assert_called_once_with(census.retry_pause_seconds(0))
        self.assertGreater(census.retry_pause_seconds(0), 2)
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
        self.assertGreater(census.CONNECT_ATTEMPTS, 4)
        self.assertEqual(calls["n"], census.CONNECT_ATTEMPTS)
        self.assertEqual(slept.call_count, census.CONNECT_ATTEMPTS - 1)
        pauses = [call.args[0] for call in slept.call_args_list]
        self.assertEqual(
            pauses,
            [census.retry_pause_seconds(index) for index in range(census.CONNECT_ATTEMPTS - 1)],
        )
        self.assertGreater(pauses[0], 2)
        self.assertTrue(all(later > earlier for earlier, later in zip(pauses, pauses[1:])))
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

    def test_field_groups_are_separate_calls(self):
        groups = dict(census.field_groups())
        self.assertEqual(
            list(groups),
            [
                "income",
                "age",
                "sex",
                "age_bands",
                "race_ethnicity",
                "education",
                "household_income",
                "mothers",
                "tenure",
                "occupation",
            ],
        )
        self.assertEqual(len(groups["race_ethnicity"]), 8)
        self.assertEqual(len(groups["education"]), 24)
        self.assertEqual(groups["income"], ("B19013_001E", "B19301_001E"))
        self.assertEqual(groups["age"], ("B01002_001E",))
        self.assertEqual(groups["sex"], ("B01001_002E", "B01001_026E"))
        self.assertIn("B01001_007E", groups["age_bands"])
        self.assertNotIn("B19013_001E", groups["age_bands"])
        self.assertTrue(all(1 <= len(codes) <= 50 for codes in groups.values()))
        self.assertEqual(census.BATCH_SIZE, 10)
        self.assertGreaterEqual(census.CALL_PAUSE_SECONDS, 2)

        calls: list[str] = []

        def fake(url: str) -> bytes:
            calls.append(url)
            self.assertNotIn("state:06", url)
            self.assertNotIn("in=", url)
            codes = url.split("get=", 1)[1].split("&", 1)[0].split(",")
            if "B19013_001E" in codes and "B01002_001E" in codes:
                raise AssertionError("income and age were requested together")
            if "B01001_007E" in codes:
                raise SystemExit(
                    "The Census API returned HTTP 500: "
                    "There was an error while running your query."
                )
            names = ",".join(f'"{code}"' for code in codes)
            values = ",".join('"1"' for _ in codes)
            return (
                f'[[{names},"zip code tabulation area"],[{values},"90001"]]'
            ).encode()

        original = census.fetch_with_optional_key
        census.fetch_with_optional_key = fake
        try:
            with patch.object(census.time, "sleep"), self.assertRaises(SystemExit):
                census.fetch_zip_batch(2024, ["90001", "94102"])
        finally:
            census.fetch_with_optional_key = original
        self.assertGreaterEqual(len(calls), 4)
        self.assertIn("B19013_001E", calls[0])
        self.assertIn("B19301_001E", calls[0])
        self.assertNotIn("B01002_001E", calls[0])
        self.assertIn("B01002_001E", calls[1])
        self.assertNotIn("B19013_001E", calls[1])
        self.assertIn("B01001_002E", calls[2])
        self.assertIn("B01001_026E", calls[2])
        self.assertNotIn("B19013_001E", calls[2])
        self.assertIn("B01001_007E", calls[3])
        income_calls = [url for url in calls if "B19013_001E" in url]
        self.assertEqual(len(income_calls), 1)
        self.assertIn("94102", calls[0])

    def test_dataframe_keeps_los_angeles_after_the_california_download(self):
        text = (
            "GEOID_ZCTA5_20|GEOID_COUNTY_20|AREALAND_PART|AREAWATER_PART\n"
            "90001|06037|10|0\n"
            "94102|06075|5|0\n"
            "10001|36061|5|0\n"
            "89001|32003|9|0\n"
            "89001|06071|1|0\n"
            "00000|06037|0|0\n"
        )
        california = census.california_zctas_from_crosswalk(text)
        self.assertEqual(california, {"90001", "94102", "89001"})
        frame = census.pd.DataFrame(
            [
                {census.GEO_COLUMN: "90001", "B19013_001E": "10"},
                {census.GEO_COLUMN: "94102", "B19013_001E": "20"},
                {census.GEO_COLUMN: "10001", "B19013_001E": "30"},
                {census.GEO_COLUMN: "89001", "B19013_001E": "40"},
            ]
        )
        kept = census.filter_downloaded_zips(
            frame, california, {"90001", "90210"}
        )
        self.assertEqual(kept[census.GEO_COLUMN].tolist(), ["90001"])

    def test_finished_batch_is_saved_before_the_next_request(self):
        requested: list[list[str]] = []

        def fake_batch(_year: int, zips: list[str]):
            requested.append(list(zips))
            if zips[0] == "94103":
                raise TimeoutError("timed out")
            return [
                {census.GEO_COLUMN: "90001", "B19013_001E": "10"},
                {census.GEO_COLUMN: "94102", "B19013_001E": "11"},
            ]

        saved: list[list[str]] = []

        def on_batch(all_rows, _new_rows):
            census.write_csv(all_rows)
            saved.append([row[1] for row in all_rows])

        original = census.fetch_zip_batch
        census.fetch_zip_batch = fake_batch
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "census_zip_population.csv"
                with (
                    patch.object(census, "OUTPUT_CSV", output),
                    patch.object(census, "BATCH_SIZE", 2),
                    patch.object(census.time, "sleep"),
                    self.assertRaises(TimeoutError),
                ):
                    census.rows_for_vintage(
                        2024,
                        ["90001", "94102", "94103", "94104"],
                        {"90001"},
                        on_batch,
                    )
                self.assertEqual(requested, [["90001", "94102"], ["94103", "94104"]])
                self.assertEqual(saved, [["90001"]])
                text = output.read_text(encoding="utf-8")
        finally:
            census.fetch_zip_batch = original
        self.assertIn("90001", text)
        self.assertNotIn("94102", text)
        self.assertNotIn("94103", text)

    def test_read_timeout_is_retried_like_a_dropped_connection(self):
        class Response:
            def __init__(self, body: bytes | None):
                self.status = 200
                self.headers = {}
                self._body = body

            def read(self):
                if self._body is None:
                    raise TimeoutError("The read operation timed out")
                return self._body

            def close(self):
                return None

        calls = {"n": 0}

        def fake_urlopen(_request, timeout=0):
            calls["n"] += 1
            self.assertEqual(timeout, 2700)
            if calls["n"] == 1:
                return Response(None)
            return Response(b"[]")

        stdout = io.StringIO()
        with (
            patch.object(census.urllib.request, "urlopen", fake_urlopen),
            patch.object(census.time, "sleep") as slept,
            patch("sys.stdout", stdout),
        ):
            status, _headers, body = census.request_bytes(
                "https://api.census.gov/data/2024/acs/acs5?get=NAME"
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"[]")
        self.assertEqual(calls["n"], 2)
        slept.assert_called_once_with(census.retry_pause_seconds(0))
        self.assertGreater(census.retry_pause_seconds(0), 2)
        shown = stdout.getvalue()
        self.assertIn("timed out", shown)
        self.assertIn("Retrying.", shown)

    def test_closed_connection_is_retried_with_a_longer_pause(self):
        class Response:
            def __init__(self, body: bytes | None):
                self.status = 200
                self.headers = {}
                self._body = body

            def read(self):
                if self._body is None:
                    raise http.client.RemoteDisconnected(
                        "Remote end closed connection without response"
                    )
                return self._body

            def close(self):
                return None

        calls = {"n": 0}

        def fake_urlopen(_request, timeout=0):
            calls["n"] += 1
            self.assertEqual(timeout, 2700)
            if calls["n"] == 1:
                return Response(None)
            return Response(b"[]")

        stdout = io.StringIO()
        with (
            patch.object(census.urllib.request, "urlopen", fake_urlopen),
            patch.object(census.time, "sleep") as slept,
            patch("sys.stdout", stdout),
        ):
            status, _headers, body = census.request_bytes(
                "https://api.census.gov/data/2024/acs/acs5?get=NAME"
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"[]")
        self.assertEqual(calls["n"], 2)
        slept.assert_called_once_with(census.retry_pause_seconds(0))
        shown = stdout.getvalue()
        self.assertIn("connection closed", shown)
        self.assertIn("Retrying.", shown)
        self.assertNotIn("state:06", shown)

    def test_field_calls_wait_a_couple_of_seconds(self):
        def fake(_year: int, _zips: list[str], _variables: tuple[str, ...]):
            return []

        with (
            patch.object(census, "fetch_field_piece", fake),
            patch.object(census.time, "sleep") as slept,
        ):
            census.fetch_zip_batch(2024, ["90001"] * census.BATCH_SIZE)
        pauses = [call.args[0] for call in slept.call_args_list]
        self.assertEqual(pauses, [census.CALL_PAUSE_SECONDS] * (len(census.field_groups()) - 1))
        self.assertGreaterEqual(census.CALL_PAUSE_SECONDS, 2)
        self.assertEqual(len(["90001"] * census.BATCH_SIZE), 10)


if __name__ == "__main__":
    unittest.main()
