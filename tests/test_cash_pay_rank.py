"""Cash-pay persona ranks and procedure marketing bullets."""

from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import rank_cash_pay_zips as rank


def join_row(zip_code: str, **overrides) -> dict[str, str]:
    row = {
        "zip": zip_code,
        "postal_code": zip_code,
        "zip_name": "Los Angeles",
        "median_household_income": "200000",
        "per_capita_income": "80000",
        "median_age": "40",
        "male_population": "400",
        "female_population": "600",
        "adult_population": "1000",
        "population_18_34": "",
        "population_35_44": "",
        "population_45_54": "",
        "population_55_64": "",
        "population_65_plus": "",
        "median_listing_price": "1000000",
        "median_listing_price_per_square_foot": "500",
        "active_listing_count": "20",
        "acs_year": "2024",
        "month_date_yyyymm": "2024_09",
        "date_pulled_yyyymmdd": "2026_10_27",
    }
    row.update({key: "" if value is None else str(value) for key, value in overrides.items()})
    return row


def persona(screens, label: str) -> rank.PersonaRank:
    return next(screen for screen in screens if screen.persona.label == label)


class RankRuleTests(unittest.TestCase):
    def test_cutoffs_are_the_plan_defaults(self):
        self.assertEqual(rank.MIN_ACTIVE_LISTINGS, 10)
        self.assertEqual(rank.TOP_N, 25)
        self.assertEqual(set(rank.PROCEDURE_PERSONAS), set(rank.procedures.PROCEDURES))

    def test_middle_averages_an_even_count(self):
        self.assertEqual(rank.middle([Decimal(100), Decimal(300)]), Decimal(200))
        self.assertEqual(rank.middle([Decimal(100), Decimal(200), Decimal(1000)]), Decimal(200))

    def test_working_age_uses_household_income_and_price_per_square_foot(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90010",
                    zip_name="High female share",
                    median_household_income="300",
                    per_capita_income="500",
                    median_listing_price="10",
                    population_18_34="40",
                    adult_population="100",
                    active_listing_count="10",
                    female_population="950",
                    male_population="50",
                    median_age="18",
                    median_listing_price_per_square_foot="400",
                ),
                join_row(
                    "90030",
                    zip_name="",
                    median_household_income="500",
                    per_capita_income="10",
                    median_listing_price="10",
                    population_18_34="40",
                    adult_population="100",
                    active_listing_count="10",
                    female_population="50",
                    male_population="950",
                    median_age="70",
                    median_listing_price_per_square_foot="400",
                ),
                join_row(
                    "90001",
                    zip_name="Beverly Hills",
                    median_household_income="100",
                    per_capita_income="900",
                    median_listing_price="999999",
                    median_listing_price_per_square_foot="100",
                    population_18_34="90",
                    adult_population="100",
                    active_listing_count="40",
                ),
                join_row(
                    "90002",
                    zip_name="Thin",
                    median_household_income="900",
                    median_listing_price="900",
                    population_18_34="99",
                    adult_population="100",
                    active_listing_count="9",
                ),
                join_row(
                    "90003",
                    zip_name="Blank income",
                    median_household_income="",
                    median_listing_price="900",
                    population_18_34="99",
                    adult_population="100",
                    active_listing_count="40",
                ),
                join_row(
                    "90004",
                    zip_name="No adults",
                    median_household_income="900",
                    median_listing_price="900",
                    population_18_34="0",
                    adult_population="0",
                    active_listing_count="40",
                ),
                join_row(
                    "90006",
                    zip_name="Blank price per square foot",
                    median_household_income="900",
                    median_listing_price="900",
                    median_listing_price_per_square_foot="",
                    population_18_34="99",
                    adult_population="100",
                    active_listing_count="40",
                ),
            ]
        )
        listed = persona(screens, "18 to 34")
        self.assertEqual([row["zip"] for row in listed.rows], ["90030", "90010"])
        self.assertEqual(listed.rows[0]["rank"], 1)
        self.assertEqual(listed.rows[0]["zip_name"], "")
        self.assertEqual(listed.household_income_middle, Decimal(300))
        self.assertEqual(listed.listing_price_middle, Decimal(400))
        self.assertEqual(listed.rows[0]["median_listing_price"], Decimal(10))
        self.assertEqual(listed.rows[0]["median_listing_price_per_square_foot"], Decimal(400))
        self.assertEqual(listed.rows[0]["per_capita_income"], 10)
        self.assertNotIn("90006", [row["zip"] for row in listed.rows])
        self.assertGreater(listed.rows[1]["female_share"], listed.rows[0]["female_share"])
        self.assertIsNone(persona(screens, "35 to 44").household_income_middle)
        self.assertEqual(persona(screens, "35 to 44").rows, [])

    def test_older_persona_uses_price_per_square_foot_and_per_person_income(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90030",
                    zip_name="Expensive",
                    median_household_income="50",
                    per_capita_income="80",
                    median_listing_price="100",
                    median_listing_price_per_square_foot="500",
                    population_55_64="20",
                    population_65_plus="20",
                    adult_population="100",
                    active_listing_count="10",
                ),
                join_row(
                    "90010",
                    zip_name="High household",
                    median_household_income="500",
                    per_capita_income="80",
                    median_listing_price="900",
                    median_listing_price_per_square_foot="300",
                    population_55_64="20",
                    population_65_plus="20",
                    adult_population="100",
                    active_listing_count="10",
                ),
                join_row(
                    "90001",
                    zip_name="Low per person",
                    median_household_income="900",
                    per_capita_income="10",
                    median_listing_price="100",
                    median_listing_price_per_square_foot="100",
                    population_55_64="40",
                    population_65_plus="40",
                    adult_population="100",
                    active_listing_count="20",
                ),
                join_row(
                    "90002",
                    zip_name="Missing older band",
                    median_household_income="900",
                    per_capita_income="900",
                    median_listing_price="900",
                    population_55_64="50",
                    population_65_plus="",
                    population_45_54="40",
                    adult_population="100",
                    active_listing_count="20",
                ),
            ]
        )
        older = persona(screens, "55 and older")
        self.assertEqual([row["zip"] for row in older.rows], ["90030", "90010"])
        self.assertEqual(older.rows[0]["age_share"], Decimal("0.4"))
        self.assertEqual(older.rows[0]["median_household_income"], 50)
        self.assertEqual(older.rows[0]["median_listing_price"], Decimal(100))
        self.assertEqual(older.rows[0]["median_listing_price_per_square_foot"], Decimal(500))
        self.assertEqual(older.listing_price_middle, Decimal(300))
        self.assertEqual(older.per_capita_income_middle, Decimal(80))
        self.assertEqual([row["zip"] for row in persona(screens, "45 to 54").rows], ["90002"])
        self.assertEqual(persona(screens, "18 to 34").rows, [])

    def test_blank_home_price_still_ranks_on_price_per_square_foot(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90001",
                    median_listing_price="",
                    median_listing_price_per_square_foot="400",
                    median_household_income="300",
                    population_18_34="40",
                    adult_population="100",
                    active_listing_count="10",
                )
            ]
        )
        listed = persona(screens, "18 to 34")
        self.assertEqual([row["zip"] for row in listed.rows], ["90001"])
        self.assertIsNone(listed.rows[0]["median_listing_price"])
        self.assertEqual(listed.rows[0]["median_listing_price_per_square_foot"], Decimal(400))

    def test_middles_are_recalculated_for_each_persona(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90001",
                    median_household_income="100",
                    median_listing_price="100",
                    population_18_34="10",
                    population_35_44="10",
                    adult_population="100",
                ),
                join_row(
                    "90002",
                    zip_name="Both lists",
                    median_household_income="300",
                    median_listing_price="300",
                    population_18_34="10",
                    population_35_44="10",
                    adult_population="100",
                ),
                join_row(
                    "90003",
                    zip_name="Only thirties",
                    median_household_income="1000",
                    median_listing_price="1000",
                    population_35_44="10",
                    adult_population="100",
                ),
            ]
        )
        younger = persona(screens, "18 to 34")
        thirties = persona(screens, "35 to 44")
        self.assertEqual(younger.household_income_middle, Decimal(200))
        self.assertEqual(thirties.household_income_middle, Decimal(300))
        self.assertEqual([row["zip"] for row in younger.rows], ["90002"])
        self.assertEqual([row["zip"] for row in thirties.rows], ["90003", "90002"])

    def test_top_25_keeps_the_highest_age_shares(self):
        rows = [
            join_row(
                f"{90000 + index}",
                median_household_income="100",
                median_listing_price="100",
                population_18_34=str(index),
                adult_population="100",
                active_listing_count="10",
            )
            for index in range(1, 31)
        ]
        listed = persona(rank.rank_personas(rows), "18 to 34")
        self.assertEqual(len(listed.rows), 25)
        self.assertEqual(listed.rows[0]["zip"], "90030")
        self.assertEqual(listed.rows[0]["rank"], 1)
        self.assertEqual(listed.rows[-1]["zip"], "90006")
        self.assertNotIn("90001", {row["zip"] for row in listed.rows})

    def test_duplicate_zip_is_rejected(self):
        with self.assertRaises(SystemExit):
            rank.rank_personas(
                [
                    join_row("90001", population_18_34="10"),
                    join_row("90001", population_18_34="20"),
                ]
            )


