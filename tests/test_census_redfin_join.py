"""The chained Redfin load keeps one census year, and the join uses its latest month."""

from __future__ import annotations

import contextlib
import csv
import inspect
import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

import join_census_redfin as join
import load_california
import practice
import pull_census_zip_population as census

ROOT = Path(__file__).resolve().parents[1]


def census_row(zip_code: str, year: int, income: str = "", age: str = "") -> dict[str, str]:
    row = {column: "" for column in census.COLUMNS}
    row["acs_year"] = str(year)
    row["zip"] = zip_code
    row["median_household_income"] = income
    row["median_age"] = age
    return row


def redfin_row(
    postal: str,
    month: str,
    price: str = "",
    name: str = "Los Angeles",
) -> dict[str, str]:
    row = {column: "" for column in load_california.LOAD_COLUMNS}
    row["month_date_yyyymm"] = month
    row["postal_code"] = postal
    row["zip_name"] = name
    row["median_listing_price"] = price
    row["date_pulled_yyyymmdd"] = "2026_10_27"
    row["practice_zip_code"] = practice.PRACTICE_ZIP_CODE
    row["county"] = "Los Angeles"
    row["distance_from_practice_miles"] = "8.50"
    return row


def source_row(month_yyyymm: str, postal: str, price: str) -> dict[str, str]:
    row = {column: "" for column in load_california.KEPT_COLUMNS}
    row["month_date_yyyymm"] = month_yyyymm
    row["postal_code"] = postal
    row["zip_name"] = "Los Angeles"
    row["median_listing_price"] = price
    row["active_listing_count"] = "4"
    return row


def history_file(rows: list[dict[str, str]], extra: str = "") -> io.BytesIO:
    """A source CSV with the kept columns plus an unused column the reader must skip."""
    buffer = io.StringIO()
    fieldnames = list(load_california.KEPT_COLUMNS) + ([extra] if extra else [])
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return io.BytesIO(buffer.getvalue().encode("utf-8"))


def read_rows(handle, locations, census_year=None, chunk_rows=2) -> list[tuple]:
    rows: list[tuple] = []
    for chunk in load_california.read_california_rows(
        handle, "2026_10_27", locations, census_year, chunk_rows=chunk_rows
    ):
        rows.extend(chunk)
    return rows


