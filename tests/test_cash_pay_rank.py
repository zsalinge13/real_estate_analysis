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

import discretionary
import pull_census_zip_population as census
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
        "population_25_44": "",
        "population_45_64": "",
        "population_65_plus": "",
        "median_listing_price": "1000000",
        "median_listing_price_per_square_foot": "500",
        "active_listing_count": "20",
        "acs_year": "2024",
        "month_date_yyyymm": "2024_09",
        "date_pulled_yyyymmdd": "2026_10_27",
        "practice_zip_code": "90210",
        "county": "Los Angeles",
        "distance_from_practice_miles": "8.50",
        "median_gross_rent": "2000",
        "median_owner_costs": "",
        "average_household_size": "2.9",
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
                    population_25_44="40",
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
                    population_25_44="40",
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
                    population_25_44="90",
                    adult_population="100",
                    active_listing_count="40",
                ),
                join_row(
                    "90002",
                    zip_name="Thin",
                    median_household_income="900",
                    median_listing_price="900",
                    population_25_44="99",
                    adult_population="100",
                    active_listing_count="9",
                ),
                join_row(
                    "90003",
                    zip_name="Blank income",
                    median_household_income="",
                    median_listing_price="900",
                    population_25_44="99",
                    adult_population="100",
                    active_listing_count="40",
                ),
                join_row(
                    "90004",
                    zip_name="No adults",
                    median_household_income="900",
                    median_listing_price="900",
                    population_25_44="0",
                    adult_population="0",
                    active_listing_count="40",
                ),
                join_row(
                    "90006",
                    zip_name="Blank price per square foot",
                    median_household_income="900",
                    median_listing_price="900",
                    median_listing_price_per_square_foot="",
                    population_25_44="99",
                    adult_population="100",
                    active_listing_count="40",
                ),
            ]
        )
        listed = persona(screens, "25–44")
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
        self.assertIsNone(persona(screens, "45–64").household_income_middle)
        self.assertEqual(persona(screens, "45–64").rows, [])

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
                    population_65_plus="40",
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
                    population_65_plus="40",
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
                    population_65_plus="80",
                    adult_population="100",
                    active_listing_count="20",
                ),
                join_row(
                    "90002",
                    zip_name="Missing older band",
                    median_household_income="900",
                    per_capita_income="900",
                    median_listing_price="900",
                    population_65_plus="",
                    population_45_64="40",
                    adult_population="100",
                    active_listing_count="20",
                ),
            ]
        )
        older = persona(screens, "65+")
        self.assertEqual([row["zip"] for row in older.rows], ["90030", "90010"])
        self.assertEqual(older.rows[0]["age_share"], Decimal("0.4"))
        self.assertEqual(older.rows[0]["median_household_income"], 50)
        self.assertEqual(older.rows[0]["median_listing_price"], Decimal(100))
        self.assertEqual(older.rows[0]["median_listing_price_per_square_foot"], Decimal(500))
        self.assertEqual(older.listing_price_middle, Decimal(300))
        self.assertEqual(older.per_capita_income_middle, Decimal(80))
        self.assertEqual([row["zip"] for row in persona(screens, "45–64").rows], ["90002"])
        self.assertEqual(persona(screens, "25–44").rows, [])

    def test_blank_home_price_still_ranks_on_price_per_square_foot(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90001",
                    median_listing_price="",
                    median_listing_price_per_square_foot="400",
                    median_household_income="300",
                    population_25_44="40",
                    adult_population="100",
                    active_listing_count="10",
                )
            ]
        )
        listed = persona(screens, "25–44")
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
                    population_25_44="10",
                    population_45_64="10",
                    adult_population="100",
                ),
                join_row(
                    "90002",
                    zip_name="Both lists",
                    median_household_income="300",
                    median_listing_price="300",
                    population_25_44="10",
                    population_45_64="10",
                    adult_population="100",
                ),
                join_row(
                    "90003",
                    zip_name="Only thirties",
                    median_household_income="1000",
                    median_listing_price="1000",
                    population_45_64="10",
                    adult_population="100",
                ),
            ]
        )
        younger = persona(screens, "25–44")
        thirties = persona(screens, "45–64")
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
                population_25_44=str(index),
                adult_population="100",
                active_listing_count="10",
            )
            for index in range(1, 31)
        ]
        listed = persona(rank.rank_personas(rows), "25–44")
        self.assertEqual(len(listed.rows), 25)
        self.assertEqual(listed.rows[0]["zip"], "90030")
        self.assertEqual(listed.rows[0]["rank"], 1)
        self.assertEqual(listed.rows[-1]["zip"], "90006")
        self.assertNotIn("90001", {row["zip"] for row in listed.rows})

    def test_duplicate_zip_is_rejected(self):
        with self.assertRaises(SystemExit):
            rank.rank_personas(
                [
                    join_row("90001", population_25_44="10"),
                    join_row("90001", population_25_44="20"),
                ]
            )