class BulletTests(unittest.TestCase):
    def setUp(self):
        self.screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="Pasadena",
                    median_household_income="300",
                    per_capita_income="20",
                    median_listing_price="300",
                    median_listing_price_per_square_foot="640",
                    population_18_34="40",
                    population_35_44="25",
                    adult_population="100",
                    active_listing_count="10",
                    female_population="750",
                    male_population="250",
                ),
                join_row(
                    "90011",
                    zip_name="Compton",
                    median_household_income="100",
                    per_capita_income="90",
                    median_listing_price="100",
                    median_listing_price_per_square_foot="50",
                    population_18_34="80",
                    population_35_44="10",
                    adult_population="100",
                    active_listing_count="30",
                    female_population="500",
                    male_population="500",
                ),
                join_row(
                    "90040",
                    zip_name="Older Hills",
                    median_household_income="50",
                    per_capita_income="200",
                    median_listing_price="400",
                    median_listing_price_per_square_foot="800",
                    population_45_54="30",
                    population_55_64="30",
                    population_65_plus="30",
                    adult_population="100",
                    active_listing_count="12",
                    female_population="800",
                    male_population="200",
                ),
            ]
        )

    def test_bullets_follow_the_procedure_persona_lists(self):
        young = rank.marketing_bullets("breast augmentation", self.screens)
        same_young = rank.marketing_bullets("rhinoplasty", self.screens)
        body = rank.marketing_bullets("buttock augmentation with fat grafting", self.screens)
        older = rank.marketing_bullets("facelift", self.screens)
        same_older = rank.marketing_bullets("eyelid surgery", self.screens)
        lift = rank.marketing_bullets("breast lift", self.screens)
        self.assertEqual(young, same_young)
        self.assertEqual(young, body)
        self.assertEqual(older, same_older)
        self.assertEqual(older, rank.marketing_bullets("forehead lift", self.screens))
        self.assertEqual(older, rank.marketing_bullets("neck lift", self.screens))
        self.assertEqual(
            rank.marketing_bullets("arm lift", self.screens),
            rank.marketing_bullets("thigh lift", self.screens),
        )
        self.assertEqual(lift, rank.marketing_bullets("tummy tuck", self.screens))
        self.assertEqual(
            rank.marketing_bullets("liposuction", self.screens),
            rank.marketing_bullets("breast reduction", self.screens),
        )
        self.assertIn("Pasadena (90012)", young)
        self.assertIn("18 to 34", young)
        self.assertIn("35 to 44", young)
        self.assertNotIn("55 and older", young)
        self.assertNotIn("Older Hills", young)
        self.assertNotIn("Compton", young)
        self.assertIn("Household income", young)
        self.assertIn("listing price per square foot", young)
        self.assertIn("$640", young)
        self.assertIn("at or above", young)
        self.assertIn("Women are 75.0% of residents.", young)
        self.assertNotIn("per-person income", young)
        self.assertIn("Older Hills (90040)", older)
        self.assertIn("45 to 54", older)
        self.assertIn("55 and older", older)
        self.assertNotIn("18 to 34", older)
        self.assertNotIn("Pasadena", older)
        self.assertIn("per-person income", older)
        self.assertIn("listing price per square foot", older)
        self.assertIn("$800", older)
        self.assertTrue(older.splitlines()[1].startswith("- 1. "))

    def test_shared_persona_lists_repeat_the_same_text(self):
        names = ["breast augmentation", "rhinoplasty", "facelift", "eyelid surgery"]
        bullets = rank.bullets_by_procedure(names, self.screens)
        self.assertEqual(bullets["breast augmentation"], bullets["rhinoplasty"])
        self.assertEqual(bullets["facelift"], bullets["eyelid surgery"])
        self.assertNotEqual(bullets["breast augmentation"], bullets["facelift"])