class CensusYearLoadTests(unittest.TestCase):
    def test_default_run_is_still_the_full_replace(self):
        self.assertIsNone(load_california.census_year_arg(["load_california.py"]))
        source = inspect.getsource(load_california.load_history)
        self.assertIn("if census_year is None:", source)
        self.assertIn("return replace_table(conn)", source)
        self.assertIn("return upsert_census_year(conn, census_year, locations, updated_at)", source)
        replace = inspect.getsource(load_california.replace_table)
        self.assertIn("DELETE FROM {TABLE}", replace)

    def test_census_year_upsert_does_not_delete(self):
        sql = load_california.census_year_upsert_sql()
        self.assertNotIn("DELETE", sql.upper())
        self.assertIn("ON CONFLICT (postal_code, month_date_yyyymm) DO UPDATE", sql)
        self.assertIn("dataset_updated_at = EXCLUDED.dataset_updated_at", sql)
        self.assertIn(load_california.TABLE, sql)
        self.assertNotIn("DELETE", sql.upper())
        schema = load_california.schema_sql()
        self.assertIn("dataset_updated_at timestamptz", schema)
        self.assertNotIn("dataset_updated_at timestamptz NOT NULL", schema)
        ensured = inspect.getsource(load_california.ensure_schema)
        self.assertIn("STORED_COLUMNS", ensured)
        self.assertNotIn("if name in LOAD_COLUMNS", ensured)
        upsert = inspect.getsource(load_california.upsert_census_year)
        self.assertNotIn("DELETE FROM", upsert)

    def test_census_year_flag_accepts_only_a_four_digit_year(self):
        self.assertEqual(
            load_california.census_year_arg(["load_california.py", "--census-year", "2024"]),
            2024,
        )
        with self.assertRaises(SystemExit):
            load_california.census_year_arg(["load_california.py", "--census-year", "24"])
        with self.assertRaises(SystemExit):
            load_california.parse_census_year("1999")

    def test_only_months_in_the_census_year_are_kept(self):
        self.assertTrue(load_california.in_census_year("2024_12", 2024))
        self.assertFalse(load_california.in_census_year("2025_01", 2024))
        self.assertFalse(load_california.in_census_year("2024_13", 2024))
        self.assertFalse(load_california.in_census_year("2024-12", 2024))
        self.assertFalse(load_california.in_census_year("2024_12", True))
        pattern = load_california.census_year_month_pattern(2024)
        self.assertIsNotNone(re.fullmatch(pattern, "2024_09"))
        self.assertIsNone(re.fullmatch(pattern, "2025_12"))
        self.assertIsNone(re.fullmatch(pattern, "2026_09"))
        handle = history_file(
            [
                source_row("202412", "90001", "10"),
                source_row("202501", "90001", "99"),
                source_row("202309", "90001", "80"),
                source_row("202401", "90002", "8"),
                source_row("202412", "10001", "7"),
            ]
        )
        kept = read_rows(
            handle,
            {
                "90001": practice.AreaZip("Los Angeles", Decimal("12.34")),
                "90002": practice.AreaZip("Los Angeles", Decimal("13.00")),
            },
            2024,
        )
        self.assertEqual([row[0] for row in kept], ["2024_12", "2024_01"])
        self.assertEqual([row[1] for row in kept], ["90001", "90002"])
        self.assertEqual(kept[0][3], "10")
        self.assertEqual(kept[0][6], 4)
        self.assertEqual(kept[0][-3:], ("90210", "Los Angeles", Decimal("12.34")))

    def test_scheduled_iterator_still_yields_every_year(self):
        handle = history_file(
            [
                source_row("202412", "90001", "10"),
                source_row("202509", "90001", "11"),
            ]
        )
        kept = read_rows(handle, {"90001": practice.AreaZip("Los Angeles", Decimal("12.34"))})
        self.assertEqual([row[0] for row in kept], ["2024_12", "2025_09"])

    def test_every_california_zip_is_kept_with_practice_fields(self):
        far = source_row("202412", "96001", "300000")
        far["zip_name"] = "Redding, CA"
        unlocated = source_row("202412", "90090", "1,250.5")
        unlocated["zip_name"] = "Los Angeles, ca"
        unlocated["active_listing_count"] = "2.5"
        blank_name = source_row("202412", "90010", "")
        blank_name["zip_name"] = ""
        other_state = source_row("202412", "10001", "7")
        other_state["zip_name"] = "New York, NY"
        bad_month = source_row("2024-12", "90001", "5")
        bad_zip = source_row("202412", "9001", "5")
        bad_zip["zip_name"] = "Somewhere, CA"
        locations = {
            "96001": practice.AreaZip("", Decimal("478.12")),
            "90010": practice.AreaZip("Los Angeles", Decimal("5.40")),
            "90001": practice.AreaZip("Los Angeles", Decimal("12.34")),
        }
        handle = history_file([far, unlocated, blank_name, other_state, bad_month, bad_zip], extra="unused_column")
        kept = read_rows(handle, locations)
        by_zip = {row[1]: row for row in kept}
        self.assertEqual(sorted(by_zip), ["90010", "90090", "96001"])
        self.assertEqual(by_zip["96001"][-3:], ("90210", None, Decimal("478.12")))
        self.assertEqual(by_zip["90090"][-3:], ("90210", None, None))
        self.assertEqual(by_zip["90090"][3], "1250.5")
        self.assertEqual(by_zip["90090"][6], 3)
        self.assertEqual(by_zip["90010"][-3:], ("90210", "Los Angeles", Decimal("5.40")))
        self.assertIsNone(by_zip["90010"][3])
        self.assertTrue(all(len(row) == len(load_california.LOAD_COLUMNS) for row in kept))

    def test_reader_refuses_a_file_without_the_kept_columns(self):
        handle = io.BytesIO(b"month_date_yyyymm,postal_code\n202412,90001\n")
        with self.assertRaises(SystemExit):
            read_rows(handle, {})


