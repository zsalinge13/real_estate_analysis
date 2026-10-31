"""Estimated discretionary income for a household, per ZIP and persona.

discretionary_income = max(0, income - (estimated_taxes + average_cost_of_living))
                       x discretionary_share_of_remaining

estimated_taxes: 2024 federal income tax, California income tax, and payroll
taxes for a married couple filing jointly with no dependents, at the
household income. A working-age household's income is treated as wages
earned evenly by two earners, so it pays Social Security up to two wage
bases, Medicare (plus the additional 0.9% above $250,000), and California
SDI. A household headed by someone 65 or older is treated as retired: no
payroll taxes or SDI, and the federal standard deduction adds $1,550 per
spouse. All of its income is taxed as ordinary income, which overstates the
tax on Social Security benefits. Credits other than the California personal
exemption credit are not applied.

average_cost_of_living: yearly housing plus non-housing essentials.
Housing is the ZIP's ACS median gross rent (B25064) and median selected
monthly owner costs for all owners (B25088), weighted by the ZIP's
owner-occupied share, times 12. Both ACS medians already include utilities
other than telephone. Non-housing essentials are the BLS Consumer
Expenditure Surveys 2023-2024 means for the Los Angeles-Long Beach-Anaheim
MSA: food at home, telephone services, transportation, healthcare, and life
and other personal insurance. The MSA table does not split utilities, so
telephone is the MSA utilities line times the national telephone share of
utilities. Retirement, pensions, and Social Security are left out because
payroll taxes are already in estimated_taxes. That total is scaled by the
ZIP's average household size (B25010) over the MSA's 2.9 people per
consumer unit.

discretionary_share_of_remaining: the BLS income-before-taxes bracket
(Table 1203, 2025) that contains the household income, measured as six
categories (food away from home, alcoholic beverages, apparel and services,
entertainment, personal care, reading) over the bracket's money left after
taxes and essentials. BLS 2025 tables no longer publish income after taxes,
so the bracket's taxes come from the same tax function at its mean income
before taxes, and its essentials use the same lines as the ZIP: shelter
less other lodging, utilities, food at home, transportation, healthcare,
and life and other personal insurance. Then it is multiplied by the
persona's age adjustment: the same share for the persona's age bands over
the share for all consumer units (Table 1300, 2025). The result is capped
between 10% and 50%. Brackets below $30,000 spend more than they have left,
so they take the 50% cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

TAX_YEAR = 2024
FEDERAL_STANDARD_DEDUCTION = Decimal(29200)
FEDERAL_65_PLUS_ADDITIONAL_DEDUCTION = Decimal(2 * 1550)
# (upper bound of taxable income, rate). The last bound is open.
FEDERAL_BRACKETS = (
    (Decimal(23200), Decimal("0.10")),
    (Decimal(94300), Decimal("0.12")),
    (Decimal(201050), Decimal("0.22")),
    (Decimal(383900), Decimal("0.24")),
    (Decimal(487450), Decimal("0.32")),
    (Decimal(731200), Decimal("0.35")),
    (None, Decimal("0.37")),
)
EARNERS = 2
SOCIAL_SECURITY_RATE = Decimal("0.062")
SOCIAL_SECURITY_WAGE_BASE = Decimal(168600)
MEDICARE_RATE = Decimal("0.0145")
ADDITIONAL_MEDICARE_RATE = Decimal("0.009")
ADDITIONAL_MEDICARE_THRESHOLD = Decimal(250000)
CALIFORNIA_STANDARD_DEDUCTION = Decimal(11080)
CALIFORNIA_EXEMPTION_CREDIT = Decimal(298)
CALIFORNIA_BRACKETS = (
    (Decimal(21512), Decimal("0.01")),
    (Decimal(50998), Decimal("0.02")),
    (Decimal(80490), Decimal("0.04")),
    (Decimal(111732), Decimal("0.06")),
    (Decimal(141212), Decimal("0.08")),
    (Decimal(721318), Decimal("0.093")),
    (Decimal(865574), Decimal("0.103")),
    (Decimal(1442628), Decimal("0.113")),
    (None, Decimal("0.123")),
)
CALIFORNIA_MENTAL_HEALTH_RATE = Decimal("0.01")
CALIFORNIA_MENTAL_HEALTH_THRESHOLD = Decimal(1000000)
CALIFORNIA_SDI_RATE = Decimal("0.011")
RETIRED_BANDS = frozenset({"65_plus"})

COST_SOURCE = (
    "https://www.bls.gov/cex/tables/geographic/mean/cu-msa-west-2-year-average-2023-2024.xlsx"
)
BRACKET_SOURCE = (
    "https://www.bls.gov/cex/tables/calendar-year/mean-item-share-average-standard-error/"
    "cu-income-before-taxes-2025.xlsx"
)
AGE_SOURCE = (
    "https://www.bls.gov/cex/tables/calendar-year/mean-item-share-average-standard-error/"
    "reference-person-age-ranges-2025.xlsx"
)
# Los Angeles-Long Beach-Anaheim MSA, BLS CE 2023-2024 Table 3033, per consumer unit.
LA_FOOD_AT_HOME = Decimal(6546)
LA_UTILITIES = Decimal(4917)
LA_TRANSPORTATION = Decimal(14497)
LA_HEALTHCARE = Decimal(5906)
LA_LIFE_INSURANCE = Decimal(597)
LA_PEOPLE_PER_CONSUMER_UNIT = Decimal("2.9")
# National telephone services over utilities, fuels, and public services (Table 1203, 2025).
TELEPHONE_SHARE_OF_UTILITIES = Decimal(1472) / Decimal(4931)
LA_ESSENTIALS = (
    LA_FOOD_AT_HOME
    + LA_UTILITIES * TELEPHONE_SHARE_OF_UTILITIES
    + LA_TRANSPORTATION
    + LA_HEALTHCARE
    + LA_LIFE_INSURANCE
)

# Output fields per ZIP and persona, in output order, with their SQL types.
FIELD_TYPES = {
    "household_income_for_age_range": "integer",
    "estimated_taxes": "integer",
    "average_cost_of_living": "integer",
    "money_left_after_essentials": "integer",
    "income_bracket": "text",
    "discretionary_share_of_remaining": "numeric",
    "discretionary_income": "integer",
}

MIN_SHARE = Decimal("0.10")
MAX_SHARE = Decimal("0.50")
SEARCH_CEILING = 10_000_000


@dataclass(frozen=True)
class SpendingGroup:
    """BLS CE annual means for one group of consumer units."""

    label: str
    consumer_units: int
    income: int
    food_away: int
    alcohol: int
    apparel: int
    entertainment: int
    personal_care: int
    reading: int
    shelter: int
    other_lodging: int
    utilities: int
    food_at_home: int
    transportation: int
    healthcare: int
    life_insurance: int

    @property
    def discretionary(self) -> int:
        return (
            self.food_away
            + self.alcohol
            + self.apparel
            + self.entertainment
            + self.personal_care
            + self.reading
        )

    @property
    def essentials(self) -> int:
        return (
            self.shelter
            - self.other_lodging
            + self.utilities
            + self.food_at_home
            + self.transportation
            + self.healthcare
            + self.life_insurance
        )


# Table 1203, 2025, by income before taxes: (lower bound, group).
INCOME_BRACKETS = (
    (0, SpendingGroup("Less than $15,000", 9463, 6971, 1768, 302, 942, 2034, 440, 37, 9613, 397, 2917, 3524, 5890, 3076, 152)),
    (15000, SpendingGroup("$15,000 to $29,999", 15491, 22538, 1365, 180, 795, 1437, 407, 26, 10678, 185, 3208, 3542, 5524, 4192, 182)),
    (30000, SpendingGroup("$30,000 to $39,999", 10490, 34965, 2088, 190, 849, 1586, 469, 62, 10902, 393, 3879, 4349, 5731, 4756, 343)),
    (40000, SpendingGroup("$40,000 to $49,999", 10199, 45071, 2285, 253, 1298, 2196, 640, 101, 12506, 473, 4199, 5235, 8756, 4908, 371)),
    (50000, SpendingGroup("$50,000 to $69,999", 17229, 59251, 3120, 509, 1253, 2376, 707, 65, 14434, 800, 4638, 5694, 9981, 5686, 365)),
    (70000, SpendingGroup("$70,000 to $99,999", 20669, 84028, 3720, 425, 1620, 2897, 841, 101, 16017, 993, 4868, 5948, 12964, 6161, 479)),
    (100000, SpendingGroup("$100,000 to $149,999", 23066, 122050, 4912, 639, 2205, 4586, 1104, 124, 18229, 1245, 5558, 7369, 16635, 7365, 647)),
    (150000, SpendingGroup("$150,000 to $199,999", 13362, 171173, 5678, 989, 3097, 5237, 1366, 134, 22418, 1744, 6203, 8921, 18855, 8931, 971)),
    (200000, SpendingGroup("$200,000 and more", 18026, 343658, 9053, 1648, 4615, 11301, 2219, 287, 35186, 4806, 7105, 10658, 27448, 10189, 1532)),
)
# Table 1300, 2025, by age of reference person.
ALL_CONSUMER_UNITS = SpendingGroup("All consumer units", 137994, 110847, 4142, 630, 2010, 4084, 986, 114, 17626, 1366, 4931, 6491, 13492, 6470, 604)
AGE_GROUPS = {
    "25 to 34": SpendingGroup("25-34 years", 20969, 115828, 4529, 617, 1702, 3965, 903, 77, 20487, 804, 4074, 5425, 12345, 4347, 291),
    "35 to 44": SpendingGroup("35-44 years", 24771, 139176, 4978, 701, 2273, 4071, 1136, 128, 20415, 1199, 5284, 7165, 15386, 5843, 598),
    "45 to 54": SpendingGroup("45-54 years", 22516, 148365, 5749, 871, 3248, 6909, 1341, 113, 20336, 1913, 5766, 9335, 18059, 6773, 874),
    "55 to 64": SpendingGroup("55-64 years", 23919, 125333, 3800, 551, 2060, 3998, 1057, 110, 16531, 1937, 5541, 6427, 14467, 7317, 726),
    "65 and older": SpendingGroup("65 years and older", 39878, 69497, 2818, 556, 1383, 2975, 757, 121, 13992, 1278, 4680, 5408, 10109, 7986, 628),
}
BAND_AGE_GROUPS = {
    "25_44": ("25 to 34", "35 to 44"),
    "45_64": ("45 to 54", "55 to 64"),
    "65_plus": ("65 and older",),
}
BRACKET_LOWS = tuple(low for low, _group in INCOME_BRACKETS)


def schedule_tax(taxable: Decimal, brackets) -> Decimal:
    tax = Decimal(0)
    lower = Decimal(0)
    for upper, rate in brackets:
        if upper is None or taxable < upper:
            return tax + (taxable - lower) * rate
        tax += (upper - lower) * rate
        lower = upper
    return tax


def estimated_taxes(income: Decimal | int, retired: bool = False) -> Decimal:
    """2024 federal, California, and payroll taxes for a married couple filing jointly."""
    income = Decimal(income)
    if income <= 0:
        return Decimal(0)
    deduction = FEDERAL_STANDARD_DEDUCTION + (
        FEDERAL_65_PLUS_ADDITIONAL_DEDUCTION if retired else 0
    )
    federal = schedule_tax(max(Decimal(0), income - deduction), FEDERAL_BRACKETS)
    california_taxable = max(Decimal(0), income - CALIFORNIA_STANDARD_DEDUCTION)
    california = max(
        Decimal(0),
        schedule_tax(california_taxable, CALIFORNIA_BRACKETS) - CALIFORNIA_EXEMPTION_CREDIT,
    ) + CALIFORNIA_MENTAL_HEALTH_RATE * max(
        Decimal(0), california_taxable - CALIFORNIA_MENTAL_HEALTH_THRESHOLD
    )
    if retired:
        return federal + california
    wages_each = income / EARNERS
    payroll = (
        SOCIAL_SECURITY_RATE * min(wages_each, SOCIAL_SECURITY_WAGE_BASE) * EARNERS
        + MEDICARE_RATE * income
        + ADDITIONAL_MEDICARE_RATE * max(Decimal(0), income - ADDITIONAL_MEDICARE_THRESHOLD)
        + CALIFORNIA_SDI_RATE * income
    )
    return federal + california + payroll


def remaining_share(groups: list[SpendingGroup], retired: bool = False) -> Decimal | None:
    """Discretionary spending over money left after taxes and essentials, weighted by consumer units.

    None when the groups have nothing left after taxes and essentials.
    """
    spent = Decimal(0)
    remaining = Decimal(0)
    for group in groups:
        units = Decimal(group.consumer_units)
        spent += units * group.discretionary
        remaining += units * (
            group.income - estimated_taxes(group.income, retired) - group.essentials
        )
    if remaining <= 0:
        return None
    return spent / remaining


def bracket_index(income: Decimal | int) -> int:
    income = Decimal(income)
    index = 0
    for position, low in enumerate(BRACKET_LOWS):
        if income >= low:
            index = position
    return index


def bracket_label(income: Decimal | int) -> str:
    return INCOME_BRACKETS[bracket_index(income)][1].label


BRACKET_SHARES = tuple(remaining_share([group]) for _low, group in INCOME_BRACKETS)


def age_adjustment(band: str) -> Decimal:
    retired = band in RETIRED_BANDS
    groups = [AGE_GROUPS[name] for name in BAND_AGE_GROUPS[band]]
    return remaining_share(groups, retired) / remaining_share([ALL_CONSUMER_UNITS])


AGE_ADJUSTMENTS = {band: age_adjustment(band) for band in BAND_AGE_GROUPS}


def discretionary_share(income: Decimal | int, band: str) -> Decimal:
    """The bracket share times the age adjustment, capped between MIN_SHARE and MAX_SHARE."""
    bracket = BRACKET_SHARES[bracket_index(income)]
    raw = (MAX_SHARE if bracket is None else bracket) * AGE_ADJUSTMENTS[band]
    return min(MAX_SHARE, max(MIN_SHARE, raw))


def housing_cost(
    median_gross_rent: int | None,
    median_owner_costs: int | None,
    owner_share: Decimal | None,
) -> Decimal | None:
    """Yearly housing. A ZIP without one tenure's median uses the other for every household."""
    if median_gross_rent is None and median_owner_costs is None:
        return None
    if median_owner_costs is None:
        return Decimal(median_gross_rent) * 12
    if median_gross_rent is None:
        return Decimal(median_owner_costs) * 12
    if owner_share is None:
        return None
    monthly = owner_share * Decimal(median_owner_costs) + (1 - owner_share) * Decimal(
        median_gross_rent
    )
    return monthly * 12


