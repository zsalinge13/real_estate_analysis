"""The practice ZIP, the LA and Orange County area, and the 50-mile cutoff."""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from decimal import Decimal

import join_census_redfin as join
import load_california
import practice
import pull_census_zip_population as census
import rank_cash_pay_zips as rank
from tests.test_cash_pay_rank import join_row
from tests.test_census_redfin_join import census_row, redfin_row

HEADER = "GEOID_ZCTA5_20|GEOID_COUNTY_20|AREALAND_PART|AREAWATER_PART"


def crosswalk(*parts: tuple[str, str, int, int]) -> str:
    return "\n".join([HEADER] + [f"{z}|{c}|{land}|{water}" for z, c, land, water in parts])


class SettingTests(unittest.TestCase):
    def test_one_setting_for_the_practice(self):
        self.assertEqual(practice.PRACTICE_ZIP_CODE, "90210")
        self.assertEqual(practice.MAX_DISTANCE_MILES, Decimal(50))
        self.assertEqual(practice.COUNTIES, {"06037": "Los Angeles", "06059": "Orange"})
        self.assertEqual(practice.FIELDS, ("practice_zip_code", "county", "distance_from_practice_miles"))
        for module in (load_california, census, join, rank):
            self.assertNotIn('"90210"', inspect.getsource(module))

    def test_majority_land_picks_one_county_and_ties_are_left_out(self):
        text = crosswalk(
            ("90210", "06037", 100, 0),
            ("92618", "06059", 90, 0),
            ("92618", "06037", 10, 0),
            ("90631", "06037", 40, 0),
            ("90631", "06059", 60, 0),
            ("91766", "06037", 30, 0),
            ("91766", "06071", 70, 0),
            ("90000", "06037", 50, 5),
            ("90000", "06059", 50, 5),
            ("90001", "06037", 50, 6),
            ("90001", "06059", 50, 5),
        )
        self.assertEqual(
            practice.majority_counties(text),
            {"90210": "Los Angeles", "92618": "Orange", "90631": "Orange", "90001": "Los Angeles"},
        )

    def test_haversine_distance_and_the_cutoff_is_inclusive(self):
        points = {"90210": (34.0, -118.0), "a": (34.0, -118.0), "b": (34.0, -117.0)}
        degree = practice.miles_between(points["90210"], points["b"])
        self.assertAlmostEqual(degree, 57.28, places=2)
        self.assertEqual(practice.distance_miles(points["90210"], points["90210"]), Decimal("0.00"))
        lat_step = 50 / 69.0933
        points["edge"] = (34.0 + lat_step, -118.0)
        points["past"] = (34.0 + lat_step * 1.001, -118.0)
        counties = {"90210": "Los Angeles", "edge": "Orange", "past": "Orange", "b": "Orange", "nopoint": "Orange"}
        area = practice.area_from(counties, points)
        self.assertEqual(area["edge"].distance_from_practice_miles, Decimal("50.00"))
        self.assertEqual(set(area), {"90210", "edge"})
        self.assertEqual(area["90210"].fields(), ("90210", "Los Angeles", Decimal("0.00")))
        with self.assertRaises(SystemExit):
            practice.area_from({"92618": "Orange"}, {"92618": (33.6, -117.7)})

    def test_in_area_checks_the_practice_county_and_distance(self):
        self.assertTrue(practice.in_area("90210", "Orange", "50.00"))
        self.assertFalse(practice.in_area("90210", "Orange", "50.01"))
        self.assertFalse(practice.in_area("90211", "Orange", "1"))
        self.assertFalse(practice.in_area("90210", "San Bernardino", "1"))
        self.assertFalse(practice.in_area("90210", "Orange", ""))
        self.assertFalse(practice.in_area("", "", ""))


