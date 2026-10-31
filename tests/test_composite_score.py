"""Composite score weights, percentile ranks, and per-procedure ranking."""

from __future__ import annotations

import unittest
from decimal import Decimal

import discretionary
import ranking_weights as weights
import rank_cash_pay_zips as rank

LIVING = Decimal(60000)
PERSONA = rank.PERSONA_BY_LABEL["25–44"]


def scored_row(zip_code: str, age_share: str, women: int, owners: int, managers: int) -> dict:
    profile = {
        "female_population_25_44": women,
        "male_population_25_44": 100 - women,
        "owner_occupied_households": 50,
        "renter_occupied_households": 50,
        "owner_occupied_householder_25_44": owners,
        "renter_occupied_householder_25_44": 100 - owners,
        "employed_women": 100,
        "women_management_business_science_arts": managers,
        "women_arts_design_entertainment_sports_media": 5,
    }
    return {
        "zip": zip_code,
        "zip_name": f"place {zip_code}, ca",
        "age_share": Decimal(age_share),
        "adult_population": 1000,
        "median_household_income": 200000,
        "per_capita_income": 80000,
        "median_listing_price_per_square_foot": Decimal(700),
        "female_share": None,
        "profile": profile,
        "cost_of_living": LIVING,
        **discretionary.household_fields(200000, LIVING, "25_44"),
    }


def screen(passed: list[dict], dropped: list[dict] | None = None) -> rank.PersonaRank:
    return rank.PersonaRank(
        PERSONA,
        Decimal(100000),
        Decimal(50000),
        Decimal(600),
        passed,
        passed,
        passed + (dropped or []),
    )


class WeightTests(unittest.TestCase):
    def test_every_procedure_has_weights_that_add_up_to_one(self):
        for procedure in rank.PROCEDURE_PERSONAS:
            for priced in (True, False):
                found = weights.weights(procedure, priced)
                self.assertAlmostEqual(float(sum(found.values())), 1.0, places=9)
                self.assertEqual(max(found, key=found.get), "age_share")
                self.assertEqual(list(found), [f for f in weights.FACTORS if f in found])

    def test_defaults(self):
        self.assertEqual(weights.AGE_SHARE_WEIGHT, Decimal("0.30"))
        raw = weights.raw_weights("breast augmentation", True)
        self.assertEqual(
            raw,
            {
                "age_share": Decimal("0.30"),
                "women_share_of_age_range": Decimal("0.20"),
                "affordable_households_share": Decimal("0.15"),
                "affordable_households": Decimal("0.10"),
                "mothers_with_children_under_18": Decimal("0.04"),
                "mothers_with_children_under_6": Decimal("0.03"),
                "recent_births": Decimal("0.03"),
                "owner_occupied_share_age_range": Decimal("0.10"),
                "women_management_professional_share": Decimal("0.10"),
            },
        )
        women = {
            name: weights.raw_weights(name, True)["women_share_of_age_range"]
            for name in ("breast lift", "tummy tuck", "rhinoplasty", "neck lift")
        }
        self.assertGreater(women["breast lift"], women["tummy tuck"])
        self.assertGreater(women["tummy tuck"], women["rhinoplasty"])
        self.assertEqual(women["rhinoplasty"], women["neck lift"])

    def test_mother_and_arts_factors_apply_only_to_their_procedures(self):
        for procedure in rank.PROCEDURE_PERSONAS:
            raw = weights.raw_weights(procedure, True)
            self.assertEqual(
                "recent_births" in raw, procedure in weights.MOTHER_PROCEDURES, procedure
            )
            self.assertEqual(
                "women_arts_entertainment_media_share" in raw,
                procedure in weights.FACE_PROCEDURES,
                procedure,
            )
        self.assertIn("rhinoplasty", weights.FACE_PROCEDURES)

    def test_no_fee_drops_the_affordability_factors(self):
        found = weights.raw_weights("eyelid surgery", False)
        self.assertNotIn("affordable_households_share", found)
        self.assertNotIn("affordable_households", found)

    def test_race_ethnicity_and_education_are_never_factors(self):
        for factor in weights.FACTORS:
            for word in ("race", "ethnic", "hispanic", "white", "black", "asian", "education"):
                self.assertNotIn(word, factor)
        self.assertEqual(rank.SCORE_COLUMNS, tuple(f"score_{f}" for f in weights.FACTORS))


