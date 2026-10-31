"""Checks for the competition and search-trends dataset."""

from __future__ import annotations

import csv
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import practice
import pull_competition_trends as ct

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 2, 18, 43, tzinfo=timezone.utc)


def result(npi, postal, codes, status="A", kind="NPI-1", purpose="LOCATION"):
    return {
        "number": npi,
        "enumeration_type": kind,
        "basic": {"status": status},
        "addresses": [
            {"address_purpose": "MAILING", "postal_code": "100010000"},
            {"address_purpose": purpose, "postal_code": postal},
        ],
        "taxonomies": [{"code": code} for code in codes],
    }


class FakeTrends:
    def __init__(self, weeks=260, fail_weekly=False, fail_cities=False):
        self.weeks = weeks
        self.fail_weekly = fail_weekly
        self.fail_cities = fail_cities
        self.weekly_calls: list[list[str]] = []
        self.city_calls: list[str] = []

    def weekly(self, terms):
        self.weekly_calls.append(list(terms))
        if self.fail_weekly:
            raise RuntimeError("429")
        batch = len(self.weekly_calls)
        out = {}
        for term in terms:
            if term == ct.TRENDS_ANCHOR:
                out[term] = [50.0 / batch] * self.weeks
            else:
                out[term] = [10.0 / batch] * (self.weeks - 52) + [20.0 / batch] * 52
        return out

    def by_city(self, term):
        self.city_calls.append(term)
        if self.fail_cities:
            raise RuntimeError("429")
        return {"Beverly Hills": 100, "Los Angeles": 13, "Walnut": 0}


class TaxonomyTests(unittest.TestCase):
    def provider(self, *codes):
        return ct.Provider("1", "90210", set(codes)).specialties()

    def test_plastic_and_dermatology_prefixes(self):
        self.assertEqual(self.provider("208200000X"), {"plastic_surgeon"})
        self.assertEqual(self.provider("2082S0099X"), {"plastic_surgeon"})
        self.assertEqual(self.provider("207ND0101X"), {"dermatologist"})
        self.assertEqual(self.provider("207YS0123X"), {"facial_plastic_surgeon"})
        self.assertEqual(self.provider("207WX0200X"), {"oculoplastic_surgeon"})

    def test_lookalike_codes_are_not_counted(self):
        self.assertEqual(self.provider("163WS0121X"), set())
        self.assertEqual(self.provider("2086S0122X"), set())
        self.assertEqual(self.provider("207W00000X", "207Y00000X"), set())

    def test_any_listed_taxonomy_counts(self):
        self.assertEqual(
            self.provider("207Q00000X", "208200000X", "207YS0123X"),
            {"plastic_surgeon", "facial_plastic_surgeon"},
        )


class NppesTests(unittest.TestCase):
    def test_only_active_individuals_at_practice_location(self):
        self.assertEqual(
            ct.provider_from_result(result("1", "902106134", ["208200000X"])).zip_code,
            "90210",
        )
        self.assertIsNone(ct.provider_from_result(result("2", "90210", ["208200000X"], status="D")))
        self.assertIsNone(ct.provider_from_result(result("3", "90210", ["208200000X"], kind="NPI-2")))
        self.assertIsNone(
            ct.provider_from_result(result("4", "90210", ["208200000X"], purpose="MAILING"))
        )

    def test_providers_merge_across_queries_and_prefixes(self):
        calls = []

        def fetch(params):
            calls.append(params)
            if params["skip"]:
                return {"results": []}
            if params["taxonomy_description"] == "Plastic":
                return {"results": [result("1", "90210", ["208200000X"])]}
            return {"results": [result("1", "90210", ["207N00000X"])]}

        providers = ct.fetch_providers(["90210", "90211", "91101"], fetch, lambda: None)
        self.assertEqual(
            sorted({(c["taxonomy_description"], c["postal_code"]) for c in calls}),
            [("Dermatology", "902*"), ("Dermatology", "911*"), ("Plastic", "902*"), ("Plastic", "911*")],
        )
        self.assertEqual(list(providers), ["1"])
        self.assertEqual(providers["1"].specialties(), {"plastic_surgeon", "dermatologist"})
        self.assertTrue(all(c["state"] == "CA" and c["enumeration_type"] == "NPI-1" for c in calls))

    def test_prefix_at_the_api_cap_is_split(self):
        def fetch(params):
            prefix = params["postal_code"].rstrip("*")
            if prefix == "902":
                return {"results": [{}] * ct.NPPES_PAGE}
            if prefix == "9021" and params["skip"] == 0:
                return {"results": [result("9", "90210", ["208200000X"])]}
            return {"results": []}

        rows = ct.nppes_prefix("Plastic", "902", fetch, lambda: None)
        self.assertEqual([row["number"] for row in rows], ["9"])


