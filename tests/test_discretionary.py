"""Taxes, cost of living, BLS shares, and the income_needed solver."""

from __future__ import annotations

import unittest
from decimal import Decimal

import discretionary as d
import pull_census_zip_population as census


class TaxTests(unittest.TestCase):
    def test_working_household_at_100000(self):
        federal = Decimal(2320) + (Decimal(70800) - 23200) * Decimal("0.12")
        payroll = Decimal(100000) * (Decimal("0.062") + Decimal("0.0145") + Decimal("0.011"))
        california_taxable = Decimal(100000 - 11080)
        california = (
            Decimal("215.12")
            + (Decimal(50998) - 21512) * Decimal("0.02")
            + (Decimal(80490) - 50998) * Decimal("0.04")
            + (california_taxable - 80490) * Decimal("0.06")
            - 298
        )
        self.assertEqual(d.estimated_taxes(100000), federal + payroll + california)

    def test_retired_household_pays_no_payroll_tax_and_gets_the_extra_deduction(self):
        working = d.estimated_taxes(100000)
        retired = d.estimated_taxes(100000, retired=True)
        payroll = Decimal(100000) * (Decimal("0.062") + Decimal("0.0145") + Decimal("0.011"))
        extra_deduction = Decimal(3100) * Decimal("0.12")
        self.assertEqual(working - retired, payroll + extra_deduction)

    def test_social_security_stops_at_two_wage_bases(self):
        high = d.estimated_taxes(400000) - d.estimated_taxes(399000)
        self.assertLess(high, Decimal(1000))
        federal_and_state = Decimal("0.24") + Decimal("0.093")
        medicare = Decimal("0.0145") + Decimal("0.009")
        self.assertEqual(high, Decimal(1000) * (federal_and_state + medicare + Decimal("0.011")))

    def test_no_income_pays_no_tax(self):
        self.assertEqual(d.estimated_taxes(0), 0)
        self.assertEqual(d.estimated_taxes(10000, retired=True), 0)


class CostTests(unittest.TestCase):
    def test_housing_weights_owner_costs_by_the_owner_share(self):
        self.assertEqual(d.housing_cost(2000, 3000, Decimal("0.25")), Decimal(27000))
        self.assertEqual(d.housing_cost(2000, None, Decimal("0.25")), Decimal(24000))
        self.assertEqual(d.housing_cost(None, 3000, Decimal("0.25")), Decimal(36000))
        self.assertIsNone(d.housing_cost(None, None, Decimal("0.25")))
        self.assertIsNone(d.housing_cost(2000, 3000, None))

    def test_essentials_scale_with_household_size(self):
        self.assertEqual(d.essentials_cost(Decimal("2.9")), d.LA_ESSENTIALS)
        self.assertEqual(d.essentials_cost(Decimal("1.45")), d.LA_ESSENTIALS / 2)
        self.assertEqual(d.essentials_cost(None), d.LA_ESSENTIALS)
        telephone = Decimal(4917) * Decimal(1472) / Decimal(4931)
        self.assertEqual(d.LA_ESSENTIALS, Decimal(6546 + 14497 + 5906 + 597) + telephone)

    def test_cost_of_living_is_housing_plus_essentials(self):
        self.assertEqual(
            d.cost_of_living(2000, 3000, Decimal("0.25"), Decimal("2.9")),
            Decimal(27000) + d.LA_ESSENTIALS,
        )
        self.assertIsNone(d.cost_of_living(None, None, Decimal("0.25"), Decimal("2.9")))


