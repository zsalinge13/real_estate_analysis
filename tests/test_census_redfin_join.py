"""The chained Redfin load keeps one census year, and the join uses its latest month."""

from __future__ import annotations

import csv
import inspect
import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

import join_census_redfin as join
import load_california
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
    return row


def source_row(month_yyyymm: str, postal: str, price: str) -> dict[str, str]:
    row = {column: "" for column in load_california.KEPT_COLUMNS}
    row["month_date_yyyymm"] = month_yyyymm
    row["postal_code"] = postal
    row["zip_name"] = "Los Angeles"
    row["median_listing_price"] = price
    row["active_listing_count"] = "4"
    return row


def history_reader(rows: list[dict[str, str]]) -> csv.DictReader:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(load_california.KEPT_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return csv.DictReader(buffer)


class CensusYearLoadTests(unittest.TestCase):
    def test_default_run_is_still_the_full_replace(self):
        self.assertIsNone(load_california.census_year_arg(["load_california.py"]))
        source = inspect.getsource(load_california.load_history)
        self.assertIn("if census_year is None:", source)
        self.assertIn("return replace_table(conn)", source)
        self.assertIn("return upsert_census_year(conn, census_year)", source)
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
        reader = history_reader(
            [
                source_row("202412", "90001", "10"),
                source_row("202501", "90001", "99"),
                source_row("202309", "90001", "80"),
                source_row("202401", "90002", "8"),
                source_row("202412", "10001", "7"),
            ]
        )
        kept = list(
            load_california.iter_los_angeles_rows(
                reader, "2026_10_27", {"90001", "90002"}, 2024
            )
        )
        self.assertEqual([row[0] for row in kept], ["2024_12", "2024_01"])
        self.assertEqual([row[1] for row in kept], ["90001", "90002"])
        self.assertEqual(kept[0][3], Decimal("10"))

    def test_scheduled_iterator_still_yields_every_year(self):
        reader = history_reader(
            [
                source_row("202412", "90001", "10"),
                source_row("202509", "90001", "11"),
            ]
        )
        kept = list(
            load_california.iter_los_angeles_rows(
                reader, "2026_10_27", {"90001"}
            )
        )
        self.assertEqual([row[0] for row in kept], ["2024_12", "2025_09"])


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

    def test_procedure_workflow_is_not_part_of_the_chain(self):
        text = (ROOT / ".github/workflows/pull-procedure-surgeon-fees.yml").read_text()
        self.assertNotIn("workflow_run", text)
        self.assertNotIn("zip_census_redfin_join", text)

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
        self.assertIn('cron: "0 17 * * *"', workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("GitHub cron is UTC and does not follow daylight saving.", workflow)
        self.assertIn("10:00 AM Pacific Daylight Time", workflow)
        self.assertIn("9:00 AM Pacific Standard Time", workflow)
        self.assertNotIn("0 16 1 1,7 *", workflow)


if __name__ == "__main__":
    unittest.main()