class JoinTests(unittest.TestCase):
    def test_join_uses_the_latest_month_in_the_census_year_only(self):
        census_rows = [
            census_row("90001", 2024, income="50000", age="36.4"),
            census_row("90002", 2024, income="1"),
            census_row("90004", 2024, income=""),
        ]
        redfin_rows = [
            redfin_row("90001", "2024_10", price="100"),
            redfin_row("90001", "2024_12", price=""),
            redfin_row("90001", "2025_12", price="999999"),
            redfin_row("90002", "2024_10", price="200"),
            redfin_row("90003", "2024_12", price="300"),
            redfin_row("90004", "2024_12", price="250000.50"),
            redfin_row("90004", "2026_09", price="1"),
        ]
        year, month, joined = join.join_records(census_rows, redfin_rows)
        self.assertEqual(year, 2024)
        self.assertEqual(month, "2024_12")
        self.assertEqual([row["zip"] for row in joined], ["90001", "90004"])
        by_zip = {row["zip"]: row for row in joined}
        self.assertEqual(by_zip["90001"]["postal_code"], "90001")
        self.assertEqual(by_zip["90001"]["median_household_income"], "50000")
        self.assertEqual(by_zip["90001"]["median_age"], "36.4")
        self.assertEqual(by_zip["90001"]["median_listing_price"], "")
        self.assertNotEqual(by_zip["90001"]["median_listing_price"], "100")
        self.assertNotEqual(by_zip["90001"]["median_listing_price"], "999999")
        self.assertEqual(by_zip["90004"]["median_listing_price"], "250000.50")
        self.assertEqual(by_zip["90004"]["median_household_income"], "")
        self.assertEqual(len(joined), len({row["zip"] for row in joined}))
        self.assertIsNone(join.db_value("median_listing_price", ""))
        self.assertIsNone(join.db_value("median_household_income", ""))
        self.assertNotEqual(join.db_value("median_listing_price", ""), 0)
        self.assertEqual(
            join.db_value("median_listing_price", "250000.50"),
            Decimal("250000.50"),
        )
        self.assertEqual(join.db_value("median_household_income", "50000"), 50000)

    def test_latest_month_ignores_other_years(self):
        rows = [
            {"month_date_yyyymm": "2024_09"},
            {"month_date_yyyymm": "2024_12"},
            {"month_date_yyyymm": "2026_09"},
            {"month_date_yyyymm": "2023_12"},
        ]
        self.assertEqual(join.latest_redfin_month(rows, 2024), "2024_12")
        self.assertEqual(join.latest_redfin_month(rows, 2026), "2026_09")
        with self.assertRaises(SystemExit):
            join.latest_redfin_month(rows, 2022)

    def test_census_file_must_be_one_year(self):
        with self.assertRaises(SystemExit):
            join.loaded_acs_year([census_row("90001", 2023), census_row("90001", 2024)])
        self.assertEqual(join.loaded_acs_year([census_row("90001", 2024)]), 2024)

    def test_join_csv_copies_source_cells(self):
        _year, month, joined = join.join_records(
            [census_row("90001", 2024, income="10", age="40.5")],
            [redfin_row("90001", "2024_08", price="250000.50")],
        )
        self.assertEqual(month, "2024_08")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zip_census_redfin_join.csv"
            join.write_join_csv(joined, path)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["median_listing_price"], "250000.50")
        self.assertEqual(rows[0]["median_age"], "40.5")
        self.assertEqual(rows[0]["month_date_yyyymm"], "2024_08")
        self.assertEqual(rows[0]["zip"], rows[0]["postal_code"])

    def test_print_acs_year_is_the_loaded_vintage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=list(census.COLUMNS), lineterminator="\n"
                )
                writer.writeheader()
                writer.writerow(census_row("90001", 2024, "10"))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = join.main(["--print-acs-year", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "2024")

    def test_join_table_replace_does_not_name_the_redfin_table(self):
        self.assertEqual(join.DELETE_JOIN_SQL, "DELETE FROM public.zip_census_redfin_join")
        self.assertNotIn("redfin_real_estate_value", join.DELETE_JOIN_SQL)
        schema = join.schema_sql()
        self.assertIn("CREATE TABLE IF NOT EXISTS public.zip_census_redfin_join", schema)
        self.assertIn("PRIMARY KEY (zip)", schema)
        self.assertIn("dataset_updated_at timestamptz", schema)
        self.assertNotIn("DELETE", schema.upper())
        self.assertNotIn("redfin_real_estate_value", schema)
        self.assertEqual(join.column_sql_type("median_listing_price"), "numeric")
        self.assertEqual(join.column_sql_type("median_age"), "numeric")
        self.assertEqual(join.column_sql_type("active_listing_count"), "integer")
        self.assertEqual(join.column_sql_type("zip"), "text")
        self.assertEqual(join.column_sql_type("postal_code"), "text")
        for name in census.ADDED_COLUMNS:
            self.assertEqual(join.column_sql_type(name), "integer")
            self.assertIn(f"    {name} integer,", schema)

    def test_race_and_education_carry_through_the_join(self):
        left = census_row("90001", 2024, "10")
        left["hispanic_or_latino_population"] = "120"
        left["education_bachelors"] = "45"
        _year, _month, joined = join.join_records([left], [redfin_row("90001", "2024_08")])
        self.assertEqual(joined[0]["hispanic_or_latino_population"], "120")
        self.assertEqual(joined[0]["education_bachelors"], "45")
        self.assertEqual(joined[0]["education_high_school"], "")
        self.assertEqual(
            list(join.JOIN_COLUMNS[: len(census.COLUMNS)]), list(census.COLUMNS)
        )

    def test_older_census_file_without_the_added_columns_still_joins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "census.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=list(census.BASE_COLUMNS), lineterminator="\n",
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerow(census_row("90001", 2024, "10"))
            rows = join.read_census_csv(path)
        self.assertEqual(rows[0]["education_bachelors"], "")
        _year, _month, joined = join.join_records(rows, [redfin_row("90001", "2024_08")])
        self.assertTrue(all(joined[0][name] == "" for name in census.ADDED_COLUMNS))

    def test_replace_adds_missing_columns_to_an_existing_join_table(self):
        class Cursor:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, _params=None):
                self.statements.append(" ".join(sql.split()))

            def executemany(self, sql, rows):
                self.rows = list(rows)

            def fetchone(self):
                return (len(self.rows),)

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

        _year, _month, joined = join.join_records(
            [census_row("90001", 2024, "10")], [redfin_row("90001", "2024_08")]
        )
        conn = Connection()
        self.assertEqual(join.replace_join_table(conn, joined), 1)
        statements = conn.cursor_obj.statements
        delete_at = statements.index(join.DELETE_JOIN_SQL)
        for name in census.ADDED_COLUMNS:
            alter = (
                f"ALTER TABLE public.zip_census_redfin_join ADD COLUMN IF NOT EXISTS {name} integer"
            )
            self.assertIn(alter, statements)
            self.assertLess(statements.index(alter), delete_at)