class GeographyTests(unittest.TestCase):
    def test_gazetteer_parse_and_radius(self):
        text = (
            "GEOID\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG                 \n"
            "90210\t1\t0\t1\t0\t34.1030\t-118.4105\n"
            "90212\t1\t0\t1\t0\t34.0627\t-118.4020\n"
            "90265\t1\t0\t1\t0\t34.0630\t-118.8500\n"
        )
        points = practice.parse_gazetteer(text)
        self.assertEqual(points["90212"], (34.0627, -118.402))
        near = ct.neighbors_within(["90210", "90212", "90265", "99999"], points)
        self.assertEqual(near["90210"], ["90210", "90212"])
        self.assertEqual(near["90265"], ["90265"])
        self.assertNotIn("99999", near)
        self.assertAlmostEqual(practice.miles_between(points["90210"], points["90212"]), 2.85, places=1)

    def test_neighbors_cross_the_county_line_and_leave_the_join(self):
        points = {
            "90720": (33.7951, -118.0647),
            "90740": (33.7562, -118.0809),
            "90803": (33.7614, -118.1323),
            "93063": (34.3070, -118.6860),
        }
        near = ct.neighbors_within(["90803"], points)
        self.assertEqual(near["90803"], ["90720", "90740", "90803"])
        self.assertEqual(ct.outside_neighbors(near, ["90803", "90720"]), ["90740"])

    def test_radius_edge_uses_the_haversine_distance(self):
        north = (34.0 + 4.99 / 69.09, -118.0)
        points = {"a": (34.0, -118.0), "b": north, "c": (34.0 + 5.01 / 69.09, -118.0)}
        self.assertEqual(ct.neighbors_within(["a"], points)["a"], ["a", "b"])


def join_input(zip_code, county="Los Angeles", distance="8.50", practice_zip="90210", adults="1000"):
    return {
        "zip": zip_code,
        "zip_name": "irvine, ca" if county == "Orange" else "beverly hills, ca",
        "adult_population": adults,
        "acs_year": "2024",
        "practice_zip_code": practice_zip,
        "county": county,
        "distance_from_practice_miles": distance,
    }


class JoinInputTests(unittest.TestCase):
    def test_only_practice_area_rows_are_kept(self):
        areas = ct.zip_areas_from_rows(
            [
                join_input("90210", distance="0.00"),
                join_input("92618", county="Orange", distance="49.21"),
                join_input("92630", county="Orange", distance="51.79"),
                join_input("93063", county="Ventura", distance="30.00"),
                join_input("90211", practice_zip="90001"),
                join_input("90212", distance=""),
            ]
        )
        self.assertEqual([area.zip_code for area in areas], ["90210", "92618"])
        irvine = areas[1]
        self.assertEqual(
            (irvine.city, irvine.practice_zip_code, irvine.county, irvine.distance_from_practice_miles),
            ("irvine", "90210", "Orange", Decimal("49.21")),
        )
        self.assertEqual(ct.join_acs_year(areas), 2024)

    def test_no_area_rows_stops_the_run(self):
        with self.assertRaises(SystemExit):
            ct.zip_areas_from_rows([join_input("92630", county="Orange", distance="51.79")])

    def test_join_csv_needs_the_practice_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "join.csv"
            path.write_text("zip,zip_name,adult_population,acs_year\n90210,beverly hills,1,2024\n")
            with self.assertRaises(SystemExit) as caught:
                ct.read_zip_areas(path)
            self.assertIn("practice_zip_code", str(caught.exception))


class OutsideAdultsTests(unittest.TestCase):
    def record(self, zip_code, value="10"):
        out = {ct.census.GEO_COLUMN: zip_code}
        for codes in ct.census.band_codes().values():
            for code in codes:
                out[code] = value
        return out

    def test_adults_sum_every_band_and_missing_is_blank(self):
        calls = []

        def fetch(year, zips, variables):
            calls.append((year, list(zips), len(variables)))
            return [self.record(z, "-666666666" if z == "90740" else "10") for z in zips]

        zips = [f"9{index:04d}" for index in range(11)] + ["90740"]
        adults = ct.fetch_adults(2024, zips, fetch, lambda: None)
        bands = sum(len(codes) for codes in ct.census.band_codes().values())
        self.assertEqual(adults["90000"], 10 * bands)
        self.assertIsNone(adults["90740"])
        self.assertEqual([len(z) for _y, z, _n in calls], [10, 2])
        self.assertTrue(all(year == 2024 and n == bands for year, _z, n in calls))