class BulletYearTests(unittest.TestCase):
    def test_join_acs_year_is_the_preferred_report_year(self):
        self.assertEqual(
            rank.join_acs_year([join_row("90001"), join_row("90002", acs_year="2024")]),
            2024,
        )
        with self.assertRaises(SystemExit):
            rank.join_acs_year(
                [join_row("90001", acs_year="2024"), join_row("90002", acs_year="2025")]
            )
        with self.assertRaises(SystemExit):
            rank.join_acs_year([join_row("90001", acs_year="")])

    def test_half_of_the_rows_does_not_qualify(self):
        self.assertFalse(rank.report_year_qualifies([(1, 2), (None, None)]))
        self.assertFalse(rank.report_year_qualifies([(1, None), (None, 2)]))
        self.assertFalse(rank.report_year_qualifies([]))
        self.assertTrue(rank.report_year_qualifies([(1, 2), (3, 4), (None, None)]))

    def test_join_year_with_fees_is_used_when_a_later_year_is_blank(self):
        rows = [
            (2024, "facelift", 8000, 12000),
            (2024, "rhinoplasty", 7000, 11000),
            (2025, "facelift", None, None),
            (2025, "rhinoplasty", None, None),
        ]
        self.assertEqual(rank.select_bullet_report_year(2024, rows), 2024)

    def test_blank_join_year_uses_the_newest_year_that_qualifies(self):
        rows = [
            (2023, "facelift", 1000, 2000),
            (2023, "rhinoplasty", 1000, 2000),
            (2024, "facelift", 1000, 2000),
            (2024, "rhinoplasty", None, None),
            (2025, "facelift", None, None),
            (2025, "rhinoplasty", None, 9000),
        ]
        self.assertEqual(rank.select_bullet_report_year(2025, rows), 2023)

    def test_qualifying_join_year_wins_over_an_older_fee_year(self):
        rows = [
            (2023, "facelift", 1000, 2000),
            (2024, "facelift", 3000, 4000),
            (2024, "rhinoplasty", 5000, 6000),
        ]
        self.assertEqual(rank.select_bullet_report_year(2024, rows), 2024)

    def test_missing_join_year_falls_back_to_the_newest_fee_year(self):
        rows = [
            (2024, "facelift", 1000, 2000),
            (2024, "rhinoplasty", 1000, 2000),
        ]
        self.assertEqual(rank.select_bullet_report_year(2026, rows), 2024)

    def test_no_qualifying_year_is_refused(self):
        rows = [
            (2024, "facelift", None, None),
            (2025, "facelift", 1000, None),
        ]
        with self.assertRaises(SystemExit) as caught:
            rank.select_bullet_report_year(2025, rows)
        self.assertIn("surgeon_fee_low", str(caught.exception))

    def test_blank_join_year_writes_bullets_only_on_the_newest_fee_year(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="Pasadena",
                    acs_year="2025",
                    median_household_income="300",
                    median_listing_price="300",
                    population_18_34="40",
                    adult_population="100",
                )
            ]
        )
        procedure_rows = [
            (2024, "rhinoplasty", 7000, 12000),
            (2025, "rhinoplasty", None, None),
            (2025, "facelift", None, None),
        ]

        class Cursor:
            def __init__(self):
                self.statements: list[tuple[str, object]] = []

            def execute(self, sql, params=None):
                self.statements.append((" ".join(sql.split()), params))

            def executemany(self, sql, batch):
                self.statements.append((" ".join(sql.split()), list(batch)))

        cursor = Cursor()
        year, count = rank.apply_marketing_bullets(cursor, screens, procedure_rows, 2025)
        self.assertEqual(year, 2024)
        self.assertEqual(count, 1)
        clear = next(
            params for sql, params in cursor.statements if "marketing_bullets = NULL" in sql
        )
        self.assertEqual(clear, (2024,))
        updates = next(
            batch
            for _sql, batch in cursor.statements
            if isinstance(batch, list) and batch
        )
        self.assertEqual(updates, [(updates[0][0], 2024, "rhinoplasty")])
        self.assertIn("Pasadena (90012)", updates[0][0])
        for sql, _params in cursor.statements:
            self.assertNotIn("surgeon_fee_low", sql)
            self.assertNotIn("surgeon_fee_high", sql)


