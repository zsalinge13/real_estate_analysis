"""Checks for the Census ZIP and national procedure loads."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

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


class CensusTests(unittest.TestCase):
    def test_zip_filter_keeps_the_overlap_only(self):
        kept = census.select_zips({"90001", "94102", "90210"}, {"90001", "90210", "91301"})
        self.assertEqual(kept, {"90001", "90210"})

    def test_new_vintage_only(self):
        self.assertEqual(census.vintages_to_insert([2023, 2024], set()), [2023, 2024])
        self.assertEqual(census.vintages_to_insert([2023, 2024], {2023}), [2024])
        self.assertEqual(census.vintages_to_insert([2024, 2025], {2023, 2024}), [2025])

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
        band_sum = sum(row[8:])
        self.assertEqual(row[7], band_sum)
        estimates["B01001_007E"] = None
        partial = census.row_from_estimates(2024, "90001", estimates)
        self.assertIsNone(partial[8])
        self.assertIsNone(partial[7])
        self.assertEqual(partial[5], 10)

    def test_variable_list_stays_within_the_api_limit(self):
        variables = census.estimate_variables()
        self.assertLessEqual(len(variables), 50)
        self.assertIn("B19013_001E", variables)
        self.assertIn("B19301_001E", variables)
        self.assertIn("B01002_001E", variables)

    def test_key_error_page_is_detected_without_printing_a_key(self):
        headers = {"X-DataWebAPI-KeyError": "1", "Location": "https://api.census.gov/data/missing_key.html"}
        self.assertTrue(census.looks_like_key_error(302, headers, b""))
        self.assertTrue(census.looks_like_key_error(200, {}, b"<title>Missing Key</title>"))
        url = census.census_data_url(2024, ("B19013_001E",), ["90001"])
        self.assertNotIn("key=", url)


if __name__ == "__main__":
    unittest.main()