class CompetitionTests(unittest.TestCase):
    def setUp(self):
        self.areas = [
            ct.ZipArea("90210", "beverly hills", 20000, "Los Angeles", Decimal("0.00")),
            ct.ZipArea("90212", "beverly hills", 10000, "Los Angeles", Decimal("2.85")),
            ct.ZipArea("90265", "malibu", None, "Los Angeles", Decimal("25.50")),
        ]
        self.points = {"90210": (34.103, -118.4105), "90212": (34.0627, -118.402), "90265": (34.063, -118.85)}
        self.providers = {
            "1": ct.Provider("1", "90210", {"208200000X"}),
            "2": ct.Provider("2", "90212", {"208200000X", "207YS0123X"}),
            "3": ct.Provider("3", "90212", {"207WX0200X"}),
            "4": ct.Provider("4", "90212", {"207N00000X"}),
            "5": ct.Provider("5", "92660", {"208200000X"}),
        }

    def rows(self, trends=None):
        comp = ct.competition_by_zip(self.areas, self.providers, self.points)
        return ct.build_rows(
            self.areas, ["eyelid surgery", "liposuction"], comp, trends or {}, NOW, NOW
        )

    def test_grain_is_zip_by_procedure_in_column_order(self):
        rows = self.rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({(r["zip_code"], r["procedure"]) for r in rows}), 6)
        self.assertTrue(all(tuple(r) == ct.COLUMN_NAMES for r in rows))

    def test_counts_radius_and_density(self):
        row = next(r for r in self.rows() if r["zip_code"] == "90210" and r["procedure"] == "liposuction")
        self.assertEqual(row["plastic_surgeon_count"], 1)
        self.assertEqual(row["plastic_surgeon_count_5mi"], 2)
        self.assertEqual(row["dermatologist_count_5mi"], 1)
        self.assertEqual(row["adult_population_5mi"], 30000)
        self.assertEqual(row["plastic_surgeons_per_10k_adults"], Decimal("0.50"))
        self.assertEqual(row["plastic_surgeons_per_10k_adults_5mi"], Decimal("0.67"))
        self.assertEqual(row["procedure_competitor_specialties"], "plastic surgery")
        self.assertEqual(row["procedure_competitor_count_5mi"], 2)

    def test_face_procedures_add_facial_and_oculoplastic_once_per_npi(self):
        row = next(r for r in self.rows() if r["zip_code"] == "90212" and r["procedure"] == "eyelid surgery")
        self.assertEqual(
            row["procedure_competitor_specialties"],
            "plastic surgery, facial plastic surgery, ophthalmic plastic surgery",
        )
        self.assertEqual(row["procedure_competitor_count"], 2)
        self.assertEqual(row["procedure_competitor_count_5mi"], 3)

    def test_blank_adults_give_blank_density(self):
        row = next(r for r in self.rows() if r["zip_code"] == "90265")
        self.assertIsNone(row["plastic_surgeons_per_10k_adults"])
        self.assertEqual(row["plastic_surgeon_count_5mi"], 0)
        self.assertIsNone(row["adult_population_5mi"])

    def test_practice_fields_follow_city(self):
        row = next(r for r in self.rows() if r["zip_code"] == "90212")
        self.assertEqual(ct.COLUMN_NAMES[1:6], ("city", *practice.FIELDS, "procedure"))
        self.assertEqual(
            (row["practice_zip_code"], row["county"], row["distance_from_practice_miles"]),
            ("90210", "Los Angeles", Decimal("2.85")),
        )

    def test_neighbors_outside_the_join_add_providers_and_adults(self):
        self.points["90211"] = (34.0652, -118.3833)
        self.providers["6"] = ct.Provider("6", "90211", {"208200000X"})
        comp = ct.competition_by_zip(self.areas, self.providers, self.points, {"90211": 5000})
        rows = ct.build_rows(self.areas, ["liposuction"], comp, {}, NOW, NOW)
        row = next(r for r in rows if r["zip_code"] == "90210")
        self.assertEqual(row["plastic_surgeon_count"], 1)
        self.assertEqual(row["plastic_surgeon_count_5mi"], 3)
        self.assertEqual(row["adult_population_5mi"], 35000)
        self.assertEqual(row["plastic_surgeons_per_10k_adults_5mi"], Decimal("0.86"))
        self.assertNotIn("90211", {r["zip_code"] for r in rows})

    def test_unknown_outside_adults_blank_the_5_mile_density(self):
        self.points["90211"] = (34.0652, -118.3833)
        for outside in ({}, {"90211": None}):
            comp = ct.competition_by_zip(self.areas, self.providers, self.points, outside)
            row = next(
                r
                for r in ct.build_rows(self.areas, ["liposuction"], comp, {}, NOW, NOW)
                if r["zip_code"] == "90210"
            )
            self.assertEqual(row["plastic_surgeon_count_5mi"], 2)
            self.assertIsNone(row["adult_population_5mi"])
            self.assertIsNone(row["plastic_surgeons_per_10k_adults_5mi"])
            self.assertIsNone(row["procedure_competitors_per_10k_adults_5mi"])
            self.assertEqual(row["plastic_surgeons_per_10k_adults"], Decimal("0.50"))

    def test_limitations_drop_the_county_line_undercount(self):
        self.assertNotIn("Los Angeles County ZIPs in the join only", ct.COMPETITION_LIMITATIONS)
        self.assertIn("DMA 803", ct.COMPETITION_LIMITATIONS)
        self.assertEqual(ct.TRENDS_GEO, "US-CA-803")

    def test_zip_without_a_centroid_has_blank_radius_fields(self):
        self.points.pop("90265")
        row = next(r for r in self.rows() if r["zip_code"] == "90265")
        self.assertIsNone(row["plastic_surgeon_count_5mi"])
        self.assertEqual(row["plastic_surgeon_count"], 0)

    def test_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ct.OUTPUT_CSV_NAME
            ct.write_csv(self.rows(), path)
            with path.open(newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames), ct.COLUMN_NAMES)
                first = next(reader)
            self.assertEqual(first["zip_code"], "90210")
            self.assertEqual(first["dataset_updated_at"], NOW.isoformat())