def essentials_cost(household_size: Decimal | None) -> Decimal:
    """Los Angeles MSA non-housing essentials scaled to the household size. Unscaled when it is blank."""
    if household_size is None or household_size <= 0:
        return LA_ESSENTIALS
    return LA_ESSENTIALS * Decimal(household_size) / LA_PEOPLE_PER_CONSUMER_UNIT


def cost_of_living(
    median_gross_rent: int | None,
    median_owner_costs: int | None,
    owner_share: Decimal | None,
    household_size: Decimal | None,
) -> Decimal | None:
    housing = housing_cost(median_gross_rent, median_owner_costs, owner_share)
    if housing is None:
        return None
    return housing + essentials_cost(household_size)


def discretionary_income(income: Decimal | int, living: Decimal, band: str) -> Decimal:
    income = Decimal(income)
    left = income - estimated_taxes(income, band in RETIRED_BANDS) - living
    return max(Decimal(0), left) * discretionary_share(income, band)


def income_needed(price: Decimal, living: Decimal, band: str) -> int | None:
    """Smallest whole-dollar household income whose discretionary income covers the price.

    The share changes at each BLS bracket edge, so discretionary income can
    fall when income crosses into the next bracket. Inside one bracket it
    only rises with income, so each bracket is checked at its top and
    searched by bisection when the top is enough.
    """
    for index, low in enumerate(BRACKET_LOWS):
        top = BRACKET_LOWS[index + 1] - 1 if index + 1 < len(BRACKET_LOWS) else SEARCH_CEILING
        if discretionary_income(top, living, band) < price:
            continue
        lower, upper = low, top
        while lower < upper:
            middle = (lower + upper) // 2
            if discretionary_income(middle, living, band) >= price:
                upper = middle
            else:
                lower = middle + 1
        return lower
    return None


def whole_dollars(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def household_fields(income: int | None, living: Decimal | None, band: str) -> dict:
    """The discretionary fields for one household. Blank without an income or a cost of living."""
    fields = dict.fromkeys(FIELD_TYPES)
    fields["household_income_for_age_range"] = income
    if income is None or living is None:
        return fields
    taxes = estimated_taxes(income, band in RETIRED_BANDS)
    share = discretionary_share(income, band)
    left = Decimal(income) - taxes - living
    fields.update(
        estimated_taxes=whole_dollars(taxes),
        average_cost_of_living=whole_dollars(living),
        money_left_after_essentials=whole_dollars(left),
        income_bracket=bracket_label(income),
        discretionary_share_of_remaining=(share * 100).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        ),
        discretionary_income=whole_dollars(max(Decimal(0), left) * share),
    )
    return fields
