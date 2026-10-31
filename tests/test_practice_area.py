"""The practice ZIP, the LA and Orange County area, and the 50-mile cutoff."""

from __future__ import annotations

import contextlib
import csv
import inspect
import io
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

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
        self.assertEqual(practice.MAX_PRACTICE_DISTANCE_MILES, Decimal(50))
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

    def test_census_year_load_keeps_every_zip_and_refreshes_the_fields(self):
        class Cursor:
            def __init__(self):
                self.statements = []
                self.rowcount = 0

            def execute(self, sql, params=None):
                self.statements.append((" ".join(sql.split()), params))
                self.rowcount = 3

        cur = Cursor()
        locations = {
            "92618": practice.AreaZip("Orange", Decimal("49.21")),
            "90210": practice.AreaZip("Los Angeles", Decimal("0.00")),
            "96001": practice.AreaZip("", Decimal("478.12")),
        }
        stamp = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(load_california.refresh_practice_fields(cur, locations, stamp), 3)
        self.assertEqual(len(cur.statements), 1)
        sql, params = cur.statements[0]
        self.assertTrue(sql.startswith("UPDATE public.redfin_real_estate_value AS stored SET practice_zip_code"))
        self.assertNotIn("DELETE", sql.upper())
        self.assertIn("LEFT JOIN unnest", sql)
        self.assertEqual(
            params,
            (
                "90210",
                stamp,
                ["90210", "92618", "96001"],
                ["Los Angeles", "Orange", None],
                [Decimal("0.00"), Decimal("49.21"), Decimal("478.12")],
                "90210",
            ),
        )
        self.assertNotIn("DELETE", load_california.census_year_upsert_sql().upper())
        self.assertNotIn("DELETE FROM", inspect.getsource(load_california.upsert_census_year).upper())
        self.assertFalse(hasattr(load_california, "prune_and_refresh_area"))

    def test_vectorized_distances_match_the_scalar_haversine(self):
        points = {
            "90210": (34.1030, -118.4105),
            "92618": (33.6600, -117.7300),
            "93535": (34.6950, -117.8700),
            "96001": (40.5800, -122.4500),
        }
        names = {"90210": "Los Angeles", "92618": "Orange", "93535": "Los Angeles", "96001": ""}
        located = practice.locations_from(names, points)
        for zip_code, (lat, lon) in points.items():
            self.assertEqual(
                located[zip_code].distance_from_practice_miles,
                practice.distance_miles(points["90210"], (lat, lon)),
            )
        self.assertEqual(located["96001"].county, "")
        self.assertEqual(set(practice.area_from(names, points)), {"90210", "92618"})

    def test_california_fips_includes_counties_outside_the_area(self):
        text = crosswalk(
            ("90210", "06037", 100, 0),
            ("96001", "06089", 100, 0),
            ("10001", "36061", 100, 0),
        )
        fips = practice.majority_county_fips(text)
        self.assertEqual(fips.to_dict(), {"10001": "36061", "90210": "06037", "96001": "06089"})
        california = fips[fips.str.startswith(practice.CALIFORNIA_FIPS)]
        self.assertEqual(sorted(california.index), ["90210", "96001"])


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
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.csv"
            path.write_text("month_date_yyyymm,postal_code\n2024_12,90210\n")
            with self.assertRaises(SystemExit):
                join.read_redfin_csv(path)

    def test_statewide_redfin_file_is_cut_to_the_eligible_set_on_read(self):
        rows = [
            redfin_row("90210", "2024_12"),
            dict(redfin_row("92618", "2024_12"), county="Orange", distance_from_practice_miles="49.21"),
            dict(redfin_row("93535", "2024_12"), distance_from_practice_miles="51.00"),
            dict(redfin_row("96001", "2024_12", name="Redding, CA"), county="", distance_from_practice_miles="478.12"),
            dict(redfin_row("90090", "2024_12"), county="", distance_from_practice_miles=""),
            dict(redfin_row("91766", "2024_12"), county="", distance_from_practice_miles="31.40"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "california_zip_inventory.csv"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(load_california.LOAD_COLUMNS) + ["extra"])
                writer.writeheader()
                writer.writerows(dict(row, extra="x") for row in rows)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                kept = join.read_redfin_csv(path)
        self.assertEqual([row["postal_code"] for row in kept], ["90210", "92618"])
        self.assertEqual(set(kept[0]), set(load_california.LOAD_COLUMNS))
        self.assertIn("Redfin file: 6 California ZIPs. Kept 2 within 50 miles", out.getvalue())
        self.assertIn("dropped 4 before the join", out.getvalue())


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


class DistanceRuleTests(unittest.TestCase):
    def rows(self):
        return [
            join_row("90210", distance_from_practice_miles="0.00", population_18_34="400", population_45_54="200"),
            join_row(
                "92618", county="Orange", distance_from_practice_miles="49.21",
                median_household_income="300000", population_18_34="300", population_45_54="300",
            ),
            join_row("92602", county="Orange", distance_from_practice_miles="50.00", population_18_34="350"),
            join_row("93535", distance_from_practice_miles="51.00", population_18_34="900", population_45_54="900"),
            join_row("92676", county="Orange", distance_from_practice_miles="63.49", population_18_34="900"),
            join_row("92001", county="Orange", distance_from_practice_miles="", population_18_34="900"),
            join_row("91766", county="San Bernardino", distance_from_practice_miles="20.00", population_18_34="900"),
        ]

    def test_constant_and_reason(self):
        source = inspect.getsource(practice)
        self.assertIn("MAX_PRACTICE_DISTANCE_MILES = Decimal(50)", source)
        self.assertIn("Patients rarely drive more than 50 miles to a new practice that is still", source)
        self.assertIn("rarely drive more than 50 miles to a new practice", practice.DISTANCE_RULE)

    def test_split_counts_distance_separately(self):
        kept, far, other = practice.split_by_area(
            self.rows(),
            lambda row: tuple(row[name] for name in practice.FIELDS),
        )
        self.assertEqual([row["zip"] for row in kept], ["90210", "92618", "92602"])
        self.assertEqual((far, other), (3, 1))

    def test_filter_runs_before_screens_and_is_logged(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            screens = rank.rank_personas(self.rows())
        self.assertIn("Excluded 3 ZIPs more than 50 miles from the practice ZIP 90210 before any screen", output.getvalue())
        self.assertIn("Excluded 1 ZIPs outside Los Angeles, Orange", output.getvalue())
        considered = {row["zip"] for screen in screens for row in screen.passed + screen.rows}
        self.assertEqual(considered & {"93535", "92676", "92001", "91766"}, set())
        kept_only = rank.rank_personas(self.rows()[:3])
        self.assertEqual(
            [s.household_income_middle for s in screens], [s.household_income_middle for s in kept_only]
        )

    def test_no_output_row_is_over_50_miles(self):
        screens = rank.rank_personas(self.rows())
        ranked = rank.flatten(screens)
        self.assertTrue(ranked)
        for row in ranked:
            self.assertEqual(row["practice_zip_code"], "90210")
            self.assertLessEqual(row["distance_from_practice_miles"], Decimal(50))
            self.assertEqual(row["distance_from_practice_miles"].as_tuple().exponent, -1)
        self.assertEqual(
            {row["zip"]: row["distance_from_practice_miles"] for row in ranked}["92618"], Decimal("49.2")
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / rank.OUTPUT_CSV_NAME
            rank.write_rank_csv(ranked, path)
            with path.open(newline="") as handle:
                written = list(csv.DictReader(handle))
        self.assertTrue(all(r["practice_zip_code"] == "90210" for r in written))
        self.assertTrue(all(Decimal(r["distance_from_practice_miles"]) <= 50 for r in written))
        self.assertIn("49.2", {r["distance_from_practice_miles"] for r in written})
        fees = [row for name in rank.PROCEDURE_PERSONAS for row in rank.bullet_rows(name, screens)]
        self.assertTrue(fees)
        for row in fees:
            self.assertEqual(row.practice_zip_code, "90210")
            self.assertLessEqual(row.distance_from_practice_miles, Decimal(50))
            self.assertIn(practice.DISTANCE_RULE, row.header)

    def test_a_ranked_far_row_is_refused(self):
        screens = rank.rank_personas(self.rows())
        screens[0].rows[0]["distance_from_practice_miles"] = Decimal("50.01")
        with self.assertRaises(SystemExit):
            rank.flatten(screens)


class CensusTests(unittest.TestCase):
    def test_census_keeps_home_value_zips_in_the_area(self):
        area = {"90210": practice.AreaZip("Los Angeles", Decimal(0)), "92618": practice.AreaZip("Orange", Decimal("49.21"))}
        self.assertEqual(census.select_zips({"90210", "92618", "92676", "94102"}, area), {"90210", "92618"})
        self.assertIn("area_zips = area_home_value_zips(area)", inspect.getsource(census.main))


if __name__ == "__main__":
    unittest.main()