class BulletTests(unittest.TestCase):
    def setUp(self):
        self.screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="pasadena, ca",
                    median_household_income="300",
                    per_capita_income="20",
                    median_listing_price="300",
                    median_listing_price_per_square_foot="640",
                    population_25_44="40",
                    population_45_64="25",
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
                    population_25_44="80",
                    population_45_64="10",
                    adult_population="100",
                    active_listing_count="30",
                    female_population="500",
                    male_population="500",
                ),
                join_row(
                    "90040",
                    zip_name="older hills, ca",
                    median_household_income="50",
                    per_capita_income="200",
                    median_listing_price="400",
                    median_listing_price_per_square_foot="800",
                    population_45_64="30",
                    population_65_plus="60",
                    adult_population="100",
                    active_listing_count="12",
                    female_population="800",
                    male_population="200",
                ),
            ]
        )

    def test_bullets_follow_the_procedure_persona_lists(self):
        def rows(name):
            return rank.bullet_rows(name, self.screens)

        young = rows("breast augmentation")
        older = rows("facelift")
        def lists(name):
            return [(row.age_range, row.zip_code) for row in rows(name)]

        self.assertEqual(lists("breast augmentation"), lists("rhinoplasty"))
        self.assertEqual(lists("breast augmentation"), lists("buttock augmentation with fat grafting"))
        self.assertEqual(lists("facelift"), lists("eyelid surgery"))
        self.assertEqual(lists("facelift"), lists("forehead lift"))
        self.assertEqual(lists("facelift"), lists("neck lift"))
        self.assertEqual(lists("arm lift"), lists("thigh lift"))
        self.assertEqual(lists("breast lift"), lists("tummy tuck"))
        self.assertEqual(lists("liposuction"), lists("breast reduction"))
        self.assertEqual(
            [(row.rank, row.city, row.state, row.zip_code) for row in young],
            [(1, "pasadena", "ca", "90012")],
        )
        self.assertEqual([row.age_range for row in rows("breast lift")], ["25–44", "45–64"])
        self.assertEqual(
            young[0].header,
            "Market breast augmentation to adults ages 25 to 44 in the ZIP codes below. "
            "Why: median household income and median listing price per square foot in these "
            "ZIP codes are at or above the medians of ZIP codes within 50 miles of the practice, $200 and $345. "
            "Affordability wasn't checked because this procedure has no published surgeon fee. "
            "ZIP codes are ranked by a score from 0 to 100 that compares each one with the other "
            "ZIP codes within 50 miles on these weighted measures: share of adults ages 25 to 44 (38%), "
            "women's share of adults ages 25 to 44 (25%), home ownership among households headed by "
            "someone 25–44 (13%), share of employed women in management and professional jobs (13%), "
            "mothers of children under 18 per adult (5%), mothers of children under 6 per adult (4%), "
            "and recent births per adult (4%). "
            "Only ZIP codes within 50 miles of the practice (ZIP 90210) are included, because patients "
            "rarely drive more than 50 miles to a new practice that is still building its reputation. "
            "Race, ethnicity and education describe these ZIP codes and are not used to rank them.",
        )
        self.assertEqual(
            young[0].insight,
            "Why Pasadena (90012): it ranks 1st in this list with a score of 31.3 out of 100. "
            "No measure here is above the median of ZIP codes within 50 miles. "
            "40.0% of adults here are ages 25 to 44. "
            "Median household income is $300 and median listing price per square foot is $640, "
            "both at or above the median of ZIP codes within 50 miles of the practice. "
            "After an estimated $26 in taxes and $53,014 in essential costs, a typical household headed "
            "by someone 25–44 here has nothing left of its $300 income for discretionary items. "
            "Women are 75.0% of residents.",
        )
        self.assertEqual([row.city for row in young], ["pasadena"])
        for row in young + older:
            self.assertTrue(row.insight.startswith(f"Why "), row.insight)
            self.assertIn(f"({row.zip_code}):", row.insight)
            self.assertNotRegex(row.insight, r"^\s*(- )?\d+\. ")
        self.assertEqual([(row.age_range, row.zip_code) for row in older], [("45–64", "90012"), ("65+", "90040")])
        self.assertTrue(older[0].header.startswith("Market facelift to adults ages 45 to 64 in the ZIP codes below. "))
        self.assertIn(
            "Why: median listing price per square foot and per-person income in these ZIP codes",
            older[1].header,
        )
        self.assertIn("per-person income is $200", older[1].insight)
        self.assertIn("$800", older[1].insight)
        self.assertEqual(rows("custom procedure"), [])

    def test_group_without_a_ranked_zip_adds_no_row(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="pasadena, ca",
                    median_household_income="300",
                    median_listing_price="300",
                    population_25_44="40",
                    adult_population="100",
                )
            ]
        )
        found = rank.bullet_rows("rhinoplasty", screens)
        self.assertEqual(
            [row.header.split(".")[0] for row in found],
            ["Market rhinoplasty to adults ages 25 to 44 in the ZIP codes below"],
        )

    def test_city_is_title_cased_in_the_text_only(self):
        self.assertEqual(rank.display_city("marina del rey"), "Marina del Rey")
        self.assertEqual(rank.display_city("la canada flintridge"), "La Canada Flintridge")
        self.assertEqual(rank.display_city("view park-windsor hills"), "View Park-Windsor Hills")
        self.assertEqual([rank.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23)],
            ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd"])

    def test_city_and_state_come_from_the_zip_name(self):
        self.assertEqual(rank.split_place({"zip_name": "marina del rey, ca"}), ("marina del rey", "ca"))
        self.assertEqual(rank.split_place({"zip_name": "Pasadena"}), ("Pasadena", ""))
        self.assertEqual(rank.split_place({"zip_name": ""}), ("", ""))

    def test_tableau_file_writes_one_row_per_zip(self):
        found = rank.bullet_rows("facelift", self.screens)
        stored = rank.bullet_update(found)
        columns = [
            "report_year",
            "procedure",
            "surgeon_fee_low",
            "marketing_bullets",
            "rank",
            "city",
            "state",
            "zip_code",
            "practice_zip_code",
            "county",
            "distance_from_practice_miles",
            "insight",
            "household_income",
            "individual_income",
            "median_listing_price_per_square_foot",
            "age_range",
            "sex",
            "race_and_ethnicity",
            "education_level",
            *rank.DESCRIPTIVE_LIST_NAMES,
            "composite_score",
            *rank.SCORE_COLUMNS,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "procedure_surgeon_fees.csv"
            count = rank.write_procedure_fee_csv(
                columns,
                [
                    (2024, "facelift", 8000) + stored,
                    (2024, "custom procedure", 100) + rank.bullet_update([]),
                    (2025, "facelift", None) + (None,) * len(rank.BULLET_CSV_COLUMNS),
                ],
                path,
            )
            raw = path.read_text(encoding="utf-8")
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(list(reader.fieldnames), columns)
                written = list(reader)
        self.assertEqual(count, len(found) + 2)
        self.assertEqual(len(raw.splitlines()), len(written) + 1)
        zip_rows = written[: len(found)]
        self.assertEqual(
            [
                (row["marketing_bullets"], row["rank"], row["city"], row["state"], row["zip_code"], row["insight"])
                for row in zip_rows
            ],
            [
                (item.header, str(item.rank), item.city, item.state, item.zip_code, item.insight)
                for item in found
            ],
        )
        self.assertTrue(all(row["rank"] for row in zip_rows))
        self.assertTrue(
            all(row["report_year"] == "2024" and row["procedure"] == "facelift" and row["surgeon_fee_low"] == "8000" for row in zip_rows)
        )
        for row in written:
            self.assertTrue(all("\n" not in value and "\r" not in value for value in row.values()))
        for row in written[len(found):]:
            self.assertEqual(
                [row[name] for name in rank.BULLET_CSV_COLUMNS], [""] * len(rank.BULLET_CSV_COLUMNS)
            )
        self.assertEqual(written[-2]["procedure"], "custom procedure")
        self.assertEqual(written[-1]["report_year"], "2025")
        self.assertEqual(written[-1]["surgeon_fee_low"], "")

    def test_shared_persona_lists_repeat_the_same_rows(self):
        names = ["breast augmentation", "rhinoplasty", "facelift", "eyelid surgery"]
        bullets = rank.bullets_by_procedure(names, self.screens)
        def body(name):
            return [(row.age_range, row.rank, row.zip_code) for row in bullets[name]]

        self.assertEqual(body("breast augmentation"), body("rhinoplasty"))
        self.assertEqual(body("facelift"), body("eyelid surgery"))
        self.assertNotEqual(
            bullets["breast augmentation"][0].composite_score, bullets["rhinoplasty"][0].composite_score
        )
        self.assertTrue(bullets["rhinoplasty"][0].header.startswith("Market rhinoplasty to "))
        self.assertNotEqual(bullets["breast augmentation"], bullets["facelift"])


LIVING = Decimal(60000)


class AffordabilityTests(unittest.TestCase):
    def _screen(self, label: str, incomes: dict[str, int]) -> rank.PersonaRank:
        persona = rank.PERSONA_BY_LABEL[label]
        passed = []
        for position, (zip_code, income) in enumerate(incomes.items(), start=1):
            passed.append(
                {
                    "zip": zip_code,
                    "zip_name": f"place {zip_code}, ca",
                    "rank": position,
                    "age_share": Decimal("0.4"),
                    "median_household_income": income,
                    "per_capita_income": income,
                    "median_listing_price_per_square_foot": Decimal(700),
                    "female_share": None,
                    "cost_of_living": LIVING,
                    **discretionary.household_fields(income, LIVING, persona.householder_band),
                }
            )
        return rank.PersonaRank(
            persona,
            Decimal(100000),
            Decimal(50000),
            Decimal(600),
            passed[: rank.TOP_N],
            passed,
        )

    def test_every_persona_uses_its_householder_band_income(self):
        brackets = {
            name: "0"
            for band, _first, _label in census.HOUSEHOLDER_AGE_BANDS
            for name in census.householder_income_columns(band)
        }
        brackets["householder_25_44_households_income_100000_124999"] = "10"
        brackets["householder_45_64_households_income_150000_199999"] = "10"
        row = join_row(
            "90001",
            population_25_44="40",
            population_45_64="30",
            population_65_plus="20",
            adult_population="100",
            **brackets,
        )
        found = {
            label: rank.screen_row(row, persona)["household_income_for_age_range"]
            for label, persona in rank.PERSONA_BY_LABEL.items()
        }
        self.assertEqual(found, {"25–44": 112500, "45–64": 175000, "65+": 200000})
        top_heavy = [1] * 14 + [2, 100]
        self.assertEqual(rank.median_from_brackets(top_heavy), Decimal(250001))
        self.assertEqual(rank.median_from_brackets([0] * 15 + [5]), Decimal(250001))
        self.assertIsNone(rank.median_from_brackets([0] * 16))
        self.assertIsNone(rank.median_from_brackets([None] + [1] * 15))
        self.assertEqual(rank.screen_row(join_row("90002", population_25_44="40"), rank.PERSONAS[0])["household_income_for_age_range"], 200000)

    def test_older_affordability_ignores_per_person_income(self):
        screen = self._screen("65+", {"90001": 150000, "90002": 100000})
        for row in screen.passed:
            row["per_capita_income"] = 40000
        kept = rank.render_screen("tummy tuck", screen, Decimal(15500))
        self.assertEqual([(row.rank, row.zip_code) for row in kept], [(1, "90001")])
        self.assertIn("per-person income is $40,000, both at or above the median of ZIP codes within 50 miles of the practice.", kept[0].insight)
        self.assertIn(
            "After an estimated $21,736 in taxes and $60,000 in essential costs, a typical household "
            "headed by someone 65 or older here has about $68,264 left of its $150,000 income and "
            "spends about 33.3% of that, roughly $22,748 a year, on discretionary items.",
            kept[0].insight,
        )
        self.assertTrue(
            kept[0].header.startswith(
                "Market tummy tuck to adults ages 65 and older in the ZIP codes below. "
                "Why: median listing price per square foot and per-person income"
            )
        )

    def test_fee_midpoint(self):
        self.assertEqual(rank.fee_midpoint(8000, 15000), Decimal(11500))
        self.assertEqual(rank.fee_midpoint(5000, 7575), Decimal("6287.5"))
        self.assertIsNone(rank.fee_midpoint(None, 15000))
        self.assertIsNone(rank.fee_midpoint(8000, None))

    def test_unaffordable_zip_is_dropped_and_the_rest_re_ranked(self):
        price = Decimal(10000)
        just_enough = discretionary.income_needed(price, LIVING, "25_44")
        self.assertLess(discretionary.discretionary_income(just_enough - 1, LIVING, "25_44"), price)
        screen = self._screen(
            "25–44", {"90001": 50000, "90002": just_enough, "90003": 300000}
        )
        kept = rank.render_screen("tummy tuck", screen, price)
        self.assertEqual([(row.rank, row.zip_code) for row in kept], [(1, "90003"), (2, "90002")])
        self.assertEqual([row.income_needed for row in kept], [just_enough, just_enough])
        unscreened = rank.render_screen("tummy tuck", screen, None)
        self.assertEqual([row.rank for row in unscreened], [1, 2, 3])
        self.assertIn(
            "Affordability wasn't checked because this procedure has no published surgeon fee.",
            unscreened[0].header,
        )
        self.assertNotIn("Sources:", unscreened[0].header)
        self.assertNotIn("fee.", unscreened[0].insight)
        self.assertIn(
            "a typical household headed by someone 25–44 here has nothing left of its $50,000 income "
            "for discretionary items.",
            unscreened[2].insight,
        )
        self.assertTrue(kept[1].insight.startswith("Why Place 90002 (90002): it ranks 2nd in this list "))

    def test_join_without_housing_costs_is_refused(self):
        rows = [
            join_row("90001", population_25_44="40", median_gross_rent="", median_owner_costs=""),
            join_row("90002", population_25_44="30", median_gross_rent="", median_owner_costs=""),
        ]
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            rank.rank_personas(rows)
        self.assertIn("Run Pull Census ZIP population", str(raised.exception))
        rows[1]["median_gross_rent"] = "2100"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(len(rank.rank_personas(rows)), len(rank.PERSONAS))

    def test_blank_cost_of_living_never_passes_the_screen(self):
        screen = self._screen("25–44", {"90001": 300000})
        screen.passed[0].update(discretionary.household_fields(300000, None, "25_44"), cost_of_living=None)
        self.assertEqual(rank.render_screen("tummy tuck", screen, Decimal(1000)), [])
        unscreened = rank.render_screen("tummy tuck", screen, None)[0]
        self.assertIsNone(unscreened.discretionary_income)
        self.assertEqual(unscreened.household_income_for_age_range, 300000)
        self.assertNotIn("After an estimated", unscreened.insight)

    def test_dropped_zips_are_backfilled_past_the_top_25(self):
        incomes = {f"9{index:04d}": 50000 for index in range(rank.TOP_N)}
        incomes.update({"91111": 300000, "92222": 300000})
        screen = self._screen("25–44", incomes)
        self.assertNotIn("91111", {row["zip"] for row in screen.rows})
        kept = rank.render_screen("tummy tuck", screen, Decimal(20000))
        self.assertEqual([row.zip_code for row in kept], ["91111", "92222"])

    def test_rankings_differ_by_procedure_price(self):
        screen = self._screen("45–64", {"90001": 100000, "90002": 250000})
        older = self._screen("65+", {"90001": 100000, "90002": 250000})
        screens = [screen, older]
        bullets = rank.bullets_by_procedure(
            ["forehead lift", "facelift"],
            screens,
            {"forehead lift": Decimal(5750), "facelift": Decimal(15500)},
        )
        self.assertEqual(
            [row.zip_code for row in bullets["forehead lift"]], ["90002", "90001", "90001", "90002"]
        )
        self.assertEqual([row.zip_code for row in bullets["facelift"]], ["90002", "90002"])
        self.assertEqual([row.rank for row in bullets["facelift"]], [1, 1])

    def test_header_and_insight_describe_the_screen(self):
        screen = self._screen("25–44", {"90001": 200000})
        row = rank.render_screen("tummy tuck", screen, Decimal(10750))[0]
        self.assertIn(
            "After estimated taxes, housing, and other essential costs, a typical household headed "
            "by someone 25–44 in each ZIP code has enough left for discretionary spending to cover "
            "the $10,750 midpoint surgeon fee.",
            row.header,
        )
        self.assertTrue(
            row.header.endswith(
                " Sources: 2024 federal and California tax rules, ACS housing costs, and the BLS "
                "Consumer Expenditure Surveys."
            )
        )
        self.assertIn(
            "After an estimated $55,538 in taxes and $60,000 in essential costs, a typical household "
            "headed by someone 25–44 here has about $84,462 left of its $200,000 income and spends "
            "about 17.5% of that, roughly $14,797 a year, on discretionary items.",
            row.insight,
        )
        self.assertEqual(
            (row.estimated_taxes, row.average_cost_of_living, row.money_left_after_essentials),
            (55538, 60000, 84462),
        )
        self.assertEqual(
            (row.income_bracket, row.discretionary_share_of_remaining, row.discretionary_income),
            ("$200,000 and more", Decimal("17.5"), 14797),
        )
        older = self._screen("65+", {"90001": 100000})
        older_row = rank.render_screen("facelift", older, Decimal(5750))[0]
        self.assertIn("headed by someone 65 or older in each ZIP code", older_row.header)
        self.assertNotIn("per-person income", older_row.header.split("Why: ")[1].split(". ", 1)[1])

    def test_a_group_with_no_affordable_zip_adds_no_rows(self):
        screen = self._screen("65+", {"90001": 40000})
        self.assertEqual(rank.render_screen("tummy tuck", screen, Decimal(15500)), [])

    def test_apply_writes_null_when_no_zip_can_afford_the_fee(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="pasadena, ca",
                    median_household_income="100000",
                    population_25_44="40",
                    adult_population="100",
                )
            ]
        )

        class Cursor:
            def __init__(self):
                self.batches = []

            def execute(self, sql, params=None):
                pass

            def executemany(self, sql, batch):
                self.batches.append(list(batch))

            def fetchall(self):
                return []

        cursor = Cursor()
        rank.apply_marketing_bullets(
            cursor,
            screens,
            [(2024, "rhinoplasty", 7000, 12000), (2024, "liposuction", 1000, 1000)],
            2024,
        )
        by_name = {row[-1]: row for row in cursor.batches[0]}
        self.assertEqual(by_name["rhinoplasty"][:6], (None,) * 6)
        self.assertEqual(by_name["liposuction"][1], [1])