class TrendsTests(unittest.TestCase):
    procedures = ["breast augmentation", "facelift", "liposuction", "rhinoplasty", "tummy tuck", "neck lift"]

    def test_fresh_pull_rescales_batches_through_the_anchor(self):
        source = FakeTrends()
        entries = ct.refresh_trends(self.procedures, {}, lambda: source, NOW)
        self.assertEqual(len(source.weekly_calls), 2)
        self.assertTrue(all(call[0] == ct.TRENDS_ANCHOR for call in source.weekly_calls))
        self.assertTrue(all(len(call) <= ct.TRENDS_BATCH for call in source.weekly_calls))
        for procedure in ("breast augmentation", "neck lift"):
            dma = entries[procedure].dma.values
            self.assertAlmostEqual(dma["latest_avg"], 20.0)
            self.assertAlmostEqual(dma["prior_avg"], 10.0)
            self.assertAlmostEqual(dma["change"], 1.0)
        self.assertEqual(entries["liposuction"].dma.values["change"], 0.0)
        self.assertEqual(entries["facelift"].dma_status, ct.STATUS_NEW)
        self.assertEqual(entries["facelift"].city.values, {"Beverly Hills": 100, "Los Angeles": 13})

    def test_failure_keeps_last_good_values_and_never_raises(self):
        old = NOW - timedelta(days=8)
        cache = {
            "facelift": ct.TrendsEntry(
                term="facelift",
                dma=ct.TrendsPart({"latest_avg": 40.0, "prior_avg": 32.0, "change": 0.25}, old.isoformat()),
                city=ct.TrendsPart({"Beverly Hills": 100}, old.isoformat()),
            )
        }
        entries = ct.refresh_trends(
            ["facelift", "rhinoplasty"], cache, lambda: FakeTrends(fail_weekly=True, fail_cities=True), NOW
        )
        self.assertEqual(entries["facelift"].dma_status, ct.STATUS_KEPT)
        self.assertEqual(entries["facelift"].dma.values["change"], 0.25)
        self.assertEqual(entries["facelift"].dma.pulled_at, old.isoformat())
        self.assertEqual(entries["rhinoplasty"].dma_status, ct.STATUS_UNAVAILABLE)
        self.assertIsNone(entries["rhinoplasty"].dma)

    def test_missing_pytrends_does_not_raise(self):
        def broken():
            raise ImportError("pytrends")

        entries = ct.refresh_trends(["facelift"], {}, broken, NOW)
        self.assertEqual(entries["facelift"].dma_status, ct.STATUS_UNAVAILABLE)

    def test_recent_pull_is_not_requested_again(self):
        recent = (NOW - timedelta(days=2)).isoformat()
        cache = {
            p: ct.TrendsEntry(
                term=ct.term_for(p),
                dma=ct.TrendsPart({"latest_avg": 1.0, "prior_avg": 1.0, "change": 0.0}, recent),
                city=ct.TrendsPart({}, recent),
            )
            for p in self.procedures
        }
        source = FakeTrends()
        entries = ct.refresh_trends(self.procedures, cache, lambda: source, NOW)
        self.assertEqual(source.weekly_calls, [])
        self.assertEqual(source.city_calls, [])
        self.assertEqual(entries["facelift"].dma_status, ct.STATUS_RECENT)

    def test_changed_search_term_is_not_carried_forward(self):
        cache = {
            "facelift": ct.TrendsEntry(
                term="face lift",
                dma=ct.TrendsPart({"latest_avg": 1.0}, (NOW - timedelta(days=1)).isoformat()),
            )
        }
        entries = ct.refresh_trends(["facelift"], cache, None, NOW)
        self.assertIsNone(entries["facelift"].dma)

    def test_short_history_gives_no_stats(self):
        self.assertIsNone(ct.dma_stats([1.0] * 103))
        self.assertEqual(ct.dma_stats([0.0] * 104)["change"], None)

    def test_cache_round_trip(self):
        entries = ct.refresh_trends(["facelift"], {}, lambda: FakeTrends(), NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ct.CACHE_JSON_NAME
            ct.save_cache(path, entries)
            loaded = ct.load_cache(path)
            json.loads(path.read_text())
        self.assertEqual(loaded["facelift"].dma.values, entries["facelift"].dma.values)
        self.assertEqual(loaded["facelift"].city.pulled_at, NOW.isoformat())

    def test_city_maps_by_name_and_low_volume_is_blank(self):
        entries = ct.refresh_trends(["facelift"], {}, lambda: FakeTrends(), NOW)
        self.assertEqual(ct.city_interest(entries["facelift"], "beverly hills"), ("Beverly Hills", 100))
        self.assertEqual(ct.city_interest(entries["facelift"], "walnut"), (None, None))
        self.assertEqual(ct.city_interest(entries["facelift"], "sherman oaks"), (None, None))

    def test_every_seed_procedure_has_a_search_term(self):
        with ct.PROCEDURE_SEED.open(newline="") as handle:
            seed = {row["procedure"] for row in csv.DictReader(handle)}
        self.assertEqual(seed, set(ct.TRENDS_TERMS))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/pull-competition-trends.yml").read_text()

    def test_weekly_schedule_without_a_chain(self):
        self.assertIn('cron: "43 18 * * 1"', self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("workflow_run:", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_artifacts_kept_90_days(self):
        for name in ("competition-trends", "competition-trends-cache"):
            self.assertRegex(self.workflow, rf"name: {name}\n\s+path: [^\n]+\n\s+retention-days: 90")

    def test_reads_the_join_and_fees_from_load_california(self):
        self.assertIn("--workflow load-california.yml", self.workflow)
        self.assertIn("-n zip-census-redfin-join", self.workflow)
        self.assertIn("-n procedure-surgeon-fees", self.workflow)
        self.assertIn("python export_supabase_backup.py competition_trends", self.workflow)

    def test_no_new_secrets(self):
        secrets = set(re.findall(r"secrets\.([A-Z_]+)", self.workflow))
        self.assertEqual(secrets, {"DATABASE_URL", "CENSUS_API_KEY"})

    def test_csv_and_cache_are_not_committed(self):
        ignored = (ROOT / ".gitignore").read_text().split()
        self.assertIn(ct.OUTPUT_CSV_NAME, ignored)
        self.assertIn(ct.CACHE_JSON_NAME, ignored)

    def test_backup_export_knows_the_table(self):
        import export_supabase_backup

        spec = export_supabase_backup.SPECS[ct.TABLE_NAME]
        self.assertEqual(spec.path, ct.OUTPUT_CSV)
        self.assertEqual(spec.order_by, ("zip_code", "procedure"))


if __name__ == "__main__":
    unittest.main()