class WorkflowTests(unittest.TestCase):
    def test_california_workflow_chains_after_census_success(self):
        text = (ROOT / ".github/workflows/load-california.yml").read_text()
        self.assertIn("workflow_run:", text)
        self.assertIn('workflows: ["Pull Census ZIP population"]', text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("conclusion == 'success'", text)
        self.assertIn("run: python load_california.py\n", text)
        self.assertIn("run: python load_california.py --census-year", text)
        self.assertIn("zip_census_redfin_join.csv", text)
        self.assertIn("zip-census-redfin-join", text)
        self.assertIn("actions/upload-artifact@v4", text)
        self.assertNotIn("pull-procedure-surgeon-fees", text)
        join_at = text.index("python join_census_redfin.py")
        rank_at = text.index("python rank_cash_pay_zips.py zip_census_redfin_join.csv")
        self.assertLess(join_at, rank_at)
        self.assertIn("name: cash-pay-zip-rank", text)
        self.assertLess(rank_at, text.index("name: cash-pay-zip-rank"))
        jobs = text.split("jobs:", 1)[1]
        load_job = jobs.split("  join_rank_full:", 1)[0]
        census_job = jobs.split("  load_census_year:", 1)[1]
        self.assertNotIn("procedure-surgeon-fees", load_job)
        self.assertNotIn("procedure_surgeon_fees.csv", load_job)
        self.assertLess(
            census_job.index("python rank_cash_pay_zips.py zip_census_redfin_join.csv"),
            census_job.index("name: procedure-surgeon-fees"),
        )
        step = census_job[census_job.rindex("- name: Upload procedure surgeon fees CSV") :]
        self.assertIn("uses: actions/upload-artifact@v4", step)
        self.assertLess(
            step.index("uses: actions/upload-artifact@v4"),
            step.index("name: procedure-surgeon-fees"),
        )
        self.assertIn("path: procedure_surgeon_fees.csv", step)
        self.assertIn("retention-days: 90", step)
        self.assertIn("if-no-files-found: error", step)
        self.assertIn("run-id: ${{ github.event.workflow_run.id }}", census_job)

    def test_full_load_joins_and_ranks_from_the_latest_census_artifact(self):
        text = (ROOT / ".github/workflows/load-california.yml").read_text()
        self.assertIn("schedule:", text)
        self.assertIn('cron: "0 16 1 1,7 *"', text)
        self.assertIn("workflow_dispatch:", text)
        jobs = text.split("jobs:", 1)[1]
        load_job, rest = jobs.split("  join_rank_full:", 1)
        full_job, census_job = rest.split("  load_census_year:", 1)
        self.assertIn("github.event_name != 'workflow_run'", load_job)
        self.assertIn("name: redfin-real-estate-value", load_job)
        self.assertNotIn("join_census_redfin.py", load_job)
        self.assertNotIn("rank_cash_pay_zips.py", load_job)
        self.assertIn("needs: load", full_job)
        self.assertIn(
            "github.event_name != 'workflow_run' && needs.load.result == 'success'",
            full_job,
        )
        self.assertNotIn("github.event_name == 'workflow_dispatch'", full_job)
        self.assertNotIn("load_california.py", full_job)
        self.assertNotIn("github.event.workflow_run.id", full_job)
        self.assertIn("name: redfin-real-estate-value", full_job)
        self.assertIn("pull-census-zip-population.yml", full_job)
        self.assertIn("--branch main", full_job)
        self.assertIn("--status success", full_job)
        self.assertIn("--limit 1", full_job)
        self.assertIn("sort_by(.createdAt)", full_job)
        self.assertIn("gh run list", full_job)
        self.assertIn("actions/download-artifact@v4", full_job)
        self.assertIn("name: census-zip-population", full_job)
        self.assertIn("run-id: ${{ steps.census_run.outputs.run_id }}", full_job)
        join_at = full_job.index(
            "python join_census_redfin.py census_artifact/census_zip_population.csv california_zip_inventory.csv"
        )
        rank_at = full_job.index("python rank_cash_pay_zips.py zip_census_redfin_join.csv")
        join_upload_at = full_job.index("name: zip-census-redfin-join")
        rank_upload_at = full_job.index("name: cash-pay-zip-rank")
        fees_upload_at = full_job.index("name: procedure-surgeon-fees")
        self.assertLess(full_job.index("name: redfin-real-estate-value"), join_at)
        self.assertLess(join_at, join_upload_at)
        self.assertLess(join_upload_at, rank_at)
        self.assertLess(rank_at, rank_upload_at)
        self.assertLess(rank_upload_at, fees_upload_at)
        self.assertIn("path: zip_census_redfin_join.csv", full_job)
        self.assertIn("path: cash_pay_zip_rank.csv", full_job)
        self.assertIn("path: procedure_surgeon_fees.csv", full_job)
        fees = full_job[full_job.rindex("- name: Upload procedure surgeon fees CSV") :]
        self.assertLess(fees.index("uses: actions/upload-artifact@v4"), fees.index("name: procedure-surgeon-fees"))
        self.assertIn("path: procedure_surgeon_fees.csv", fees)
        self.assertIn("retention-days: 90", fees)
        self.assertIn("if-no-files-found: error", fees)
        self.assertIn("run-id: ${{ github.event.workflow_run.id }}", census_job)
        self.assertIn('workflows: ["Pull Census ZIP population"]', text)
        self.assertIn("cancel-in-progress: false", text)

    def test_procedure_workflow_is_not_part_of_the_chain(self):
        text = (ROOT / ".github/workflows/pull-procedure-surgeon-fees.yml").read_text()
        self.assertNotIn("workflow_run", text)
        self.assertNotIn("zip_census_redfin_join", text)
        self.assertNotIn("rank_cash_pay_zips.py", text)

    def test_census_safeguards_stay_in_place(self):
        workflow = (ROOT / ".github/workflows/pull-census-zip-population.yml").read_text()
        self.assertNotIn("workflow_run:", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertEqual(census.BATCH_SIZE, 10)
        self.assertEqual(census.CALL_PAUSE_SECONDS, 2)
        self.assertGreaterEqual(census.CONNECT_ATTEMPTS, 2)
        self.assertIn("timeout=2700", inspect.getsource(census.open_census))
        self.assertIn("on_batch", inspect.getsource(census.rows_for_vintage))
        url = census.census_data_url(2024, ("B19013_001E",), ["90001", "90002"])
        self.assertIn("zip%20code%20tabulation%20area:90001,90002", url)
        self.assertNotIn("in=", url)
        self.assertNotIn("state", url)
        self.assertIn('cron: "17 17 * * *"', workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("GitHub cron is UTC and does not follow daylight saving.", workflow)
        self.assertIn("10:17 AM Pacific Daylight Time", workflow)
        self.assertIn("9:17 AM Pacific Standard Time", workflow)
        self.assertNotIn("0 16 1 1,7 *", workflow)
        self.assert_county_batches_stay_small()

    def assert_county_batches_stay_small(self):
        """Orange County gets its own batches, split the same way as Los Angeles County."""
        la = [f"90{index:03d}" for index in range(12)]
        oc = [f"92{index:03d}" for index in range(13)]
        mixed = sorted(la + oc + ["90620", "90630"])
        counties = {**{z: "Los Angeles" for z in la}, **{z: "Orange" for z in oc + ["90620", "90630"]}}
        groups = census.field_groups()
        self.assertGreater(len(groups), 4)
        self.assertTrue(all(0 < len(variables) <= 50 for _name, variables in groups))
        calls: list[tuple[str, ...]] = []

        def fetch_query(year, zips, variables):
            calls.append((tuple(zips), variables))
            return [{census.GEO_COLUMN: z, **{v: "10" for v in variables}} for z in zips]

        ticks = iter(range(10_000))
        with mock.patch.object(census, "fetch_query", fetch_query), mock.patch.object(census.time, "sleep"):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rows = census.rows_for_vintage(
                    2024, mixed, set(mixed), lambda *_: None, counties, clock=lambda: next(ticks)
                )
        self.assertEqual(len(rows), len(mixed))
        batches = list(dict.fromkeys(zips for zips, _variables in calls))
        self.assertEqual([len(b) for b in batches], [10, 2, 10, 5])
        for batch in batches:
            self.assertEqual(len({counties[z] for z in batch}), 1, batch)
        self.assertEqual({counties[z] for z in batches[0] + batches[1]}, {"Los Angeles"})
        self.assertEqual({counties[z] for z in batches[2] + batches[3]}, {"Orange"})
        self.assertEqual(len(calls), len(batches) * len(groups))
        self.assertTrue(all(len(variables) <= 50 for _zips, variables in calls))
        log = out.getvalue()
        self.assertIn("ACS 2024 Los Angeles County: downloading batch 1 of 2, 10 ZIPs", log)
        self.assertIn("ACS 2024 Orange County: downloading batch 2 of 2, 5 ZIPs", log)
        self.assertRegex(log, r"ACS 2024 Orange County: 15 of 15 ZIPs in 2 batches, [\d.]+ s\.")
        self.assertEqual(census.county_pulls(mixed, None), [("California", mixed)])
        with self.assertRaises(SystemExit):
            census.county_pulls(["94102"], counties)
        self.assertIn("rows_for_vintage(vintage, california, area_zips, on_batch, counties)", inspect.getsource(census.main))


if __name__ == "__main__":
    unittest.main()