class ShareTests(unittest.TestCase):
    def test_bracket_shares(self):
        rounded = [None if share is None else round(float(share), 3) for share in d.BRACKET_SHARES]
        self.assertEqual(rounded, [None, None, 2.976, 1.749, 0.767, 0.405, 0.318, 0.263, 0.207])
        self.assertEqual(d.BRACKET_LOWS, (0, 15000, 30000, 40000, 50000, 70000, 100000, 150000, 200000))

    def test_age_adjustments(self):
        rounded = {band: round(float(value), 3) for band, value in d.AGE_ADJUSTMENTS.items()}
        self.assertEqual(rounded, {"25_44": 0.846, "45_64": 0.965, "65_plus": 1.265})
        self.assertEqual(
            set(d.AGE_ADJUSTMENTS), {band for band, _first, _label in census.HOUSEHOLDER_AGE_BANDS}
        )

    def test_share_is_capped(self):
        self.assertEqual(d.discretionary_share(10000, "25_44"), d.MAX_SHARE * d.AGE_ADJUSTMENTS["25_44"])
        self.assertEqual(d.discretionary_share(35000, "45_64"), d.MAX_SHARE)
        self.assertEqual(d.discretionary_share(10000, "65_plus"), d.MAX_SHARE)
        self.assertEqual(
            d.discretionary_share(120000, "25_44"), d.BRACKET_SHARES[6] * d.AGE_ADJUSTMENTS["25_44"]
        )
        for income in range(0, 600001, 25000):
            for band in d.AGE_ADJUSTMENTS:
                share = d.discretionary_share(income, band)
                self.assertTrue(d.MIN_SHARE <= share <= d.MAX_SHARE)

    def test_bracket_label_contains_the_income(self):
        self.assertEqual(d.bracket_label(14999), "Less than $15,000")
        self.assertEqual(d.bracket_label(15000), "$15,000 to $29,999")
        self.assertEqual(d.bracket_label(199999), "$150,000 to $199,999")
        self.assertEqual(d.bracket_label(250001), "$200,000 and more")


class SolverTests(unittest.TestCase):
    living = Decimal(60000)

    def test_income_needed_is_the_smallest_income_that_covers_the_price(self):
        for band in d.AGE_ADJUSTMENTS:
            for price in (Decimal(3000), Decimal(8250), Decimal("11500.5"), Decimal(25000)):
                needed = d.income_needed(price, self.living, band)
                self.assertGreaterEqual(d.discretionary_income(needed, self.living, band), price)
                self.assertTrue(
                    all(
                        d.discretionary_income(income, self.living, band) < price
                        for income in range(0, needed, 250)
                    ),
                    (band, price),
                )
                self.assertLess(d.discretionary_income(needed - 1, self.living, band), price)

    def test_higher_costs_need_more_income(self):
        cheap = d.income_needed(Decimal(8000), Decimal(40000), "25_44")
        dear = d.income_needed(Decimal(8000), Decimal(80000), "25_44")
        self.assertLess(cheap, dear)

    def test_fields_for_one_household(self):
        fields = d.household_fields(150000, self.living, "65_plus")
        self.assertEqual(
            fields,
            {
                "household_income_for_age_range": 150000,
                "estimated_taxes": 21736,
                "average_cost_of_living": 60000,
                "money_left_after_essentials": 68264,
                "income_bracket": "$150,000 to $199,999",
                "discretionary_share_of_remaining": Decimal("33.3"),
                "discretionary_income": 22748,
            },
        )
        poor = d.household_fields(40000, self.living, "25_44")
        self.assertLess(poor["money_left_after_essentials"], 0)
        self.assertEqual(poor["discretionary_income"], 0)
        blank = d.household_fields(150000, None, "25_44")
        self.assertEqual(blank["household_income_for_age_range"], 150000)
        self.assertTrue(all(blank[name] is None for name in d.FIELD_TYPES if name != "household_income_for_age_range"))
        self.assertEqual(list(fields), list(d.FIELD_TYPES))


class CensusHousingTests(unittest.TestCase):
    def test_housing_measures_are_read_as_single_values(self):
        estimates = {code: Decimal(1) for _name, codes in census.field_groups() for code in codes}
        estimates[census.MEASURES["median_age"]] = Decimal("36.5")
        estimates["B25064_001E"] = Decimal(2150)
        estimates["B25088_001E"] = None
        estimates["B25010_001E"] = Decimal("2.87")
        row = dict(zip(census.COLUMNS, census.row_from_estimates(2024, "90001", estimates)))
        self.assertEqual(row["median_gross_rent"], 2150)
        self.assertIsNone(row["median_owner_costs"])
        self.assertEqual(row["average_household_size"], Decimal("2.87"))
        self.assertIn(census.HOUSING_COST_COLUMNS, census.BACKFILL_SETS)


if __name__ == "__main__":
    unittest.main()