def profile_columns(**overrides) -> dict[str, str]:
    """A ZIP with one value for every profile column, then the overrides."""
    counted = census.PROFILE_COLUMNS + census.HOUSEHOLDER_AGE_COLUMNS
    values = {name: "0" for name in counted if name not in census.AGE_BAND_GROUPS}
    brackets = [column for column, _code, _phrase, _low in census.HOUSEHOLD_INCOME_BRACKETS]
    for column in brackets:
        values[column] = "100"
    for band, _first, _label in census.HOUSEHOLDER_AGE_BANDS:
        for column in census.householder_income_columns(band):
            values[column] = "100"
    values.update(
        households=str(100 * len(brackets)),
        female_population_25_44="52",
        male_population_25_44="48",
        female_population_65_plus="30",
        male_population_65_plus="20",
        mothers_with_children_under_18="1234",
        mothers_with_children_under_6="456",
        recent_births="78",
        owner_occupied_households="621",
        renter_occupied_households="379",
        owner_occupied_householder_25_44="48",
        renter_occupied_householder_25_44="52",
        employed_women="1000",
        women_management_business_science_arts="583",
        women_arts_design_entertainment_sports_media="47",
    )
    values.update({key: "" if value is None else str(value) for key, value in overrides.items()})
    return values


# Households in the open $200,000-and-over bracket for every householder band,
# so each band's median household income is about $232,000.
RICH_BANDS = {
    f"householder_{band}_households_income_200000_plus": "1600"
    for band, _first, _label in census.HOUSEHOLDER_AGE_BANDS
}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.row = join_row(
            "90292",
            zip_name="marina del rey, ca",
            median_household_income="300000",
            population_25_44="40",
            population_65_plus="20",
            adult_population="100",
            **profile_columns(**RICH_BANDS),
        )
        self.living = discretionary.cost_of_living(2000, None, Decimal("0.621"), Decimal("2.9"))

    def profile(self, **overrides):
        return {name: rank.as_int(value, name) for name, value in profile_columns(**overrides).items()}

    def test_affordable_households_use_the_matching_householder_band(self):
        self.assertEqual([persona.label for persona in rank.PERSONAS], ["25–44", "45–64", "65+"])
        price = Decimal(20000)
        for label, band in (("25–44", "25_44"), ("45–64", "45_64"), ("65+", "65_plus")):
            band_persona = rank.PERSONA_BY_LABEL[label]
            base = rank.zip_metrics({"profile": self.profile(), "cost_of_living": LIVING}, band_persona, price)
            all_households = rank.zip_metrics(
                {"profile": self.profile(households_income_200000_plus="5000"), "cost_of_living": LIVING},
                band_persona,
                price,
            )
            self.assertEqual(base["affordable_households"], all_households["affordable_households"])
            richer = rank.zip_metrics(
                {
                    "profile": self.profile(**{f"householder_{band}_households_income_200000_plus": "5000"}),
                    "cost_of_living": LIVING,
                },
                band_persona,
                price,
            )
            self.assertGreater(richer["affordable_households"], base["affordable_households"])
            self.assertEqual(
                base["affordable_households_share"],
                rank.percent_of(
                    rank.households_at_or_above(
                        self.profile(), Decimal(base["income_needed"]), census.householder_income_columns(band)
                    ),
                    1600,
                ),
            )
        older = self.row | {"population_65_plus": "40", "per_capita_income": "80000"}
        screen = rank.rank_personas([older])
        kept = rank.render_screen("facelift", persona(screen, "65+"), Decimal(5750))
        self.assertIn("of households headed by someone 65 or older here", kept[0].insight)

    def test_linear_interpolation_inside_a_closed_bracket(self):
        profile = self.profile()
        self.assertEqual(rank.households_at_or_above(profile, Decimal(0)), Decimal(1600))
        self.assertEqual(rank.households_at_or_above(profile, Decimal(100000)), Decimal(400))
        self.assertEqual(rank.households_at_or_above(profile, Decimal(112500)), Decimal(350))
        self.assertEqual(rank.households_at_or_above(profile, Decimal(5000)), Decimal(1550))
        self.assertEqual(rank.households_at_or_above(profile, Decimal(175000)), Decimal(150))
        self.assertIsNone(rank.households_at_or_above(self.profile(households_income_60000_74999=""), Decimal(1)))

    def test_pareto_tail_only_in_the_open_bracket(self):
        profile = self.profile()
        self.assertEqual(rank.households_at_or_above(profile, Decimal(200000)), Decimal(100))
        alpha = (Decimal(2).ln()) / (Decimal(200000) / Decimal(150000)).ln()
        expected = Decimal(100) * (Decimal(200000) / Decimal(300000)) ** alpha
        self.assertEqual(rank.households_at_or_above(profile, Decimal(300000)), expected)
        self.assertLess(expected, Decimal(100))
        none_between = self.profile(households_income_150000_199999="0")
        self.assertIsNone(rank.households_at_or_above(none_between, Decimal(250000)))
        none_on_top = self.profile(households_income_200000_plus="0")
        self.assertEqual(rank.households_at_or_above(none_on_top, Decimal(250000)), Decimal(0))

    def test_income_needed_uses_the_zip_cost_of_living(self):
        persona = rank.PERSONA_BY_LABEL["25–44"]
        cheap = rank.income_needed({"cost_of_living": Decimal(40000)}, Decimal(8250), persona)
        dear = rank.income_needed({"cost_of_living": LIVING}, Decimal(8250), persona)
        self.assertEqual(dear, discretionary.income_needed(Decimal(8250), LIVING, "25_44"))
        self.assertLess(cheap, dear)
        self.assertIsNone(rank.income_needed({"cost_of_living": None}, Decimal(8250), persona))

    def test_row_metrics_and_insight_sentences(self):
        screens = rank.rank_personas([self.row])
        row = rank.bullet_rows("tummy tuck", screens, Decimal(10750))[0]
        needed = discretionary.income_needed(Decimal(10750), self.living, "25_44")
        affordable = rank.households_at_or_above(
            self.profile(**RICH_BANDS), Decimal(needed), census.householder_income_columns("25_44")
        )
        self.assertEqual(row.income_needed, needed)
        self.assertEqual(row.affordable_households, int(affordable.quantize(Decimal(1), rounding="ROUND_HALF_UP")))
        self.assertEqual(row.affordable_households_share, rank.percent_points(affordable / Decimal(3100)))
        self.assertEqual(row.average_cost_of_living, discretionary.whole_dollars(self.living))
        self.assertGreater(row.household_income_for_age_range, 200000)
        self.assertLess(row.household_income_for_age_range, 300000)
        self.assertEqual((row.women_in_age_range, row.men_in_age_range), (52, 48))
        self.assertEqual(row.women_share_of_age_range, Decimal("52.0"))
        self.assertEqual((row.home_ownership, row.owner_occupied_share), ("own", Decimal("62.1")))
        self.assertEqual(row.owner_occupied_share_age_range, Decimal("48.0"))
        self.assertEqual(row.women_management_professional_share, Decimal("58.3"))
        self.assertEqual(row.women_arts_entertainment_media_share, Decimal("4.7"))
        self.assertEqual(
            (row.mothers_with_children_under_18, row.mothers_with_children_under_6, row.recent_births),
            (1234, 456, 78),
        )
        self.assertIn(
            f"About {row.affordable_households_share}% of households headed by someone 25–44 here "
            f"({row.affordable_households:,} households) earn at least ${needed:,}, "
            "enough to cover the $10,750 fee.",
            row.insight,
        )
        self.assertIn("Women are 60.0% of residents and 52.0% of adults ages 25 to 44.", row.insight)
        self.assertIn(
            "Here, 62.1% of households own their home, and 58.3% of employed women work in "
            "management, business, science, or arts jobs.",
            row.insight,
        )
        self.assertTrue(
            row.insight.endswith(
                "About 1,234 women ages 20 to 64 here are raising children under 18, "
                "including 456 with a child under 6."
            )
        )
        older = rank.bullet_rows("facelift", screens, Decimal(15500))
        self.assertEqual([item.age_range for item in older], ["65+"])
        self.assertEqual((older[0].women_in_age_range, older[0].men_in_age_range), (30, 20))
        self.assertNotIn("raising children", older[0].insight)
        self.assertIsNone(older[0].owner_occupied_share_age_range)

    def test_mothers_sentence_only_for_body_and_breast_procedures(self):
        screens = rank.rank_personas([self.row])
        for procedure in rank.PROCEDURE_PERSONAS:
            for item in rank.bullet_rows(procedure, screens, Decimal(1000)):
                self.assertEqual(
                    "raising children under 18" in item.insight,
                    procedure in rank.MOTHER_PROCEDURES,
                    procedure,
                )
                self.assertEqual(item.mothers_with_children_under_18, 1234)
        self.assertEqual(
            rank.MOTHER_PROCEDURES,
            {"tummy tuck", "breast lift", "breast augmentation", "breast reduction", "liposuction"},
        )

    def test_no_fee_leaves_affordability_blank(self):
        screens = rank.rank_personas([self.row])
        for item in rank.bullet_rows("eyelid surgery", screens, None):
            self.assertIsNone(item.income_needed)
            self.assertIsNone(item.affordable_households)
            self.assertIsNone(item.affordable_households_share)
            self.assertNotIn("earn at least", item.insight)
            self.assertEqual(item.home_ownership, "own")

    def test_renter_majority_says_rent(self):
        row = dict(self.row, owner_occupied_households="400", renter_occupied_households="600")
        item = rank.bullet_rows("tummy tuck", rank.rank_personas([row]), Decimal(10750))[0]
        self.assertEqual((item.home_ownership, item.owner_occupied_share), ("rent", Decimal("40.0")))
        self.assertIn("Here, 60.0% of households rent their home, and ", item.insight)

    def test_profile_never_changes_the_persona_rank(self):
        rows = []
        for index, share in enumerate(("40", "35", "30")):
            rows.append(
                join_row(
                    f"9000{index}",
                    median_household_income="300000",
                    population_25_44=share,
                    adult_population="100",
                    **profile_columns(householder_25_44_households_income_200000_plus=str(10 + index * 500)),
                )
            )
        counted = set(census.PROFILE_COLUMNS + census.HOUSEHOLDER_AGE_COLUMNS) - set(census.AGE_BAND_GROUPS)
        plain = [{key: value for key, value in row.items() if key not in counted} for row in rows]
        for label in rank.PERSONA_BY_LABEL:
            self.assertEqual(
                [row["zip"] for row in persona(rank.rank_personas(rows), label).rows],
                [row["zip"] for row in persona(rank.rank_personas(plain), label).rows],
            )
        screened = rank.bullet_rows("rhinoplasty", rank.rank_personas(rows), Decimal(8000))
        self.assertEqual([item.zip_code for item in screened], ["90002"])