class SaveTests(unittest.TestCase):
    def test_save_replaces_the_rank_and_writes_the_join_year_when_it_has_fees(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="Pasadena",
                    median_household_income="300",
                    median_listing_price="300",
                    population_18_34="40",
                    adult_population="100",
                )
            ]
        )
        moment = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)

        class Cursor:
            def __init__(self):
                self.statements: list[tuple[str, object]] = []
                self._fetchone = None
                self._fetchall = None
                self.count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                self.statements.append((text, params))
                if "information_schema.tables" in text:
                    self._fetchone = (1,)
                    self._fetchall = None
                elif "SELECT report_year, procedure" in text:
                    self._fetchone = None
                    self._fetchall = [
                        (2024, "custom procedure", 1200, 2200),
                        (2024, "facelift", 8000, 15000),
                        (2024, "rhinoplasty", 7000, 12000),
                        (2025, "custom procedure", None, None),
                        (2025, "facelift", None, None),
                        (2025, "rhinoplasty", None, None),
                    ]
                elif "SELECT COUNT(*)" in text:
                    self._fetchone = (self.count,)
                else:
                    self._fetchone = None

            def executemany(self, sql, rows):
                text = " ".join(sql.split())
                stored = list(rows)
                self.statements.append((text, stored))
                if "INSERT INTO" in text:
                    self.count = len(stored)

            def fetchone(self):
                return self._fetchone

            def fetchall(self):
                return list(self._fetchall or [])

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

        conn = Connection()
        count, year, bullet_count = rank.save_rank(
            conn, screens, moment, preferred_year=2024
        )
        self.assertEqual(count, 1)
        self.assertEqual(year, 2024)
        self.assertEqual(bullet_count, 3)
        statements = conn.cursor_obj.statements
        joined = " ".join(sql for sql, _params in statements)
        self.assertIn("DELETE FROM public.cash_pay_zip_rank", joined)
        self.assertNotIn("DROP TABLE", joined.upper())
        self.assertNotIn("DROP COLUMN", joined.upper())
        self.assertNotIn("nfhl", joined.lower())
        self.assertNotIn("nri", joined.lower())
        self.assertNotIn("redfin_real_estate_value", joined)
        self.assertIn("ADD COLUMN IF NOT EXISTS marketing_bullets text", joined)
        clear = next(params for sql, params in statements if "marketing_bullets = NULL" in sql)
        self.assertEqual(clear, (2024,))
        updates = next(
            rows
            for sql, rows in statements
            if isinstance(rows, list) and rows and "marketing_bullets = %s" in sql
        )
        by_name = {procedure: text for text, report_year, procedure in updates}
        self.assertEqual(set(by_name), {"facelift", "rhinoplasty", "custom procedure"})
        self.assertTrue(all(report_year == 2024 for _text, report_year, _name in updates))
        self.assertIn("Pasadena (90012)", by_name["rhinoplasty"])
        self.assertNotIn("Pasadena", by_name["facelift"])
        self.assertIn("no cash-pay persona list", by_name["custom procedure"])
        insert = next(rows for sql, rows in statements if "INSERT INTO public.cash_pay_zip_rank" in sql)
        self.assertIs(insert[0][-1], moment)
        update_sql = next(sql for sql, rows in statements if isinstance(rows, list) and "marketing_bullets = %s" in sql)
        self.assertNotIn("dataset_updated_at", update_sql)
        self.assertNotIn("surgeon_fee_low", update_sql)
        self.assertNotIn("surgeon_fee_high", update_sql)
        fee_select = next(sql for sql, _params in statements if "SELECT report_year, procedure" in sql)
        self.assertIn("surgeon_fee_low", fee_select)
        self.assertIn("surgeon_fee_high", fee_select)
        for sql, _params in statements:
            if sql.upper().startswith("UPDATE"):
                self.assertNotIn("surgeon_fee_low", sql)
                self.assertNotIn("surgeon_fee_high", sql)

    def test_schema_adds_the_rank_table_without_dropping(self):
        schema = rank.schema_sql()
        self.assertIn("CREATE TABLE IF NOT EXISTS public.cash_pay_zip_rank", schema)
        self.assertIn("PRIMARY KEY (persona, zip)", schema)
        self.assertIn("dataset_updated_at timestamptz", schema)
        self.assertNotIn("DROP", schema.upper())
        self.assertEqual(rank.DELETE_RANK_SQL, "DELETE FROM public.cash_pay_zip_rank")