class PercentileTests(unittest.TestCase):
    def test_ties_share_the_middle_and_blanks_are_neutral(self):
        found = rank.percentile_ranks(
            {"a": Decimal(1), "b": Decimal(2), "c": Decimal(2), "d": Decimal(5), "e": None}
        )
        self.assertEqual(found["a"], 0)
        self.assertEqual(found["b"], found["c"])
        self.assertEqual(found["b"], Decimal("0.5"))
        self.assertEqual(found["d"], 1)
        self.assertEqual(found["e"], Decimal("0.5"))
        self.assertEqual(rank.percentile_ranks({"a": Decimal(3)}), {"a": Decimal("0.5")})


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.young = scored_row("90001", "0.5", women=10, owners=10, managers=10)
        self.women = scored_row("90002", "0.3", women=90, owners=90, managers=90)

    def test_the_procedure_weights_change_the_order(self):
        listed = screen([self.young, self.women])
        nose = rank.score_screen("rhinoplasty", listed)
        breast = rank.score_screen("breast augmentation", listed)
        self.assertEqual([row["zip"] for row in nose], ["90001", "90002"])
        self.assertEqual([row["zip"] for row in breast], ["90002", "90001"])
        self.assertEqual([row["rank"] for row in breast], [1, 2])
        self.assertEqual(nose[0]["composite_score"], Decimal("54.2"))
        self.assertEqual(breast[0]["composite_score"], Decimal("56.3"))
        self.assertEqual(breast[0]["score_women_share_of_age_range"], Decimal("25.00"))
        self.assertEqual(breast[0]["score_mothers_with_children_under_18"], Decimal("2.50"))
        self.assertIsNone(breast[0]["score_women_arts_entertainment_media_share"])
        self.assertIsNone(breast[0]["score_affordable_households"])
        for row in nose + breast:
            parts = sum(row[name] for name in rank.SCORE_COLUMNS if row[name] is not None)
            self.assertLessEqual(abs(parts - row["composite_score"]), Decimal("0.1"))
            self.assertEqual(row["procedure"] in ("rhinoplasty", "breast augmentation"), True)
            self.assertEqual(row["persona"], "25–44")

    def test_percentiles_use_every_eligible_zip_including_dropped_ones(self):
        dropped = scored_row("90003", "0.9", women=50, owners=50, managers=50)
        found = rank.score_screen("rhinoplasty", screen([self.young, self.women], [dropped]))
        self.assertEqual({row["zip"] for row in found}, {"90001", "90002"})
        young = next(row for row in found if row["zip"] == "90001")
        self.assertEqual(young["percentiles"]["age_share"], Decimal("0.5"))
        self.assertEqual(young["score_age_share"], Decimal("25.00"))

    def test_the_affordability_screen_comes_before_the_score(self):
        poor = scored_row("90004", "0.9", women=90, owners=90, managers=90)
        poor.update(discretionary.household_fields(50000, LIVING, "25_44"))
        found = rank.score_screen("tummy tuck", screen([self.young, poor]), Decimal(10000))
        self.assertEqual([row["zip"] for row in found], ["90001"])
        self.assertIsNotNone(found[0]["score_affordable_households_share"])
        self.assertEqual(found[0]["surgeon_fee_midpoint"], Decimal(10000))

    def test_insight_names_the_top_drivers_and_the_header_the_weights(self):
        bullets = rank.render_screen("breast augmentation", screen([self.young, self.women]))
        self.assertTrue(
            bullets[0].insight.startswith(
                "Why Place 90002 (90002): it ranks 1st in this list with a score of 56.3 out of 100, "
                "driven most by a high share of women among adults ages 25 to 44 (the highest within "
                "50 miles), high home ownership among households headed by someone 25–44 (the highest "
                "within 50 miles), and many women in management and professional jobs (the highest "
                "within 50 miles). 30.0% of adults here are ages 25 to 44."
            ),
            bullets[0].insight,
        )
        self.assertIn("driven most by a large share of adults ages 25 to 44", bullets[1].insight)
        self.assertIn(
            "ZIP codes are ranked by a score from 0 to 100 that compares each one with the other ZIP "
            "codes within 50 miles on these weighted measures: share of adults ages 25 to 44 (38%), "
            "women's share of adults ages 25 to 44 (25%),",
            bullets[0].header,
        )
        self.assertNotIn("race", bullets[0].header.split("Only ZIP codes")[0].lower())
        self.assertEqual(bullets[0].composite_score, Decimal("56.3"))
        self.assertEqual(bullets[0].value("score_women_share_of_age_range"), Decimal("25.00"))

    def test_rank_rows_carry_the_procedure_and_every_measure(self):
        lists = {"rhinoplasty": [(screen([self.young, self.women]), rank.score_screen("rhinoplasty", screen([self.young, self.women])))]}
        for row in lists["rhinoplasty"][0][1]:
            row.update(practice_zip_code="90210", distance_from_practice_miles=Decimal("8.5"))
        rows = rank.flatten(lists)
        self.assertEqual([row["procedure"] for row in rows], ["rhinoplasty", "rhinoplasty"])
        for name in rank.MEASURE_COLUMNS:
            self.assertIn(name, rows[0])
        self.assertEqual(rows[0]["women_share_of_age_range"], Decimal("10.0"))
        self.assertEqual(rows[0]["owner_occupied_share_age_range"], Decimal("10.0"))
        self.assertEqual(rank.COLUMN_TYPES["composite_score"], "numeric")
        self.assertEqual(rank.COLUMN_TYPES["women_in_age_range"], "integer")
        self.assertEqual(rank.COLUMN_TYPES["home_ownership"], "text")


if __name__ == "__main__":
    unittest.main()