class MeasureColumnTests(unittest.TestCase):
    def setUp(self):
        self.screens = rank.rank_personas(
            [
                join_row(
                    "90292",
                    zip_name="marina del rey, ca",
                    median_household_income="146167",
                    per_capita_income="120000",
                    median_listing_price="2000000",
                    median_listing_price_per_square_foot="857.5",
                    population_25_44="29",
                    population_65_plus="30",
                    adult_population="100",
                    female_population="538",
                    male_population="462",
                    hispanic_or_latino_population="150",
                    non_hispanic_white_population="520",
                    non_hispanic_black_population="40",
                    non_hispanic_asian_population="230",
                    non_hispanic_other_or_multiracial_population="60",
                    education_less_than_high_school="20",
                    education_high_school="80",
                    education_some_college_or_associate="150",
                    education_bachelors="410",
                    education_graduate_or_professional="340",
                )
            ]
        )

    def test_columns_match_the_values_in_the_insight(self):
        rows = rank.bullet_rows("arm lift", self.screens, Decimal(8250))
        self.assertEqual([row.age_range for row in rows], ["25–44", "65+"])
        for row in rows:
            self.assertEqual(row.household_income, 146167)
            self.assertEqual(row.individual_income, 120000)
            self.assertEqual(row.median_listing_price_per_square_foot, Decimal("857.5"))
            self.assertEqual(row.sex, "woman")
            self.assertEqual(row.race_and_ethnicity, "Non-Hispanic White")
            self.assertEqual(row.education_level, "Bachelor's degree")
            self.assertTrue(
                row.insight.endswith(
                    "Women are 53.8% of residents. Non-Hispanic White residents are the largest "
                    "group at 52.0%, and 75.0% of adults 25 and older have a bachelor's degree or higher."
                ),
                row.insight,
            )
            self.assertIn(
                " Race, ethnicity and education describe these ZIP codes and are not used to rank them. Sources: ",
                row.header,
            )
            self.assertIn("median listing price per square foot is $858", row.insight.lower())
            self.assertTrue(row.insight.startswith("Why Marina del Rey (90292): "))
            self.assertEqual(row.city, "marina del rey")
        self.assertIn("Median household income is $146,167", rows[0].insight)
        self.assertTrue(rows[1].header.startswith("Market arm lift to adults ages 65 and older in the ZIP codes below. "))

    def test_text_is_plain_whole_dollar_sentences(self):
        texts = []
        for price in (None, Decimal("8250.5")):
            for row in rank.bullet_rows("arm lift", self.screens, price):
                texts += [row.header, row.insight]
        for text in texts:
            lowered = text.lower()
            self.assertNotIn("middle", lowered)
            self.assertNotIn("in this screen", lowered)
            self.assertNotRegex(text, r"^\d")
            self.assertNotRegex(text, r"\$[\d,]+\.\d")
            self.assertTrue(text.endswith("."), text)
        self.assertIn("$8,251", " ".join(texts))

    def test_race_and_education_describe_but_never_rank(self):
        def zip_row(zip_code, white, hispanic, bachelors, share):
            return join_row(
                zip_code,
                median_household_income="300000",
                population_25_44=share,
                adult_population="100",
                hispanic_or_latino_population=hispanic,
                non_hispanic_white_population=white,
                non_hispanic_black_population="0",
                non_hispanic_asian_population="0",
                non_hispanic_other_or_multiracial_population="0",
                education_less_than_high_school="10",
                education_high_school="10",
                education_some_college_or_associate="10",
                education_bachelors=bachelors,
                education_graduate_or_professional="0",
            )

        rows = [zip_row("90001", "10", "90", "0", "30"), zip_row("90002", "90", "10", "90", "20")]
        plain = [join_row(row["zip"], **{k: v for k, v in row.items() if k not in rank.RACE_ETHNICITY_LABELS and k not in rank.EDUCATION_LABELS}) for row in rows]
        ranked = rank.bullet_rows("tummy tuck", rank.rank_personas(rows), Decimal(10750))
        unlabeled = rank.bullet_rows("tummy tuck", rank.rank_personas(plain), Decimal(10750))
        self.assertEqual(
            [(row.age_range, row.rank, row.zip_code) for row in ranked],
            [(row.age_range, row.rank, row.zip_code) for row in unlabeled],
        )
        self.assertEqual([row.race_and_ethnicity for row in ranked[:2]], ["Hispanic or Latino", "Non-Hispanic White"])
        self.assertEqual([row.education_level for row in ranked[:2]], ["Less than high school", "Bachelor's degree"])
        self.assertEqual([row.race_and_ethnicity for row in unlabeled], [None] * len(unlabeled))
        self.assertNotIn("largest group", unlabeled[0].insight)
        self.assertNotIn("bachelor", unlabeled[0].insight)
        self.assertIn("are not used to rank them.", unlabeled[0].header)
        tie = rank.largest_group(
            {name: "5" for name in rank.EDUCATION_LABELS}, rank.EDUCATION_LABELS
        )
        self.assertEqual(tie, ("Less than high school", Decimal("0.2")))
        only_degree = dict(rows[1])
        only_degree["non_hispanic_asian_population"] = ""
        described = rank.describe_zip(only_degree)
        self.assertEqual(
            rank.describe_sentence(described),
            "Of adults 25 and older, 75.0% have a bachelor's degree or higher.",
        )

    def test_sex_is_woman_at_fifty_percent_or_more(self):
        self.assertEqual(rank.sex_label(Decimal("0.538")), "woman")
        self.assertEqual(rank.sex_label(Decimal("0.5")), "woman")
        self.assertEqual(rank.sex_label(Decimal("0.49951")), "woman")
        self.assertEqual(rank.sex_label(Decimal("0.4994")), "man")
        self.assertEqual(rank.sex_label(Decimal("0.3")), "man")
        self.assertIsNone(rank.sex_label(None))

    def test_mostly_male_zip_is_man_and_keeps_its_insight(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="pasadena, ca",
                    median_household_income="300000",
                    population_25_44="40",
                    adult_population="100",
                    female_population="499",
                    male_population="501",
                )
            ]
        )
        row = rank.bullet_rows("tummy tuck", screens, Decimal(10750))[0]
        self.assertEqual(row.sex, "man")
        self.assertIn("Women are 49.9% of residents.", row.insight)
        names = list(rank.BULLET_LIST_NAMES)
        self.assertEqual(
            names[names.index("sex") :],
            [
                "sex",
                "race_and_ethnicity",
                "education_level",
                "women_in_age_range",
                "men_in_age_range",
                "women_share_of_age_range",
                "household_income_for_age_range",
                "estimated_taxes",
                "average_cost_of_living",
                "money_left_after_essentials",
                "income_bracket",
                "discretionary_share_of_remaining",
                "discretionary_income",
                "income_needed",
                "affordable_households_share",
                "affordable_households",
                "mothers_with_children_under_18",
                "mothers_with_children_under_6",
                "recent_births",
                "home_ownership",
                "owner_occupied_share",
                "owner_occupied_share_age_range",
                "women_management_professional_share",
                "women_arts_entertainment_media_share",
                "composite_score",
                "score_age_share",
                "score_women_share_of_age_range",
                "score_affordable_households_share",
                "score_affordable_households",
                "score_mothers_with_children_under_18",
                "score_mothers_with_children_under_6",
                "score_recent_births",
                "score_owner_occupied_share_age_range",
                "score_women_management_professional_share",
                "score_women_arts_entertainment_media_share",
            ],
        )

    def test_blank_female_share_leaves_sex_blank(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    median_household_income="300000",
                    population_25_44="40",
                    adult_population="100",
                    female_population="",
                )
            ]
        )
        row = rank.bullet_rows("tummy tuck", screens, Decimal(10750))[0]
        self.assertIsNone(row.sex)
        self.assertNotIn("Women are", row.insight)

    def test_stored_lists_rebuild_the_measure_columns(self):
        found = rank.bullet_rows("arm lift", self.screens, Decimal(8250))
        columns = ["report_year", "procedure"] + list(rank.BULLET_CSV_COLUMNS)
        expanded = rank.expand_marketing_bullet_rows(
            columns, [(2024, "arm lift") + rank.bullet_update(found)]
        )
        start = columns.index("household_income")
        tail = [row[start : start + 7] for row in expanded]
        self.assertEqual(
            tail,
            [
                (146167, 120000, Decimal("857.5"), "25–44", "woman", "Non-Hispanic White", "Bachelor's degree"),
                (146167, 120000, Decimal("857.5"), "65+", "woman", "Non-Hispanic White", "Bachelor's degree"),
            ],
        )
        self.assertEqual(
            list(rank.BULLET_CSV_COLUMNS[8:16]),
            [
                "insight",
                "household_income",
                "individual_income",
                "median_listing_price_per_square_foot",
                "age_range",
                "sex",
                "race_and_ethnicity",
                "education_level",
            ],
        )

    def test_text_never_says_plain_listing_price(self):
        thin = rank.rank_personas([join_row("90001", active_listing_count="1")])
        texts = []
        for screens in (self.screens, thin):
            for procedure in rank.PROCEDURE_PERSONAS:
                for price in (None, Decimal(1000)):
                    for row in rank.bullet_rows(procedure, screens, price):
                        texts += [row.header, row.insight]
            for screen in screens:
                texts.append(rank.screen_header("arm lift", screen, Decimal(1000)))
        self.assertTrue(any("median listing price per square foot" in text.lower() for text in texts))
        for text in texts:
            lowered = text.lower()
            self.assertEqual(
                lowered.count("listing price"),
                lowered.count("median listing price per square foot"),
                text,
            )


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
                    median_household_income="300000",
                    median_listing_price="300",
                    population_25_44="40",
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

            def fetchall(self):
                return []

        cursor = Cursor()
        year, count = rank.apply_marketing_bullets(cursor, screens, procedure_rows, 2025)
        self.assertEqual(year, 2024)
        self.assertEqual(count, 1)
        clear_sql, clear = next(
            (sql, params) for sql, params in cursor.statements if "marketing_bullets = NULL" in sql
        )
        self.assertEqual(clear, (2024,))
        for name in ("rank", "city", "state", "zip_code", "insight"):
            self.assertIn(f"{name} = NULL", clear_sql)
        updates = next(
            batch
            for _sql, batch in cursor.statements
            if isinstance(batch, list) and batch
        )
        (
            headers,
            ranks,
            cities,
            states,
            zips,
            practice_zips,
            counties,
            distances,
            insights,
            incomes,
            individual_incomes,
            prices,
            ages,
            sexes,
            races,
            educations,
            *descriptive,
            report_year,
            name,
        ) = updates[0]
        self.assertEqual(len(descriptive), len(rank.DESCRIPTIVE_LIST_NAMES) + 1 + len(rank.SCORE_COLUMNS))
        self.assertEqual((races, educations), ([None], [None]))
        self.assertEqual(incomes, [300000])
        self.assertEqual(individual_incomes, [80000])
        self.assertEqual((len(updates), report_year, name), (1, 2024, "rhinoplasty"))
        self.assertTrue(headers.startswith("Market rhinoplasty to adults ages 25 to 44 "))
        self.assertNotIn("\n", headers)
        self.assertEqual(ranks, [1])
        self.assertEqual((cities, states, zips), (["Pasadena"], [""], ["90012"]))
        self.assertEqual((practice_zips, counties, distances), (["90210"], ["Los Angeles"], [Decimal("8.50")]))
        self.assertTrue(
            insights[0].startswith("Why Pasadena (90012): it ranks 1st in this list with a score of ")
        )
        joined = " ".join(sql for sql, _params in cursor.statements)
        for name, sql_type in rank.BULLET_LIST_COLUMNS:
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {name} {sql_type}", joined)
        self.assertNotIn("RENAME", joined)
        for sql, _params in cursor.statements:
            self.assertNotIn("surgeon_fee_low", sql)
            self.assertNotIn("surgeon_fee_high", sql)

    def _column_cursor(self, found):
        class Cursor:
            def __init__(self):
                self.statements: list[str] = []

            def execute(self, sql, params=None):
                self.statements.append(" ".join(sql.split()))

            def fetchall(self):
                return list(found.items())

        return Cursor()

    def test_existing_bullet_rank_is_renamed_to_rank(self):
        cursor = self._column_cursor({"marketing_bullets": "text", "bullet_rank": "int4"})
        rank.ensure_bullet_columns(cursor)
        joined = " ".join(cursor.statements)
        self.assertIn(
            "ALTER TABLE public.procedure_surgeon_fees RENAME COLUMN bullet_rank TO rank",
            joined,
        )
        self.assertIn("ALTER COLUMN rank TYPE integer[]", joined)
        self.assertNotIn("DROP", joined.upper())
        self.assertIn("ADD COLUMN IF NOT EXISTS insight text[]", joined)

    def test_bullet_rank_and_rank_together_are_refused(self):
        cursor = self._column_cursor({"bullet_rank": "_int4", "rank": "_int4"})
        with self.assertRaises(SystemExit):
            rank.ensure_bullet_columns(cursor)
        self.assertFalse(any("RENAME" in sql for sql in cursor.statements))