class MainTests(unittest.TestCase):
    def test_csv_is_written_without_a_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "zip_census_redfin_join.csv"
            output = Path(tmp) / "cash_pay_zip_rank.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rank.JOIN_FIELDS), lineterminator="\n")
                writer.writeheader()
                writer.writerow(
                    join_row(
                        "90012",
                        zip_name="Pasadena",
                        population_18_34="40",
                        adult_population="100",
                    )
                )
            previous = os.environ.pop(rank.DATABASE_URL_ENV, None)
            rank.OUTPUT_CSV = output
            stdout = io.StringIO()
            try:
                with redirect_stdout(stdout):
                    code = rank.main([str(source)])
            finally:
                rank.OUTPUT_CSV = Path(__file__).resolve().parents[1] / rank.OUTPUT_CSV_NAME
                if previous is None:
                    os.environ.pop(rank.DATABASE_URL_ENV, None)
                else:
                    os.environ[rank.DATABASE_URL_ENV] = previous
            self.assertEqual(code, 0)
            self.assertIn("Left public.cash_pay_zip_rank", stdout.getvalue())
            self.assertIn("procedure_surgeon_fees unchanged", stdout.getvalue())
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["zip"], "90012")
        self.assertEqual(rows[0]["persona"], "18 to 34")
        self.assertEqual(rows[0]["rank"], "1")
        self.assertEqual(rows[0]["zip_name"], "Pasadena")

    def test_main_exports_fees_after_the_rank_is_saved(self):
        calls = []

        seen = {}

        def save_rank(conn, screens, updated_at=None, preferred_year=None):
            calls.append("save")
            seen["year"] = preferred_year
            return (1, preferred_year, 2)

        def export_procedure_fees(conn, path=None):
            calls.append("export")
            return 9

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "zip_census_redfin_join.csv"
            output = Path(tmp) / "cash_pay_zip_rank.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=list(rank.JOIN_FIELDS), lineterminator="\n"
                )
                writer.writeheader()
                writer.writerow(
                    join_row(
                        "90012",
                        zip_name="Pasadena",
                        population_18_34="40",
                        adult_population="100",
                    )
                )
            previous = os.environ.get(rank.DATABASE_URL_ENV)
            os.environ[rank.DATABASE_URL_ENV] = "postgres://example.test/db"
            rank.OUTPUT_CSV = output
            stdout = io.StringIO()
            try:
                with (
                    patch.object(rank.load_california, "connect", lambda: Connection()),
                    patch.object(rank, "save_rank", save_rank),
                    patch.object(rank, "export_procedure_fees", export_procedure_fees),
                    redirect_stdout(stdout),
                ):
                    code = rank.main([str(source)])
            finally:
                rank.OUTPUT_CSV = Path(__file__).resolve().parents[1] / rank.OUTPUT_CSV_NAME
                if previous is None:
                    os.environ.pop(rank.DATABASE_URL_ENV, None)
                else:
                    os.environ[rank.DATABASE_URL_ENV] = previous
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["save", "export"])
        self.assertEqual(seen["year"], 2024)
        self.assertIn("Wrote 9 rows to procedure_surgeon_fees.csv.", stdout.getvalue())


