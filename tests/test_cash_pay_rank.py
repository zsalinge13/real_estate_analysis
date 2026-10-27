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
        "median_listing_price_per_square_foot": "",
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

    def test_working_age_uses_household_income_and_listing_price(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90010",
                    zip_name="High female share",
                    median_household_income="300",
                    per_capita_income="500",
                    median_listing_price="400",
                    population_18_34="40",
                    adult_population="100",
                    active_listing_count="10",
                    female_population="950",
                    male_population="50",
                    median_age="18",
                    median_listing_price_per_square_foot="999",
                ),
                join_row(
                    "90030",
                    zip_name="",
                    median_household_income="500",
                    per_capita_income="10",
                    median_listing_price="400",
                    population_18_34="40",
                    adult_population="100",
                    active_listing_count="10",
                    female_population="50",
                    male_population="950",
                    median_age="70",
                    median_listing_price_per_square_foot="100",
                ),
                join_row(
                    "90001",
                    zip_name="Beverly Hills",
                    median_household_income="100",
                    per_capita_income="900",
                    median_listing_price="100",
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
            ]
        )
        listed = persona(screens, "18 to 34")
        self.assertEqual([row["zip"] for row in listed.rows], ["90030", "90010"])
        self.assertEqual(listed.rows[0]["rank"], 1)
        self.assertEqual(listed.rows[0]["zip_name"], "")
        self.assertEqual(listed.household_income_middle, Decimal(300))
        self.assertEqual(listed.listing_price_middle, Decimal(400))
        self.assertEqual(listed.rows[0]["per_capita_income"], 10)
        self.assertGreater(listed.rows[1]["female_share"], listed.rows[0]["female_share"])
        self.assertIsNone(persona(screens, "35 to 44").household_income_middle)
        self.assertEqual(persona(screens, "35 to 44").rows, [])

    def test_older_persona_uses_listing_price_and_per_person_income(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90030",
                    zip_name="Expensive",
                    median_household_income="50",
                    per_capita_income="80",
                    median_listing_price="500",
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
                    median_listing_price="300",
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
        self.assertEqual(older.listing_price_middle, Decimal(300))
        self.assertEqual(older.per_capita_income_middle, Decimal(80))
        self.assertEqual([row["zip"] for row in persona(screens, "45 to 54").rows], ["90002"])
        self.assertEqual(persona(screens, "18 to 34").rows, [])

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
        self.assertIn("listing price", young)
        self.assertIn("at or above", young)
        self.assertIn("Women are 75.0% of residents.", young)
        self.assertNotIn("per-person income", young)
        self.assertIn("Older Hills (90040)", older)
        self.assertIn("45 to 54", older)
        self.assertIn("55 and older", older)
        self.assertNotIn("18 to 34", older)
        self.assertNotIn("Pasadena", older)
        self.assertIn("per-person income", older)
        self.assertIn("listing price", older)
        self.assertTrue(older.splitlines()[1].startswith("- 1. "))

    def test_shared_persona_lists_repeat_the_same_text(self):
        names = ["breast augmentation", "rhinoplasty", "facelift", "eyelid surgery"]
        bullets = rank.bullets_by_procedure(names, self.screens)
        self.assertEqual(bullets["breast augmentation"], bullets["rhinoplasty"])
        self.assertEqual(bullets["facelift"], bullets["eyelid surgery"])
        self.assertNotEqual(bullets["breast augmentation"], bullets["facelift"])


class SaveTests(unittest.TestCase):
    def test_save_replaces_the_rank_and_writes_the_latest_year_only(self):
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
                        (2024, "facelift"),
                        (2024, "rhinoplasty"),
                        (2025, "facelift"),
                        (2025, "rhinoplasty"),
                        (2025, "custom procedure"),
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
        count, year, bullet_count = rank.save_rank(conn, screens, moment)
        self.assertEqual(count, 1)
        self.assertEqual(year, 2025)
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
        self.assertEqual(clear, (2025,))
        updates = next(
            rows
            for sql, rows in statements
            if isinstance(rows, list) and rows and "marketing_bullets = %s" in sql
        )
        by_name = {procedure: text for text, report_year, procedure in updates}
        self.assertEqual(set(by_name), {"facelift", "rhinoplasty", "custom procedure"})
        self.assertTrue(all(report_year == 2025 for _text, report_year, _name in updates))
        self.assertIn("Pasadena (90012)", by_name["rhinoplasty"])
        self.assertNotIn("Pasadena", by_name["facelift"])
        self.assertIn("no cash-pay persona list", by_name["custom procedure"])
        insert = next(rows for sql, rows in statements if "INSERT INTO public.cash_pay_zip_rank" in sql)
        self.assertIs(insert[0][-1], moment)
        update_sql = next(sql for sql, rows in statements if isinstance(rows, list) and "marketing_bullets = %s" in sql)
        self.assertNotIn("dataset_updated_at", update_sql)

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


if __name__ == "__main__":
    unittest.main()