class SaveTests(unittest.TestCase):
    def test_save_replaces_the_rank_and_writes_the_join_year_when_it_has_fees(self):
        screens = rank.rank_personas(
            [
                join_row(
                    "90012",
                    zip_name="Pasadena",
                    median_household_income="300000",
                    median_listing_price="300",
                    population_25_44="40",
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
                elif "information_schema.columns" in text:
                    self._fetchone = None
                    self._fetchall = [("marketing_bullets", "text")]
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
                elif "information_schema.table_constraints" in text:
                    self._fetchone = None
                    self._fetchall = [("persona",), ("zip",)]
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
        count, year, bullet_count, rows = rank.save_rank(
            conn, screens, moment, preferred_year=2024
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            [(row["procedure"], row["persona"], row["zip"], row["surgeon_fee_midpoint"]) for row in rows],
            [("rhinoplasty", "25–44", "90012", Decimal(9500))],
        )
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
        by_name = {row[-1]: row for row in updates}
        self.assertEqual(set(by_name), {"facelift", "rhinoplasty", "custom procedure"})
        self.assertTrue(all(row[-2] == 2024 for row in updates))
        headers, ranks, cities, _states, zips, practice_zips, counties, _miles, insights = by_name["rhinoplasty"][:9]
        self.assertEqual((practice_zips, counties), (["90210"], ["Los Angeles"]))
        self.assertTrue(headers.startswith("Market rhinoplasty to adults ages 25 to 44 "))
        self.assertEqual((ranks, cities, zips), ([1], ["Pasadena"], ["90012"]))
        self.assertTrue(insights[0].startswith("Why Pasadena (90012): "))
        self.assertEqual(by_name["facelift"][:6], (None,) * 6)
        self.assertEqual(by_name["custom procedure"][:6], (None,) * 6)
        insert = next(rows for sql, rows in statements if "INSERT INTO public.cash_pay_zip_rank" in sql)
        self.assertIs(insert[0][-1], moment)
        texts = [sql for sql, _params in statements]
        delete_at = texts.index("DELETE FROM public.cash_pay_zip_rank")
        key_at = texts.index(
            "ALTER TABLE public.cash_pay_zip_rank ADD CONSTRAINT cash_pay_zip_rank_pkey "
            "PRIMARY KEY (procedure, persona, zip)"
        )
        self.assertLess(delete_at, key_at)
        self.assertIn("ALTER TABLE public.cash_pay_zip_rank DROP CONSTRAINT IF EXISTS cash_pay_zip_rank_pkey", texts)
        insert_at = next(index for index, sql in enumerate(texts) if sql.startswith("INSERT INTO public.cash_pay_zip_rank"))
        self.assertLess(key_at, insert_at)
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
        self.assertIn("PRIMARY KEY (procedure, persona, zip)", schema)
        self.assertIn("dataset_updated_at timestamptz", schema)
        self.assertNotIn("DROP", schema.upper())
        self.assertEqual(rank.DELETE_RANK_SQL, "DELETE FROM public.cash_pay_zip_rank")


class MainTests(unittest.TestCase):
    def test_csv_is_written_without_a_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "zip_census_redfin_join.csv"
            output = Path(tmp) / "cash_pay_zip_rank.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=list(rank.JOIN_FIELDS) + list(census.HOUSING_COST_COLUMNS),
                    lineterminator="\n", extrasaction="ignore"
                )
                writer.writeheader()
                writer.writerow(
                    join_row(
                        "90012",
                        zip_name="Pasadena",
                        population_25_44="40",
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
        young = [name for name, labels in rank.PROCEDURE_PERSONAS.items() if "25–44" in labels]
        self.assertEqual([row["procedure"] for row in rows], young)
        self.assertTrue(all(row["zip"] == "90012" and row["rank"] == "1" for row in rows))
        self.assertTrue(all(row["surgeon_fee_midpoint"] == "" for row in rows))
        self.assertIn("no list was screened for affordability", stdout.getvalue())
        rows = [row for row in rows if row["procedure"] == "rhinoplasty"]
        self.assertEqual(rows[0]["persona"], "25–44")
        self.assertEqual(rows[0]["zip_name"], "Pasadena")
        start = rank.RANK_COLUMNS.index("household_income_for_age_range")
        self.assertEqual(list(rank.RANK_COLUMNS[start : start + 7]), list(discretionary.FIELD_TYPES))
        self.assertEqual(
            list(rank.RANK_COLUMNS[start + 7 :]),
            list(rank.MEASURE_COLUMNS) + ["composite_score", *rank.SCORE_COLUMNS],
        )
        self.assertEqual(rows[0]["score_affordable_households"], "")
        self.assertNotEqual(rows[0]["score_women_arts_entertainment_media_share"], "")
        self.assertEqual(rows[0]["score_mothers_with_children_under_18"], "")
        self.assertEqual(rows[0]["composite_score"], "50.0")
        self.assertEqual(rows[0]["household_income_for_age_range"], "200000")
        self.assertEqual(rows[0]["average_cost_of_living"], "53014")
        self.assertEqual(rows[0]["income_bracket"], "$200,000 and more")
        self.assertEqual(rows[0]["discretionary_share_of_remaining"], "17.5")
        self.assertTrue(int(rows[0]["discretionary_income"]) > 0)

    def test_main_exports_fees_after_the_rank_is_saved(self):
        calls = []

        seen = {}

        def save_rank(conn, screens, updated_at=None, preferred_year=None):
            calls.append("save")
            seen["year"] = preferred_year
            return (1, preferred_year, 2, [{"procedure": "facelift", "persona": "45–64", "zip": "90012"}])

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
                    handle,
                    fieldnames=list(rank.JOIN_FIELDS) + list(census.HOUSING_COST_COLUMNS),
                    lineterminator="\n",
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerow(
                    join_row(
                        "90012",
                        zip_name="Pasadena",
                        population_25_44="40",
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
        self.assertIn("Wrote 1 rows to cash_pay_zip_rank.csv.", stdout.getvalue())
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
            "rank",
            "city",
            "state",
            "zip_code",
            "practice_zip_code",
            "county",
            "distance_from_practice_miles",
            "insight",
            "household_income",
            "individual_income",
            "median_listing_price_per_square_foot",
            "age_range",
            "sex",
            "race_and_ethnicity",
            "education_level",
            *rank.DESCRIPTIVE_LIST_NAMES,
            "composite_score",
            *rank.SCORE_COLUMNS,
        ]
        moment = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
        headers = "25–44. Ranked.\n45–64. Ranked."
        lists = (
            [1, 1],
            ["marina del rey", "pasadena"],
            ["ca", "ca"],
            ["90292", "90012"],
            ["90210", "90210"],
            ["Los Angeles", "Orange"],
            [Decimal("8.5"), Decimal("37.25")],
            ["Young.", "Thirties."],
            [146167, 98000],
            [64075, 52010],
            [Decimal("858"), Decimal("612.5")],
            ["25–44", "25–44"],
            ["woman", None],
            ["Hispanic or Latino", None],
            ["Bachelor's degree", None],
        ) + tuple([None, None] for _name in (*rank.DESCRIPTIVE_LIST_NAMES, "composite_score", *rank.SCORE_COLUMNS))
        blank = (None,) * (len(rank.BULLET_CSV_COLUMNS) - 1)
        table_rows = [
            (2024, "facelift", 10, 1000, 2000, "national", "https://example.test/2024", moment, None, "older") + blank,
            (2024, "rhinoplasty", 11, 1100, 2100, "national", "https://example.test/2024", moment, headers, "older") + lists,
            (2025, "facelift", 12, None, None, "national", "https://example.test/2025", moment, None, "latest") + blank,
        ]
        conn = self._connection(columns, table_rows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "procedure_surgeon_fees.csv"
            count = rank.export_procedure_fees(conn, path)
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(
                    list(reader.fieldnames),
                    [name for name in columns if name not in rank.BULLET_CSV_COLUMNS]
                    + [
                        "marketing_bullets",
                        "rank",
                        "city",
                        "state",
                        "zip_code",
                        "practice_zip_code",
                        "county",
                        "distance_from_practice_miles",
                        "insight",
                        "household_income",
                        "individual_income",
                        "median_listing_price_per_square_foot",
                        "age_range",
                        "sex",
                        "race_and_ethnicity",
                        "education_level",
                        *rank.DESCRIPTIVE_LIST_NAMES,
                        "composite_score",
                        *rank.SCORE_COLUMNS,
                    ],
                )
                written = list(reader)
        self.assertEqual(count, 4)
        self.assertEqual(
            [(row["report_year"], row["procedure"]) for row in written],
            [("2024", "facelift"), ("2024", "rhinoplasty"), ("2024", "rhinoplasty"), ("2025", "facelift")],
        )
        self.assertEqual(
            [
                (row["marketing_bullets"], row["rank"], row["city"], row["state"], row["zip_code"], row["insight"])
                for row in written
            ],
            [
                ("", "", "", "", "", ""),
                ("25–44. Ranked.", "1", "marina del rey", "ca", "90292", "Young."),
                ("45–64. Ranked.", "1", "pasadena", "ca", "90012", "Thirties."),
                ("", "", "", "", "", ""),
            ],
        )
        self.assertEqual(
            [
                (
                    row["household_income"],
                    row["individual_income"],
                    row["median_listing_price_per_square_foot"],
                    row["age_range"],
                    row["sex"],
                    row["race_and_ethnicity"],
                    row["education_level"],
                )
                for row in written
            ],
            [
                ("", "", "", "", "", "", ""),
                ("146167", "64075", "858", "25–44", "woman", "Hispanic or Latino", "Bachelor's degree"),
                ("98000", "52010", "612.5", "25–44", "", "", ""),
                ("", "", "", "", "", "", ""),
            ],
        )
        self.assertEqual(written[1]["surgeon_fee_low"], "1100")
        self.assertEqual(written[2]["surgeon_fee_high"], "2100")
        self.assertEqual(written[2]["source_note"], "older")
        self.assertEqual(written[0]["dataset_updated_at"], moment.isoformat())
        self.assertEqual(written[3]["surgeon_fee_low"], "")
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

    def test_export_refuses_lists_that_do_not_match_the_headers(self):
        columns = ["report_year", "procedure", "marketing_bullets", "rank", "zip_code"]
        rows = [(2024, "facelift", "Header one.\nHeader two.", [1], ["90012", "90013"])]
        with self.assertRaises(SystemExit):
            rank.expand_marketing_bullet_rows(columns, rows)

    def test_export_refuses_old_text_without_zip_rows(self):
        columns = ["report_year", "procedure", "marketing_bullets"]
        rows = [(2024, "facelift", "25–44. Header.\n- 1. pasadena, ca (90012): cash.")]
        with self.assertRaises(SystemExit):
            rank.expand_marketing_bullet_rows(columns, rows)

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