class LoadTests(unittest.TestCase):
    def test_redfin_rows_carry_the_practice_fields(self):
        self.assertEqual(load_california.LOAD_COLUMNS[-3:], practice.FIELDS)
        self.assertEqual(load_california.COLUMN_TYPES["distance_from_practice_miles"], "numeric")
        for name in practice.FIELDS:
            self.assertIn(name, load_california.STORED_COLUMNS)

    def test_census_year_load_prunes_outside_zips_and_keeps_area_months(self):
        class Cursor:
            def __init__(self):
                self.statements = []
                self.rowcount = 0

            def execute(self, sql, params=None):
                self.statements.append((" ".join(sql.split()), params))
                self.rowcount = 7 if sql.startswith("DELETE") else 1

        cur = Cursor()
        area = {
            "90210": practice.AreaZip("Los Angeles", Decimal("0.00")),
            "92618": practice.AreaZip("Orange", Decimal("49.21")),
        }
        stamp = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(load_california.prune_and_refresh_area(cur, area, stamp), (7, 2))
        delete_sql, delete_params = cur.statements[0]
        self.assertEqual(
            delete_sql, "DELETE FROM public.redfin_real_estate_value WHERE NOT (postal_code = ANY(%s))"
        )
        self.assertEqual(delete_params, (["90210", "92618"],))
        self.assertNotIn("month_date_yyyymm", delete_sql)
        update_sql, params = cur.statements[2]
        self.assertTrue(update_sql.startswith("UPDATE public.redfin_real_estate_value SET practice_zip_code"))
        self.assertEqual(params[:5], ("90210", "Orange", Decimal("49.21"), stamp, "92618"))
        self.assertNotIn("DELETE", load_california.census_year_upsert_sql().upper())


class JoinTests(unittest.TestCase):
    def test_join_keeps_only_area_rows_and_carries_the_fields(self):
        census_rows = [census_row(code, 2024, "1") for code in ("90210", "92618", "93535", "92676", "91766")]
        redfin_rows = [
            redfin_row("90210", "2024_12"),
            dict(redfin_row("92618", "2024_12"), county="Orange", distance_from_practice_miles="49.21"),
            dict(redfin_row("93535", "2024_12"), distance_from_practice_miles="51.00"),
            dict(redfin_row("92676", "2024_12"), county="Orange", practice_zip_code="92618"),
            dict(redfin_row("91766", "2024_12"), county="San Bernardino"),
        ]
        _year, _month, joined = join.join_records(census_rows, redfin_rows)
        self.assertEqual([row["zip"] for row in joined], ["90210", "92618"])
        self.assertEqual(
            [tuple(row[name] for name in practice.FIELDS) for row in joined],
            [("90210", "Los Angeles", "8.50"), ("90210", "Orange", "49.21")],
        )
        self.assertEqual(join.column_sql_type("county"), "text")
        self.assertEqual(join.column_sql_type("distance_from_practice_miles"), "numeric")

    def test_old_redfin_file_without_the_fields_is_refused(self):
        self.assertEqual(set(practice.FIELDS) - set(join.JOIN_COLUMNS), set())


class RankTests(unittest.TestCase):
    def test_outside_rows_never_rank_or_move_the_medians(self):
        rows = [
            join_row("90210", median_household_income="100000", population_18_34="400"),
            join_row(
                "92618",
                county="Orange",
                distance_from_practice_miles="49.21",
                median_household_income="300000",
                population_18_34="300",
            ),
        ]
        far = join_row(
            "92676",
            county="Orange",
            distance_from_practice_miles="51.79",
            median_household_income="900000",
            population_18_34="900",
        )
        inside = rank.rank_personas(rows)
        with_far = rank.rank_personas(rows + [far])
        young = next(screen for screen in with_far if screen.persona.label == "18 to 34")
        self.assertEqual(young.household_income_middle, Decimal(200000))
        self.assertEqual([row["zip"] for row in young.rows], ["92618"])
        self.assertEqual(young.rows[0]["county"], "Orange")
        self.assertEqual(
            [(s.household_income_middle, [r["zip"] for r in s.rows]) for s in inside],
            [(s.household_income_middle, [r["zip"] for r in s.rows]) for s in with_far],
        )
        bullet = rank.bullet_rows("rhinoplasty", with_far)[0]
        self.assertEqual(
            (bullet.practice_zip_code, bullet.county, bullet.distance_from_practice_miles),
            ("90210", "Orange", Decimal("49.21")),
        )
        self.assertIn("medians of ZIP codes within 50 miles of the practice, $200,000", bullet.header)
        self.assertIn("the median of ZIP codes within 50 miles of the practice.", bullet.insight)
        self.assertNotIn("Los Angeles County", bullet.header + bullet.insight)
        self.assertEqual(rank.RANK_COLUMNS[4:7], practice.FIELDS)


class CensusTests(unittest.TestCase):
    def test_census_keeps_home_value_zips_in_the_area(self):
        area = {"90210": practice.AreaZip("Los Angeles", Decimal(0)), "92618": practice.AreaZip("Orange", Decimal("49.21"))}
        self.assertEqual(census.select_zips({"90210", "92618", "92676", "94102"}, area), {"90210", "92618"})
        self.assertIn("area_zips = area_home_value_zips()", inspect.getsource(census.main))


if __name__ == "__main__":
    unittest.main()