class ProcedureFeeExportTests(unittest.TestCase):
    def _connection(self, columns, table_rows):
        class Cursor:
            def __init__(self):
                self.statements = []
                self._fetchall = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                text = " ".join(sql.split())
                self.statements.append((text, params))
                if "information_schema.columns" in text:
                    self._fetchall = [(name,) for name in columns]
                elif text.startswith("SELECT"):
                    self._fetchall = list(table_rows)
                else:
                    self._fetchall = []

            def fetchall(self):
                return list(self._fetchall)

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

        return Connection()

    def test_export_includes_every_column_and_both_report_years(self):
        columns = [
            "report_year",
            "procedure",
            "procedure_count",
            "surgeon_fee_low",
            "surgeon_fee_high",
            "fee_basis",
            "source_url",
            "dataset_updated_at",
            "marketing_bullets",
            "source_note",
        ]
        moment = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
        bullet = "18 to 34. Ranked.\n- 1. Pasadena (90012): cash."
        table_rows = [
            (2024, "facelift", 10, 1000, 2000, "national", "https://example.test/2024", moment, None, "older"),
            (2024, "rhinoplasty", 11, 1100, 2100, "national", "https://example.test/2024", moment, None, "older"),
            (2025, "facelift", 12, None, None, "national", "https://example.test/2025", moment, "older personas", "latest"),
            (2025, "rhinoplasty", 13, None, None, "national", "https://example.test/2025", moment, bullet, "latest"),
        ]
        conn = self._connection(columns, table_rows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "procedure_surgeon_fees.csv"
            count = rank.export_procedure_fees(conn, path)
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(list(reader.fieldnames), columns)
                written = list(reader)
        self.assertEqual(count, 4)
        self.assertEqual(
            [row["report_year"] for row in written],
            ["2024", "2024", "2025", "2025"],
        )
        self.assertEqual(
            [row["procedure"] for row in written],
            ["facelift", "rhinoplasty", "facelift", "rhinoplasty"],
        )
        self.assertEqual(written[0]["marketing_bullets"], "")
        self.assertEqual(written[1]["marketing_bullets"], "")
        self.assertEqual(written[2]["marketing_bullets"], "older personas")
        self.assertEqual(written[3]["marketing_bullets"], bullet)
        self.assertEqual(written[0]["source_note"], "older")
        self.assertEqual(written[3]["source_note"], "latest")
        self.assertEqual(written[0]["dataset_updated_at"], moment.isoformat())
        self.assertEqual(written[2]["surgeon_fee_low"], "")
        self.assertEqual(written[0]["surgeon_fee_low"], "1000")
        sqls = [sql for sql, _params in conn.cursor_obj.statements]
        joined = "\n".join(sqls)
        self.assertNotIn("DROP", joined.upper())
        self.assertNotIn("DELETE", joined.upper())
        self.assertIn("SET TRANSACTION READ ONLY", joined)
        select = next(sql for sql in sqls if "FROM public.procedure_surgeon_fees" in sql)
        self.assertTrue(select.startswith("SELECT "))
        self.assertNotIn("WHERE", select.upper())
        self.assertIn("marketing_bullets", select)
        self.assertIn("source_note", select)
        self.assertIn("dataset_updated_at", select)
        self.assertIn("ORDER BY report_year, procedure", select)
        columns_sql = next(sql for sql in sqls if "information_schema.columns" in sql)
        self.assertIn("ORDER BY ordinal_position", columns_sql)
        self.assertEqual(columns_sql.count("procedure_surgeon_fees"), 0)
        self.assertEqual(
            next(params for sql, params in conn.cursor_obj.statements if params),
            ("procedure_surgeon_fees",),
        )

    def test_export_refuses_a_table_without_marketing_bullets(self):
        conn = self._connection(["report_year", "procedure"], [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "procedure_surgeon_fees.csv"
            with self.assertRaises(SystemExit) as caught:
                rank.export_procedure_fees(conn, path)
            self.assertIn("marketing_bullets", str(caught.exception))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
