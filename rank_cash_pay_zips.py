"""Rank practice-area ZIPs from the Census-Redfin join.

The practice area is in practice.py: Los Angeles and Orange County ZIPs
within 50 miles of the practice ZIP. A join row outside it is skipped. The
screen medians are taken over the eligible ZIPs in that area.

The input is the join that just succeeded. This script does not rebuild
that join and does not delete Redfin months. Each persona has a cash screen,
and each procedure and persona list keeps the top 25 ZIPs that pass it, ranked
by composite_score (ranking_weights.py). The same run writes marketing_bullets on
one procedure_surgeon_fees report year. The preferred year is acs_year on
zip_census_redfin_join. A year qualifies when surgeon_fee_low and
surgeon_fee_high are both non-null on more than half of that year's
procedure rows. When the join year qualifies, bullets are written only
there. Otherwise they are written only on the most recent report year that
qualifies. Every other report year is left blank in that column. Fees are
not copied from another year. The fee load owns the column and does not
fill it. Each procedure then gets an affordability screen: a ZIP stays on
that procedure's list only when its discretionary_income (discretionary.py)
is at least the midpoint of that procedure's surgeon fee range. That is the
money a typical household headed by someone in the persona's age band has
left after estimated taxes and the ZIP's cost of living, times the share
such a household spends on discretionary items. The household income is the
median of the B19037 brackets for that householder band, or the ZIP's median
household income when those brackets are blank. A procedure with a blank
fee is not screened. The survivors are scored: each factor is a percentile
rank among every eligible ZIP for the persona, weighted for the procedure.
cash_pay_zip_rank holds the same lists, one row per procedure, persona, and
ZIP, with the discretionary fields, the profile measures, and the scores.
The stored form is one entry per ranked ZIP, in persona-list order:
marketing_bullets has one line per ZIP holding that ZIP's age-group header
sentence, and rank, city, state, zip_code, insight, household_income,
individual_income (ACS per-capita income),
median_listing_price_per_square_foot, age_range, sex, and the descriptive
columns after it, through composite_score and the score_<factor> columns,
are same-order lists. Race, ethnicity, and education never change a screen
or a rank. A group with no ranked ZIP adds no entry.
After that write succeeds, the same run exports every column of
procedure_surgeon_fees for the chosen report year and every other report
year. The file has one row per ranked ZIP. report_year, procedure, and the
fee columns repeat on each of those rows. A procedure year with no ZIP rows
stays one row with the bullet fields blank. No cell has a line break.
"""

from __future__ import annotations

import csv
import os
from bisect import bisect_left, bisect_right
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import dataset_stamp
import discretionary
import load_california
import practice
import pull_census_zip_population as census
import pull_procedure_surgeon_fees as procedures
import ranking_weights

RANK_TABLE_NAME = "cash_pay_zip_rank"
RANK_TABLE = "public.cash_pay_zip_rank"
DELETE_RANK_SQL = f"DELETE FROM {RANK_TABLE}"
OUTPUT_CSV_NAME = "cash_pay_zip_rank.csv"
OUTPUT_CSV = Path(__file__).resolve().parent / OUTPUT_CSV_NAME
MIN_ACTIVE_LISTINGS = 10
TOP_N = 25
DATABASE_URL_ENV = load_california.DATABASE_URL_ENV
BULLET_RANK = "rank"
LEGACY_BULLET_RANK = "bullet_rank"
# Same-order lists on procedure_surgeon_fees. Element i of each list, with
# line i of marketing_bullets (that ZIP's group header), is one CSV row.
BULLET_LIST_COLUMNS = (
    (BULLET_RANK, "integer[]"),
    ("city", "text[]"),
    ("state", "text[]"),
    ("zip_code", "text[]"),
    ("practice_zip_code", "text[]"),
    ("county", "text[]"),
    ("distance_from_practice_miles", "numeric[]"),
    ("insight", "text[]"),
    ("household_income", "integer[]"),
    ("individual_income", "integer[]"),
    ("median_listing_price_per_square_foot", "numeric[]"),
    ("age_range", "text[]"),
    ("sex", "text[]"),
    ("race_and_ethnicity", "text[]"),
    ("education_level", "text[]"),
    ("women_in_age_range", "integer[]"),
    ("men_in_age_range", "integer[]"),
    ("women_share_of_age_range", "numeric[]"),
    ("household_income_for_age_range", "integer[]"),
    ("estimated_taxes", "integer[]"),
    ("average_cost_of_living", "integer[]"),
    ("money_left_after_essentials", "integer[]"),
    ("income_bracket", "text[]"),
    ("discretionary_share_of_remaining", "numeric[]"),
    ("discretionary_income", "integer[]"),
    ("income_needed", "integer[]"),
    ("affordable_households_share", "numeric[]"),
    ("affordable_households", "integer[]"),
    ("mothers_with_children_under_18", "integer[]"),
    ("mothers_with_children_under_6", "integer[]"),
    ("recent_births", "integer[]"),
    ("home_ownership", "text[]"),
    ("owner_occupied_share", "numeric[]"),
    ("owner_occupied_share_age_range", "numeric[]"),
    ("women_management_professional_share", "numeric[]"),
    ("women_arts_entertainment_media_share", "numeric[]"),
)
SCORE_COLUMNS = tuple(f"score_{factor}" for factor in ranking_weights.FACTORS)
BULLET_LIST_COLUMNS += (("composite_score", "numeric[]"),) + tuple(
    (name, "numeric[]") for name in SCORE_COLUMNS
)
BULLET_LIST_NAMES = tuple(name for name, _sql_type in BULLET_LIST_COLUMNS)
DESCRIPTIVE_LIST_NAMES = BULLET_LIST_NAMES[
    BULLET_LIST_NAMES.index("women_in_age_range") : BULLET_LIST_NAMES.index("composite_score")
]
BULLET_CSV_COLUMNS = ("marketing_bullets",) + BULLET_LIST_NAMES
# The ZIP and persona measures from DESCRIPTIVE_LIST_NAMES that cash_pay_zip_rank
# also carries. The discretionary fields are already there.
MEASURE_COLUMNS = tuple(
    name for name in DESCRIPTIVE_LIST_NAMES if name not in discretionary.FIELD_TYPES
)
SCORE_PLACES = Decimal("0.01")
COMPOSITE_PLACES = Decimal("0.1")
NEUTRAL_PERCENTILE = Decimal("0.5")

# composite_score ranks each list. Equal scores are the close case: the higher
# age share ranks first, then the higher cash signal. Household income is that
# signal for working-age personas. Median listing price per square foot is
# that signal for ages 65 and older.


@dataclass(frozen=True)
class Persona:
    label: str
    age_columns: tuple[str, ...]
    cash: str
    age_phrase: str
    householder_band: str
    householder_phrase: str


# The persona bands are the Census B19037 householder-age bands, so the
# household income of each band can be read directly. Adults under 25 are not
# a persona.
PERSONAS = (
    Persona("25–44", ("population_25_44",), "working", "ages 25 to 44", "25_44", "25–44"),
    Persona("45–64", ("population_45_64",), "working", "ages 45 to 64", "45_64", "45–64"),
    Persona("65+", ("population_65_plus",), "older", "ages 65 and older", "65_plus", "65 or older"),
)
PERSONA_BY_LABEL = {persona.label: persona for persona in PERSONAS}

# The ZIP rank does not change by procedure. A procedure uses these persona
# lists, and procedures that use the same lists get the same bullet text.
PROCEDURE_PERSONAS = {
    "breast augmentation": ("25–44",),
    "breast lift": ("25–44", "45–64"),
    "breast reduction": ("25–44", "45–64"),
    "liposuction": ("25–44", "45–64"),
    "tummy tuck": ("25–44", "45–64"),
    "arm lift": ("25–44", "45–64", "65+"),
    "thigh lift": ("25–44", "45–64", "65+"),
    "buttock augmentation with fat grafting": ("25–44",),
    "eyelid surgery": ("45–64", "65+"),
    "facelift": ("45–64", "65+"),
    "neck lift": ("45–64", "65+"),
    "rhinoplasty": ("25–44",),
    "forehead lift": ("45–64", "65+"),
}

LOWERCASE_CITY_WORDS = frozenset({"del", "de", "of", "the"})
# Labels for the largest group in a ZIP. The order breaks a tie. These
# describe the ZIP and are never a screen or a rank key.
RACE_ETHNICITY_LABELS = {
    "hispanic_or_latino_population": "Hispanic or Latino",
    "non_hispanic_white_population": "Non-Hispanic White",
    "non_hispanic_black_population": "Non-Hispanic Black",
    "non_hispanic_asian_population": "Non-Hispanic Asian",
    "non_hispanic_other_or_multiracial_population": "Non-Hispanic other or multiracial",
}
EDUCATION_LABELS = {
    "education_less_than_high_school": "Less than high school",
    "education_high_school": "High school",
    "education_some_college_or_associate": "Some college or associate degree",
    "education_bachelors": "Bachelor's degree",
    "education_graduate_or_professional": "Graduate or professional degree",
}
BACHELORS_OR_HIGHER = ("education_bachelors", "education_graduate_or_professional")
MOTHER_PROCEDURES = ranking_weights.MOTHER_PROCEDURES

RANK_COLUMNS = (
    "procedure",
    "surgeon_fee_midpoint",
    "persona",
    "rank",
    "zip",
    "zip_name",
    "practice_zip_code",
    "county",
    "distance_from_practice_miles",
    "age_share",
    "median_household_income",
    "per_capita_income",
    "median_listing_price",
    "active_listing_count",
    "male_population",
    "female_population",
    "female_share",
    "median_age",
    "median_listing_price_per_square_foot",
    "household_income_middle",
    "per_capita_income_middle",
    "listing_price_middle",
    "acs_year",
    "month_date_yyyymm",
    "date_pulled_yyyymmdd",
    *discretionary.FIELD_TYPES,
    *MEASURE_COLUMNS,
    "composite_score",
    *SCORE_COLUMNS,
)
COLUMN_TYPES = {
    "procedure": "text",
    "surgeon_fee_midpoint": "numeric",
    "persona": "text",
    "rank": "integer",
    "zip": "text",
    "zip_name": "text",
    "practice_zip_code": "text",
    "county": "text",
    "distance_from_practice_miles": "numeric",
    "age_share": "numeric",
    "median_household_income": "integer",
    "per_capita_income": "integer",
    "median_listing_price": "numeric",
    "active_listing_count": "integer",
    "male_population": "integer",
    "female_population": "integer",
    "female_share": "numeric",
    "median_age": "numeric",
    "median_listing_price_per_square_foot": "numeric",
    "household_income_middle": "numeric",
    "per_capita_income_middle": "numeric",
    "listing_price_middle": "numeric",
    "acs_year": "integer",
    "month_date_yyyymm": "text",
    "date_pulled_yyyymmdd": "text",
    **discretionary.FIELD_TYPES,
    **{
        name: sql_type.removesuffix("[]")
        for name, sql_type in BULLET_LIST_COLUMNS
        if name in MEASURE_COLUMNS or name == "composite_score" or name in SCORE_COLUMNS
    },
}
NOT_NULL_COLUMNS = {
    "procedure",
    "composite_score",
    "persona",
    "rank",
    "zip",
    "zip_name",
    "age_share",
    "median_household_income",
    "per_capita_income",
    "median_listing_price_per_square_foot",
    "active_listing_count",
    "household_income_middle",
    "per_capita_income_middle",
    "listing_price_middle",
    "acs_year",
    "month_date_yyyymm",
    "date_pulled_yyyymmdd",
}
JOIN_FIELDS = (
    "zip",
    "postal_code",
    "zip_name",
    "median_household_income",
    "per_capita_income",
    "median_age",
    "male_population",
    "female_population",
    "adult_population",
    "population_25_44",
    "population_45_64",
    "population_65_plus",
    "median_listing_price",
    "median_listing_price_per_square_foot",
    "active_listing_count",
    "acs_year",
    "month_date_yyyymm",
    "date_pulled_yyyymmdd",
    *practice.FIELDS,
)
INTEGER_RANK_COLUMNS = {
    name for name, sql_type in COLUMN_TYPES.items() if sql_type == "integer"
}

if set(PROCEDURE_PERSONAS) != set(procedures.PROCEDURES):
    raise SystemExit("Every procedure needs a persona list.")
if any(persona.householder_band not in discretionary.AGE_ADJUSTMENTS for persona in PERSONAS):
    raise SystemExit("Every persona needs a BLS age adjustment.")
if any(label not in PERSONA_BY_LABEL for labels in PROCEDURE_PERSONAS.values() for label in labels):
    raise SystemExit("A procedure lists an unknown persona.")
for _procedure in PROCEDURE_PERSONAS:
    ranking_weights.procedure_group(_procedure)
if "DELETE" in DELETE_RANK_SQL.upper() and "redfin_real_estate_value" in DELETE_RANK_SQL:
    raise SystemExit("The rank refresh must not delete Redfin rows.")


@dataclass
class PersonaRank:
    persona: Persona
    household_income_middle: Decimal | None
    per_capita_income_middle: Decimal | None
    listing_price_middle: Decimal | None
    rows: list[dict]
    passed: list[dict] = field(default_factory=list)
    eligible: list[dict] = field(default_factory=list)


def quote_ident(name: str) -> str:
    if load_california.IDENT_RE.fullmatch(name) is None:
        raise SystemExit(f"Refusing unexpected column {name!r}.")
    return name


def blank(value: object) -> bool:
    if value is None:
        return True
    return str(value).strip() == ""


def as_decimal(value: object) -> Decimal | None:
    if blank(value):
        return None
    try:
        return Decimal(str(value).strip())
    except Exception as exc:
        raise SystemExit(f"{value!r} is not a number.") from exc


def as_int(value: object, column: str) -> int | None:
    number = as_decimal(value)
    if number is None:
        return None
    if number != number.to_integral_value():
        raise SystemExit(f"{column} is not a whole number.")
    return int(number)


def middle(values: list[Decimal]) -> Decimal:
    """The middle of the values that remain. An even count averages the two middles."""
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        raise SystemExit("Cannot find the middle of an empty list.")
    half = count // 2
    if count % 2 == 1:
        return ordered[half]
    return (ordered[half - 1] + ordered[half]) / Decimal(2)


def format_money(value: Decimal) -> str:
    """Whole dollars with commas. Half a dollar rounds up."""
    return f"${int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)):,}"


def percent_points(share: Decimal | None) -> Decimal | None:
    """A share as percentage points to one decimal, the number the text shows."""
    if share is None:
        return None
    return (share * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def sex_label(female_share: Decimal | None) -> str | None:
    """"woman" when the female share the insight prints is 50.0% or more, else "man"."""
    points = percent_points(female_share)
    if points is None:
        return None
    return "woman" if points >= Decimal(50) else "man"


def format_percent(share: Decimal) -> str:
    return f"{percent_points(share)}%"


def read_join_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"{path} is missing.")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in JOIN_FIELDS if name not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path.name} is missing columns: {missing}")
        rows: list[dict[str, str]] = []
        for raw in reader:
            rows.append(
                {
                    key: "" if value is None else value
                    for key, value in raw.items()
                    if key is not None
                }
            )
        return rows


def female_share(row: dict[str, str]) -> Decimal | None:
    male = as_int(row.get("male_population"), "male_population")
    female = as_int(row.get("female_population"), "female_population")
    if male is None or female is None:
        return None
    total = male + female
    if total <= 0:
        return None
    return Decimal(female) / Decimal(total)


def largest_group(row: dict[str, str], labels: dict[str, str]) -> tuple[str | None, Decimal | None]:
    """Label and share of the largest group. Blank when any count is missing."""
    counts = {name: as_int(row.get(name), name) for name in labels}
    if any(value is None for value in counts.values()):
        return None, None
    total = sum(counts.values())
    if total <= 0:
        return None, None
    name = max(labels, key=lambda column: counts[column])
    return labels[name], Decimal(counts[name]) / Decimal(total)


def bachelors_or_higher_share(row: dict[str, str]) -> Decimal | None:
    counts = [as_int(row.get(name), name) for name in EDUCATION_LABELS]
    if any(value is None for value in counts):
        return None
    total = sum(counts)
    if total <= 0:
        return None
    degree = sum(as_int(row.get(name), name) for name in BACHELORS_OR_HIGHER)
    return Decimal(degree) / Decimal(total)


def describe_zip(row: dict[str, str]) -> dict:
    race, race_share = largest_group(row, RACE_ETHNICITY_LABELS)
    education, _share = largest_group(row, EDUCATION_LABELS)
    return {
        "race_and_ethnicity": race,
        "race_and_ethnicity_share": race_share,
        "education_level": education,
        "bachelors_or_higher_share": bachelors_or_higher_share(row),
    }


def age_count(row: dict[str, str], persona: Persona) -> int | None:
    total = 0
    for column in persona.age_columns:
        value = as_int(row.get(column), column)
        if value is None:
            return None
        total += value
    return total


def screen_row(row: dict[str, str], persona: Persona) -> dict | None:
    """Return the measures used for one persona, or None when the ZIP is skipped."""
    if not practice.in_area(
        row.get("practice_zip_code"), row.get("county"), row.get("distance_from_practice_miles")
    ):
        return None
    counted = age_count(row, persona)
    adults = as_int(row.get("adult_population"), "adult_population")
    household = as_int(row.get("median_household_income"), "median_household_income")
    per_capita = as_int(row.get("per_capita_income"), "per_capita_income")
    price = as_decimal(row.get("median_listing_price_per_square_foot"))
    listings = as_int(row.get("active_listing_count"), "active_listing_count")
    if (
        counted is None
        or adults is None
        or adults <= 0
        or household is None
        or per_capita is None
        or price is None
        or listings is None
        or listings < MIN_ACTIVE_LISTINGS
    ):
        return None
    zip_code = str(row.get("zip") or "").strip()
    if not zip_code:
        raise SystemExit("The join has a row without a ZIP.")
    postal = str(row.get("postal_code") or "").strip()
    if postal and postal != zip_code:
        raise SystemExit(f"ZIP {zip_code} does not match postal code {postal}.")
    year = as_int(row.get("acs_year"), "acs_year")
    month = str(row.get("month_date_yyyymm") or "").strip()
    pulled = str(row.get("date_pulled_yyyymmdd") or "").strip()
    if year is None or not month or not pulled:
        raise SystemExit(f"ZIP {zip_code} is missing the join freshness fields.")
    profile = {
        name: as_int(row.get(name), name)
        for name in census.PROFILE_COLUMNS + census.HOUSEHOLDER_AGE_COLUMNS
    }
    band = persona.householder_band
    band_median = median_from_brackets(
        [profile.get(name) for name in census.householder_income_columns(band)]
    )
    income = household if band_median is None else discretionary.whole_dollars(band_median)
    living = discretionary.cost_of_living(
        as_int(row.get("median_gross_rent"), "median_gross_rent"),
        as_int(row.get("median_owner_costs"), "median_owner_costs"),
        owner_fraction(profile),
        as_decimal(row.get("average_household_size")),
    )
    return {
        "zip": zip_code,
        "zip_name": str(row.get("zip_name") or "").strip(),
        "practice_zip_code": str(row["practice_zip_code"]).strip(),
        "county": str(row["county"]).strip(),
        "distance_from_practice_miles": Decimal(str(row["distance_from_practice_miles"]).strip()),
        "age_count": counted,
        "adult_population": adults,
        "age_share": Decimal(counted) / Decimal(adults),
        "median_household_income": household,
        "per_capita_income": per_capita,
        "median_listing_price": as_decimal(row.get("median_listing_price")),
        "active_listing_count": listings,
        "male_population": as_int(row.get("male_population"), "male_population"),
        "female_population": as_int(row.get("female_population"), "female_population"),
        "female_share": female_share(row),
        "median_age": as_decimal(row.get("median_age")),
        "median_listing_price_per_square_foot": price,
        "acs_year": year,
        "month_date_yyyymm": month,
        "date_pulled_yyyymmdd": pulled,
        **describe_zip(row),
        "profile": profile,
        "cost_of_living": living,
        **discretionary.household_fields(income, living, band),
    }


MEDIAN_INCOME_CEILING = Decimal(250001)


def owner_fraction(profile: dict) -> Decimal | None:
    owners = profile.get("owner_occupied_households")
    renters = profile.get("renter_occupied_households")
    if owners is None or renters is None or owners + renters <= 0:
        return None
    return Decimal(owners) / Decimal(owners + renters)


def median_from_brackets(counts: list[int | None]) -> Decimal | None:
    """Median household income from the 16 bracket counts in HOUSEHOLD_INCOME_BRACKETS order.

    Inside a closed bracket the households are spread evenly. A median at or
    above $200,000 follows the Pareto tail that households_at_or_above uses,
    capped at MEDIAN_INCOME_CEILING, the value ACS publishes for a median
    household income of $250,000 or more. Blank when a count is missing or
    there are no households.
    """
    if any(count is None for count in counts):
        return None
    total = sum(counts)
    if total <= 0:
        return None
    half = Decimal(total) / 2
    lows = [Decimal(low) for _column, _code, _phrase, low in census.HOUSEHOLD_INCOME_BRACKETS]
    below = Decimal(0)
    for index in range(len(lows) - 1):
        count = counts[index]
        if count and below + count >= half:
            return lows[index] + (half - below) / count * (lows[index + 1] - lows[index])
        below += count
    at_or_above_lower = counts[-2] + counts[-1]
    if at_or_above_lower <= counts[-1]:
        return MEDIAN_INCOME_CEILING
    alpha = (Decimal(at_or_above_lower) / Decimal(counts[-1])).ln() / (lows[-1] / lows[-2]).ln()
    return min(MEDIAN_INCOME_CEILING, lows[-1] * (Decimal(counts[-1]) / half) ** (1 / alpha))


def passes_cash(row: dict, persona: Persona, income_middle, per_capita_middle, price_middle) -> bool:
    if persona.cash == "working":
        return (
            Decimal(row["median_household_income"]) >= income_middle
            and row["median_listing_price_per_square_foot"] >= price_middle
        )
    if persona.cash == "older":
        return (
            row["median_listing_price_per_square_foot"] >= price_middle
            and Decimal(row["per_capita_income"]) >= per_capita_middle
        )
    raise SystemExit(f"Unknown cash test {persona.cash!r}.")


def covers_price(row: dict, price: Decimal) -> bool:
    """The affordability screen. A ZIP without discretionary income does not pass."""
    spending = row.get("discretionary_income")
    return spending is not None and spending >= price


def cash_signal(row: dict, persona: Persona) -> Decimal:
    if persona.cash == "working":
        return Decimal(row["median_household_income"])
    return row["median_listing_price_per_square_foot"]


def rank_persona(rows: list[dict[str, str]], persona: Persona) -> PersonaRank:
    prepared = []
    seen: set[str] = set()
    for row in rows:
        screened = screen_row(row, persona)
        if screened is None:
            continue
        if screened["zip"] in seen:
            raise SystemExit(f"The join has more than one row for ZIP {screened['zip']}.")
        seen.add(screened["zip"])
        prepared.append(screened)
    if not prepared:
        return PersonaRank(persona, None, None, None, [])
    income_middle = middle(
        [Decimal(row["median_household_income"]) for row in prepared]
    )
    per_capita_middle = middle([Decimal(row["per_capita_income"]) for row in prepared])
    price_middle = middle(
        [row["median_listing_price_per_square_foot"] for row in prepared]
    )
    kept = [
        row
        for row in prepared
        if passes_cash(row, persona, income_middle, per_capita_middle, price_middle)
    ]
    kept.sort(
        key=lambda row: (
            -row["age_share"],
            -cash_signal(row, persona),
            row["zip"],
        )
    )
    ranked = kept[:TOP_N]
    for index, row in enumerate(ranked, start=1):
        row["persona"] = persona.label
        row["rank"] = index
        row["household_income_middle"] = income_middle
        row["per_capita_income_middle"] = per_capita_middle
        row["listing_price_middle"] = price_middle
    return PersonaRank(
        persona,
        income_middle,
        per_capita_middle,
        price_middle,
        ranked,
        kept,
        prepared,
    )


def rank_personas(rows: list[dict[str, str]]) -> list[PersonaRank]:
    """Rank each persona from the join rows. Do not add or drop a ZIP by name."""
    zips = [str(row.get("zip") or "").strip() for row in rows]
    if any(not zip_code for zip_code in zips):
        raise SystemExit("The join has a row without a ZIP.")
    if len(zips) != len(set(zips)):
        raise SystemExit("The join has more than one row for a ZIP.")
    rows = practice_area_rows(rows)
    if rows and all(
        blank(row.get(name)) for row in rows for name in ("median_gross_rent", "median_owner_costs")
    ):
        raise SystemExit(
            "The join has no ACS housing costs (B25064 and B25088), so no ZIP could pass the "
            "affordability screen. Run Pull Census ZIP population to fill them, then rerun."
        )
    return [rank_persona(rows, persona) for persona in PERSONAS]


def practice_area_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """The 50-mile rule, applied before any screen, median, or rank."""
    kept, far, other = practice.split_by_area(
        rows,
        lambda row: (
            row.get("practice_zip_code"),
            row.get("county"),
            row.get("distance_from_practice_miles"),
        ),
    )
    print(
        f"Excluded {far} ZIPs more than {practice.MAX_PRACTICE_DISTANCE_MILES} miles from the "
        f"practice ZIP {practice.PRACTICE_ZIP_CODE} before any screen, median, or rank."
    )
    if other:
        print(f"Excluded {other} ZIPs outside {', '.join(practice.COUNTIES.values())} or another practice ZIP.")
    return kept


@dataclass(frozen=True)
class BulletRow:
    """One ranked ZIP under its age group's header sentence."""

    header: str
    rank: int
    city: str
    state: str
    zip_code: str
    insight: str
    practice_zip_code: str | None = None
    county: str | None = None
    distance_from_practice_miles: Decimal | None = None
    household_income: int | None = None
    individual_income: int | None = None
    median_listing_price_per_square_foot: Decimal | None = None
    age_range: str | None = None
    sex: str | None = None
    race_and_ethnicity: str | None = None
    education_level: str | None = None
    women_in_age_range: int | None = None
    men_in_age_range: int | None = None
    women_share_of_age_range: Decimal | None = None
    household_income_for_age_range: int | None = None
    estimated_taxes: int | None = None
    average_cost_of_living: int | None = None
    money_left_after_essentials: int | None = None
    income_bracket: str | None = None
    discretionary_share_of_remaining: Decimal | None = None
    discretionary_income: int | None = None
    income_needed: int | None = None
    affordable_households_share: Decimal | None = None
    affordable_households: int | None = None
    mothers_with_children_under_18: int | None = None
    mothers_with_children_under_6: int | None = None
    recent_births: int | None = None
    home_ownership: str | None = None
    owner_occupied_share: Decimal | None = None
    owner_occupied_share_age_range: Decimal | None = None
    women_management_professional_share: Decimal | None = None
    women_arts_entertainment_media_share: Decimal | None = None
    composite_score: Decimal | None = None
    scores: dict[str, Decimal | None] = field(default_factory=dict)

    def value(self, name: str) -> object:
        if name in SCORE_COLUMNS:
            return self.scores.get(name)
        return getattr(self, name)


def split_place(row: dict) -> tuple[str, str]:
    """City and state from the Realtor.com zip_name, such as "marina del rey, ca"."""
    name = str(row.get("zip_name") or "").strip()
    city, comma, state = name.rpartition(",")
    if not comma:
        return name, ""
    return city.strip(), state.strip()


def display_city(city: str) -> str:
    """Title case for the text, such as "Marina del Rey". The city field keeps its own case."""
    words = city.split()
    return " ".join(
        word if index and word in LOWERCASE_CITY_WORDS else "-".join(part.capitalize() for part in word.split("-"))
        for index, word in enumerate(words)
    )


def ordinal(position: int) -> str:
    if 10 <= position % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(position % 10, "th")
    return f"{position}{suffix}"


def screen_reasons(screen: PersonaRank) -> str:
    listing = format_money(screen.listing_price_middle)
    if screen.persona.cash == "working":
        return (
            "median household income and median listing price per square foot in these ZIP codes "
            f"are at or above the medians of {practice.AREA_PHRASE}, "
            f"{format_money(screen.household_income_middle)} and {listing}"
        )
    return (
        "median listing price per square foot and per-person income in these ZIP codes "
        f"are at or above the medians of {practice.AREA_PHRASE}, {listing} and "
        f"{format_money(screen.per_capita_income_middle)}"
    )


def screen_header(procedure: str, screen: PersonaRank, price: Decimal | None = None) -> str:
    persona = screen.persona
    group = f"adults {persona.age_phrase}"
    if screen.household_income_middle is None or not screen.rows:
        return (
            f"No ZIP code within {practice.MAX_PRACTICE_DISTANCE_MILES} miles of the practice qualified "
            f"for marketing {procedure} to {group}. "
            "Each ZIP code needs income and median listing price per square foot at or above "
            f"the median of {practice.AREA_PHRASE}, plus at least {MIN_ACTIVE_LISTINGS} active listings. "
            f"{practice.DISTANCE_RULE}"
        )
    if price is None:
        why = (
            f"Why: {screen_reasons(screen)}. "
            "Affordability wasn't checked because this procedure has no published surgeon fee."
        )
    else:
        why = (
            f"Why: {screen_reasons(screen)}. After estimated taxes, housing, and other essential "
            f"costs, a typical household headed by someone {persona.householder_phrase} in each "
            "ZIP code has enough left for discretionary spending to cover the "
            f"{format_money(price)} midpoint surgeon fee."
        )
    sentences = [
        f"Market {procedure} to {group} in the ZIP codes below.",
        why,
        ranking_sentence(procedure, persona, price),
        practice.DISTANCE_RULE,
        "Race, ethnicity and education describe these ZIP codes and are not used to rank them.",
    ]
    if price is not None:
        sentences.append(
            f"Sources: {discretionary.TAX_YEAR} federal and California tax rules, ACS housing costs, "
            "and the BLS Consumer Expenditure Surveys."
        )
    return " ".join(sentences)


def zip_insight(
    screen: PersonaRank,
    row: dict,
    position: int,
    price: Decimal | None = None,
    metrics: dict | None = None,
    procedure: str = "",
) -> str:
    metrics = metrics if metrics is not None else zip_metrics(row, screen.persona, price)
    city, _ = split_place(row)
    place = f"{display_city(city)} ({row['zip']})" if city else f"ZIP code {row['zip']}"
    group = screen.persona.age_phrase
    standing = (
        f"Why {place}: it ranks {ordinal(position)} in this list with a score of "
        f"{row['composite_score']} out of 100"
    )
    drivers = driver_sentence(row, screen.persona)
    if drivers:
        standing += f", driven most by {drivers}."
    else:
        standing += (
            f". No measure here is above the median of ZIP codes within "
            f"{practice.MAX_PRACTICE_DISTANCE_MILES} miles."
        )
    sentences = [standing, f"{format_percent(row['age_share'])} of adults here are {group}."]
    household = format_money(Decimal(row["median_household_income"]))
    listing = format_money(row["median_listing_price_per_square_foot"])
    if screen.persona.cash == "working":
        sentences.append(
            f"Median household income is {household} and median listing price per square foot "
            f"is {listing}, both at or above the median of {practice.AREA_PHRASE}."
        )
    else:
        sentences.append(
            f"Median listing price per square foot is {listing} and per-person income is "
            f"{format_money(Decimal(row['per_capita_income']))}, both at or above the median of "
            f"{practice.AREA_PHRASE}."
        )
    money = money_sentence(row, screen.persona)
    if money:
        sentences.append(money)
    if price is not None:
        if metrics["affordable_households"] is not None:
            count = metrics["affordable_households"]
            noun = "household" if count == 1 else "households"
            sentences.append(
                f"About {metrics['affordable_households_share']}% of households headed by someone "
                f"{screen.persona.householder_phrase} here ({count:,} {noun}) earn at least "
                f"{format_money(metrics['income_needed'])}, "
                f"enough to cover the {format_money(price)} fee."
            )
    residents = row.get("female_share")
    in_band = metrics["women_share_of_age_range"]
    if residents is not None and in_band is not None:
        sentences.append(
            f"Women are {format_percent(residents)} of residents and {in_band}% of adults {group}."
        )
    elif residents is not None:
        sentences.append(f"Women are {format_percent(residents)} of residents.")
    elif in_band is not None:
        sentences.append(f"Women are {in_band}% of adults {group}.")
    described = describe_sentence(row)
    if described:
        sentences.append(described)
    housing = housing_work_sentence(metrics)
    if housing:
        sentences.append(housing)
    if procedure in MOTHER_PROCEDURES and metrics["mothers_with_children_under_18"] is not None:
        mothers = f"About {metrics['mothers_with_children_under_18']:,} women ages 20 to 64 here are raising children under 18"
        if metrics["mothers_with_children_under_6"] is not None:
            mothers += f", including {metrics['mothers_with_children_under_6']:,} with a child under 6"
        sentences.append(mothers + ".")
    return " ".join(sentences)


def money_sentence(row: dict, persona: Persona) -> str:
    """Taxes, essentials, and discretionary spending in one sentence. Nothing when they are blank."""
    if row.get("discretionary_income") is None:
        return ""
    household = f"a typical household headed by someone {persona.householder_phrase} here"
    costs = (
        f"After an estimated {format_money(row['estimated_taxes'])} in taxes and "
        f"{format_money(row['average_cost_of_living'])} in essential costs"
    )
    income = format_money(row["household_income_for_age_range"])
    left = row["money_left_after_essentials"]
    if left <= 0:
        return f"{costs}, {household} has nothing left of its {income} income for discretionary items."
    return (
        f"{costs}, {household} has about {format_money(left)} left of its {income} income "
        f"and spends about {row['discretionary_share_of_remaining']}% of that, roughly "
        f"{format_money(row['discretionary_income'])} a year, on discretionary items."
    )


def housing_work_sentence(metrics: dict) -> str:
    """Home ownership and women's management and professional jobs. Nothing when both are blank."""
    clauses = []
    if metrics["home_ownership"] == "own":
        clauses.append(f"{metrics['owner_occupied_share']}% of households own their home")
    elif metrics["home_ownership"] == "rent":
        clauses.append(f"{metrics['renter_occupied_share']}% of households rent their home")
    if metrics["women_management_professional_share"] is not None:
        clauses.append(
            f"{metrics['women_management_professional_share']}% of employed women work in "
            "management, business, science, or arts jobs"
        )
    if not clauses:
        return ""
    return "Here, " + ", and ".join(clauses) + "."


def count_sum(profile: dict, columns: list[str]) -> int | None:
    values = [profile.get(column) for column in columns]
    if any(value is None for value in values):
        return None
    return sum(values)


def percent_of(part: int | Decimal | None, total: int | Decimal | None) -> Decimal | None:
    """Percentage points to one decimal, the number the text shows. Blank without a positive total."""
    if part is None or total is None or total <= 0:
        return None
    return percent_points(Decimal(part) / Decimal(total))


def income_needed(row: dict, price: Decimal, persona: Persona) -> int | None:
    """Household income whose discretionary income covers the fee with this ZIP's costs."""
    living = row.get("cost_of_living")
    if living is None:
        return None
    return discretionary.income_needed(price, living, persona.householder_band)


def households_at_or_above(
    profile: dict, threshold: Decimal, columns: tuple[str, ...] | None = None
) -> Decimal | None:
    """Estimated households with income at or above the threshold, from income brackets.

    `columns` are the 16 bracket counts in HOUSEHOLD_INCOME_BRACKETS order: the
    B19037 brackets of one householder-age band, or B19001 when not given.

    Inside a closed bracket the households are spread evenly, so the part at
    or above the threshold is linear. At or above $200,000 the open bracket
    uses a Pareto tail fitted to the $150,000 and $200,000 bounds.
    """
    if columns is None:
        columns = tuple(column for column, _code, _phrase, _low in census.HOUSEHOLD_INCOME_BRACKETS)
    counts = [profile.get(column) for column in columns]
    if any(count is None for count in counts):
        return None
    lows = [Decimal(low) for _column, _code, _phrase, low in census.HOUSEHOLD_INCOME_BRACKETS]
    if threshold <= 0:
        return Decimal(sum(counts))
    if threshold >= lows[-1]:
        return pareto_tail(counts[-2] + counts[-1], counts[-1], lows[-2], lows[-1], threshold)
    for index in range(len(lows) - 1):
        low, high = lows[index], lows[index + 1]
        if low <= threshold < high:
            inside = Decimal(counts[index]) * (high - threshold) / (high - low)
            return Decimal(sum(counts[index + 1 :])) + inside
    raise SystemExit("The household income brackets do not cover the threshold.")


def pareto_tail(
    at_or_above_lower: int, at_or_above_top: int, lower: Decimal, top: Decimal, threshold: Decimal
) -> Decimal | None:
    """Pareto estimate of households at or above a threshold in the open top bracket.

    The shape comes from the households at or above the two bounds. It is
    blank when no household falls between those bounds, because the tail
    cannot be fitted.
    """
    if at_or_above_top == 0:
        return Decimal(0)
    if at_or_above_lower <= at_or_above_top:
        return None
    alpha = (Decimal(at_or_above_lower) / Decimal(at_or_above_top)).ln() / (top / lower).ln()
    return Decimal(at_or_above_top) * (top / threshold) ** alpha


def zip_metrics(row: dict, persona: Persona, price: Decimal | None) -> dict:
    """List values for one ZIP row. discretionary_income, copied from the row, is the screen."""
    profile = row.get("profile") or {}
    women = count_sum(profile, [f"female_{column}" for column in persona.age_columns])
    men = count_sum(profile, [f"male_{column}" for column in persona.age_columns])
    owners = profile.get("owner_occupied_households")
    renters = profile.get("renter_occupied_households")
    occupied = None if owners is None or renters is None else owners + renters
    owner_share = percent_of(owners, occupied)
    band = persona.householder_band
    band_owners = profile.get(f"owner_occupied_householder_{band}")
    band_renters = profile.get(f"renter_occupied_householder_{band}")
    band_total = None if band_owners is None or band_renters is None else band_owners + band_renters
    employed = profile.get("employed_women")
    metrics = {
        "women_in_age_range": women,
        "men_in_age_range": men,
        "women_share_of_age_range": percent_of(
            women, None if women is None or men is None else women + men
        ),
        "income_needed": None,
        "affordable_households_share": None,
        "affordable_households": None,
        "mothers_with_children_under_18": profile.get("mothers_with_children_under_18"),
        "mothers_with_children_under_6": profile.get("mothers_with_children_under_6"),
        "recent_births": profile.get("recent_births"),
        "home_ownership": None if owner_share is None else ("own" if owner_share >= 50 else "rent"),
        "owner_occupied_share": owner_share,
        "renter_occupied_share": percent_of(renters, occupied),
        "owner_occupied_share_age_range": percent_of(band_owners, band_total),
        "women_management_professional_share": percent_of(
            profile.get("women_management_business_science_arts"), employed
        ),
        "women_arts_entertainment_media_share": percent_of(
            profile.get("women_arts_design_entertainment_sports_media"), employed
        ),
    }
    for name in discretionary.FIELD_TYPES:
        metrics[name] = row.get(name)
    needed = None if price is None else income_needed(row, price, persona)
    if needed is not None:
        metrics["income_needed"] = needed
        columns = census.householder_income_columns(persona.householder_band)
        affordable = households_at_or_above(profile, Decimal(needed), columns)
        brackets = count_sum(profile, list(columns))
        if affordable is not None and brackets:
            metrics["affordable_households"] = int(
                affordable.quantize(Decimal(1), rounding=ROUND_HALF_UP)
            )
            metrics["affordable_households_share"] = percent_of(affordable, brackets)
    return metrics


def describe_sentence(row: dict) -> str:
    """Race, ethnicity, and education in one plain sentence. Nothing when both are blank."""
    race = row.get("race_and_ethnicity")
    degree = row.get("bachelors_or_higher_share")
    degree_clause = f"{format_percent(degree)} of adults 25 and older" if degree is not None else ""
    if race and degree_clause:
        return (
            f"{race} residents are the largest group at "
            f"{format_percent(row['race_and_ethnicity_share'])}, and {degree_clause} "
            "have a bachelor's degree or higher."
        )
    if race:
        return (
            f"{race} residents are the largest group at "
            f"{format_percent(row['race_and_ethnicity_share'])}."
        )
    if degree_clause:
        return f"Of adults 25 and older, {format_percent(degree)} have a bachelor's degree or higher."
    return ""


def affordable_rows(screen: PersonaRank, price: Decimal | None) -> list[dict]:
    """The persona's ZIPs that pass the cash screen and, with a price, cover it."""
    candidates = screen.passed if screen.passed else screen.rows
    if price is None:
        return list(candidates)
    return [row for row in candidates if covers_price(row, price)]


def percentile_ranks(values: dict[str, Decimal | None]) -> dict[str, Decimal]:
    """Each ZIP's percentile rank among the non-blank values, from 0 to 1.

    Tied values share the middle of their ranks. A blank value, or a factor
    with fewer than two values, gets NEUTRAL_PERCENTILE.
    """
    present = sorted(value for value in values.values() if value is not None)
    count = len(present)
    ranks: dict[str, Decimal] = {}
    for zip_code, value in values.items():
        if value is None or count < 2:
            ranks[zip_code] = NEUTRAL_PERCENTILE
            continue
        below = bisect_left(present, value)
        equal = bisect_right(present, value) - below
        ranks[zip_code] = (Decimal(below) + Decimal(equal - 1) / 2) / Decimal(count - 1)
    return ranks


def factor_value(factor: str, row: dict, metrics: dict) -> Decimal | None:
    if factor == "age_share":
        return row["age_share"]
    value = metrics.get(factor)
    if value is None:
        return None
    if factor in ranking_weights.PER_ADULT_FACTORS:
        adults = row.get("adult_population")
        return Decimal(value) / Decimal(adults) if adults else None
    return Decimal(value)


def score_screen(procedure: str, screen: PersonaRank, price: Decimal | None = None) -> list[dict]:
    """One procedure list for one persona, ranked by composite_score, top 25.

    The hard screens come first: the cash screen, then, with a price, the
    affordability screen. Percentile ranks are taken over every eligible ZIP
    within 50 miles, including ZIPs a screen dropped.
    """
    candidates = affordable_rows(screen, price)
    if not candidates:
        return []
    weights = ranking_weights.weights(procedure, price is not None)
    pool = list(screen.eligible)
    listed = {row["zip"] for row in pool}
    pool += [row for row in candidates if row["zip"] not in listed]
    metrics = {row["zip"]: zip_metrics(row, screen.persona, price) for row in pool}
    percentiles = {
        factor: percentile_ranks(
            {row["zip"]: factor_value(factor, row, metrics[row["zip"]]) for row in pool}
        )
        for factor in weights
    }
    scored: list[dict] = []
    for row in candidates:
        zip_code = row["zip"]
        total = Decimal(0)
        parts: dict[str, Decimal | None] = dict.fromkeys(SCORE_COLUMNS)
        for factor, weight in weights.items():
            part = Decimal(100) * weight * percentiles[factor][zip_code]
            total += part
            parts[f"score_{factor}"] = part.quantize(SCORE_PLACES, rounding=ROUND_HALF_UP)
        scored.append(
            {
                **row,
                **metrics[zip_code],
                "procedure": procedure,
                "surgeon_fee_midpoint": price,
                "persona": screen.persona.label,
                "household_income_middle": screen.household_income_middle,
                "per_capita_income_middle": screen.per_capita_income_middle,
                "listing_price_middle": screen.listing_price_middle,
                "score": total,
                "composite_score": total.quantize(COMPOSITE_PLACES, rounding=ROUND_HALF_UP),
                **parts,
                "percentiles": {factor: percentiles[factor][zip_code] for factor in weights},
            }
        )
    scored.sort(
        key=lambda row: (
            -row["score"],
            -row["age_share"],
            -cash_signal(row, screen.persona),
            row["zip"],
        )
    )
    ranked = scored[:TOP_N]
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position
    return ranked


def score_drivers(row: dict) -> list[str]:
    """Up to three factors above the median that add the most to the score."""
    ranks = row.get("percentiles") or {}
    order = sorted(
        ranks, key=lambda factor: (-row[f"score_{factor}"], ranking_weights.FACTORS.index(factor))
    )
    return [factor for factor in order if ranks[factor] > NEUTRAL_PERCENTILE][:3]


def percentile_phrase(rank: Decimal) -> str:
    if rank >= 1:
        return "the highest within 50 miles"
    points = int((rank * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"{ordinal(min(99, max(1, points)))} percentile"


def plain_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def persona_words(template: str, persona: Persona) -> str:
    return template.format(group=persona.age_phrase, householder=persona.householder_phrase)


def driver_sentence(row: dict, persona: Persona) -> str:
    ranks = row.get("percentiles") or {}
    drivers = [
        f"{persona_words(ranking_weights.DRIVER_PHRASES[factor], persona)} "
        f"({percentile_phrase(ranks[factor])})"
        for factor in score_drivers(row)
    ]
    return plain_list(drivers)


def ranking_sentence(procedure: str, persona: Persona, price: Decimal | None) -> str:
    weights = ranking_weights.weights(procedure, price is not None)
    measures = [
        f"{persona_words(ranking_weights.FACTOR_LABELS[factor], persona)} "
        f"({int((weight * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))}%)"
        for factor, weight in sorted(weights.items(), key=lambda item: -item[1])
    ]
    return (
        "ZIP codes are ranked by a score from 0 to 100 that compares each one with the other "
        f"ZIP codes within {practice.MAX_PRACTICE_DISTANCE_MILES} miles on these weighted "
        f"measures: {plain_list(measures)}."
    )


def render_screen(
    procedure: str,
    screen: PersonaRank,
    price: Decimal | None = None,
    scored: list[dict] | None = None,
) -> list[BulletRow]:
    header = screen_header(procedure, screen, price)
    scored = score_screen(procedure, screen, price) if scored is None else scored
    rows: list[BulletRow] = []
    for position, row in enumerate(scored, start=1):
        city, state = split_place(row)
        rows.append(
            BulletRow(
                header=header,
                rank=position,
                city=city,
                state=state,
                zip_code=str(row["zip"]),
                practice_zip_code=row.get("practice_zip_code"),
                county=row.get("county"),
                distance_from_practice_miles=row.get("distance_from_practice_miles"),
                insight=zip_insight(screen, row, position, price, row, procedure),
                household_income=row["median_household_income"],
                individual_income=row["per_capita_income"],
                median_listing_price_per_square_foot=row["median_listing_price_per_square_foot"],
                age_range=screen.persona.label,
                sex=sex_label(row.get("female_share")),
                race_and_ethnicity=row.get("race_and_ethnicity"),
                education_level=row.get("education_level"),
                **{name: row[name] for name in DESCRIPTIVE_LIST_NAMES},
                composite_score=row["composite_score"],
                scores={name: row[name] for name in SCORE_COLUMNS},
            )
        )
    return rows


ProcedureLists = dict[str, list[tuple[PersonaRank, list[dict]]]]


def procedure_lists(
    procedures_in_year: list[str],
    screens: list[PersonaRank],
    prices: dict[str, Decimal | None] | None = None,
) -> ProcedureLists:
    """Scored persona lists for each procedure, in persona-list order."""
    prices = prices or {}
    by_label = {screen.persona.label: screen for screen in screens}
    lists: ProcedureLists = {}
    for name in procedures_in_year:
        price = prices.get(name)
        lists[name] = [
            (by_label[label], score_screen(name, by_label[label], price))
            for label in PROCEDURE_PERSONAS.get(name, ())
        ]
    return lists


def bullets_from_lists(
    lists: ProcedureLists, prices: dict[str, Decimal | None] | None = None
) -> dict[str, list[BulletRow]]:
    prices = prices or {}
    return {
        name: [
            row
            for screen, scored in persona_lists
            for row in render_screen(name, screen, prices.get(name), scored)
        ]
        for name, persona_lists in lists.items()
    }


def bullet_rows(
    procedure: str, screens: list[PersonaRank], price: Decimal | None = None
) -> list[BulletRow]:
    """ZIP rows for one procedure's persona lists, in list order. A group with no ZIP adds none."""
    lists = procedure_lists([procedure], screens, {procedure: price})
    return bullets_from_lists(lists, {procedure: price})[procedure]


def bullets_by_procedure(
    procedures_in_year: list[str],
    screens: list[PersonaRank],
    prices: dict[str, Decimal | None] | None = None,
) -> dict[str, list[BulletRow]]:
    return bullets_from_lists(procedure_lists(procedures_in_year, screens, prices), prices)


def fee_midpoint(low: object, high: object) -> Decimal | None:
    """Midpoint of the published surgeon fee range. A blank end means no price."""
    if not fees_are_present(low, high):
        return None
    return (Decimal(str(low)) + Decimal(str(high))) / Decimal(2)


RANK_DISTANCE_PLACES = Decimal("0.1")


def flatten(lists: ProcedureLists) -> list[dict]:
    """cash_pay_zip_rank rows, one per procedure, persona, and ranked ZIP.

    Every row has the practice ZIP and its distance to one decimal.
    """
    rows: list[dict] = []
    for persona_lists in lists.values():
        for row in (item for _screen, scored in persona_lists for item in scored):
            distance = row.get("distance_from_practice_miles")
            if row.get("practice_zip_code") != practice.PRACTICE_ZIP_CODE or practice.beyond_distance(distance):
                raise SystemExit(f"ZIP {row['zip']} is outside the practice area but was ranked.")
            rows.append(
                {
                    **row,
                    "distance_from_practice_miles": Decimal(str(distance)).quantize(
                        RANK_DISTANCE_PLACES, rounding=ROUND_HALF_UP
                    ),
                }
            )
    return rows


def csv_cell(column: str, value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def write_rank_csv(rows: list[dict], path: Path | None = None) -> None:
    path = OUTPUT_CSV if path is None else path
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=RANK_COLUMNS,
                lineterminator="\n",
                extrasaction="ignore",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {column: csv_cell(column, row.get(column)) for column in RANK_COLUMNS}
                )
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def schema_sql() -> str:
    lines = []
    for name in RANK_COLUMNS:
        ident = quote_ident(name)
        null_sql = " NOT NULL" if name in NOT_NULL_COLUMNS else ""
        lines.append(f"    {ident} {COLUMN_TYPES[name]}{null_sql}")
    lines.append(f"    {quote_ident(dataset_stamp.COLUMN)} {dataset_stamp.SQL_TYPE}")
    body = ",\n".join(lines)
    return (
        f"CREATE TABLE IF NOT EXISTS {RANK_TABLE} (\n"
        f"{body},\n"
        f"    PRIMARY KEY ({', '.join(RANK_KEY)})\n"
        ");\n"
    )


RANK_KEY = ("procedure", "persona", "zip")
RANK_KEY_NAME = f"{RANK_TABLE_NAME}_pkey"


def rank_key_columns(cur) -> list[str]:
    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.table_schema = tc.table_schema
        WHERE tc.table_schema = 'public'
          AND tc.table_name = %s
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """,
        (RANK_TABLE_NAME,),
    )
    return [str(name) for (name,) in (cur.fetchall() or [])]


def ensure_rank_key(cur) -> None:
    """Key the emptied table on procedure, persona, and ZIP. An older key was persona and ZIP."""
    if rank_key_columns(cur) == list(RANK_KEY):
        return
    cur.execute(f"ALTER TABLE {RANK_TABLE} DROP CONSTRAINT IF EXISTS {RANK_KEY_NAME}")
    for name in RANK_KEY:
        cur.execute(f"ALTER TABLE {RANK_TABLE} ALTER COLUMN {quote_ident(name)} SET NOT NULL")
    cur.execute(
        f"ALTER TABLE {RANK_TABLE} ADD CONSTRAINT {RANK_KEY_NAME} PRIMARY KEY ({', '.join(RANK_KEY)})"
    )


def ensure_rank_columns(cur) -> None:
    cur.execute(schema_sql())
    for name in RANK_COLUMNS:
        cur.execute(
            f"ALTER TABLE {RANK_TABLE} ADD COLUMN IF NOT EXISTS "
            f"{quote_ident(name)} {COLUMN_TYPES[name]}"
        )
    cur.execute(
        f"ALTER TABLE {RANK_TABLE} ADD COLUMN IF NOT EXISTS "
        f"{quote_ident(dataset_stamp.COLUMN)} {dataset_stamp.SQL_TYPE}"
    )
    cur.execute(
        f"ALTER TABLE {RANK_TABLE} ALTER COLUMN "
        f"{quote_ident('median_listing_price')} DROP NOT NULL"
    )


def rank_db_value(column: str, row: dict):
    value = row.get(column)
    if column in INTEGER_RANK_COLUMNS:
        if value is None or value == "":
            return None
        return int(value)
    if COLUMN_TYPES[column] == "numeric":
        if value is None or value == "":
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))
    if value is None:
        return ""
    return str(value)


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
          AND table_type = 'BASE TABLE'
        """,
        (table_name,),
    )
    return cur.fetchone() is not None


def fetch_procedure_rows(cur) -> list[tuple[int, str, object, object]]:
    if not table_exists(cur, procedures.TABLE_NAME):
        raise SystemExit(f"{procedures.TABLE} is missing.")
    cur.execute(
        f"SELECT report_year, procedure, surgeon_fee_low, surgeon_fee_high "
        f"FROM {procedures.TABLE} "
        "ORDER BY report_year, procedure"
    )
    found = [
        (int(year), str(name), low, high)
        for year, name, low, high in cur.fetchall()
    ]
    if not found:
        raise SystemExit(f"{procedures.TABLE} has no rows.")
    return found


def join_acs_year(rows: list[dict[str, str]]) -> int:
    """Read acs_year on zip_census_redfin_join. That is the preferred report year."""
    if not rows:
        raise SystemExit("The join has no rows.")
    years: set[int] = set()
    for row in rows:
        year = as_int(row.get("acs_year"), "acs_year")
        if year is None:
            raise SystemExit("The join is missing acs_year.")
        years.add(year)
    if len(years) != 1:
        listed = ", ".join(str(year) for year in sorted(years))
        raise SystemExit(f"The join has more than one acs_year: {listed}.")
    return years.pop()


def fees_are_present(low: object, high: object) -> bool:
    return low is not None and high is not None


def report_year_qualifies(fee_pairs: list[tuple[object, object]]) -> bool:
    """Both fee columns are non-null on more than half of the procedure rows."""
    if not fee_pairs:
        return False
    filled = sum(1 for low, high in fee_pairs if fees_are_present(low, high))
    return filled * 2 > len(fee_pairs)


def select_bullet_report_year(
    preferred_year: int,
    procedure_rows: list[tuple[int, str, object, object]],
) -> int:
    """Use the join year when it qualifies. Otherwise the newest year that does."""
    grouped: dict[int, list[tuple[object, object]]] = {}
    for year, _name, low, high in procedure_rows:
        grouped.setdefault(int(year), []).append((low, high))
    if report_year_qualifies(grouped.get(preferred_year, [])):
        return preferred_year
    qualifying = [
        year for year, pairs in grouped.items() if report_year_qualifies(pairs)
    ]
    if not qualifying:
        raise SystemExit(
            "No procedure report_year has surgeon_fee_low and surgeon_fee_high "
            "on more than half of its rows."
        )
    return max(qualifying)


def procedure_column_types(cur) -> dict[str, str]:
    cur.execute(
        """
        SELECT column_name, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        (procedures.TABLE_NAME,),
    )
    return {str(name): str(udt) for name, udt in (cur.fetchall() or [])}


def ensure_bullet_columns(cur) -> None:
    """Add the same-order bullet lists beside marketing_bullets. An old bullet_rank becomes rank."""
    procedures.ensure_marketing_bullets(cur)
    found = procedure_column_types(cur)
    if LEGACY_BULLET_RANK in found:
        if BULLET_RANK in found:
            raise SystemExit(
                f"{procedures.TABLE} has both {LEGACY_BULLET_RANK} and {BULLET_RANK}."
            )
        cur.execute(
            f"ALTER TABLE {procedures.TABLE} RENAME COLUMN "
            f"{quote_ident(LEGACY_BULLET_RANK)} TO {quote_ident(BULLET_RANK)}"
        )
        if found[LEGACY_BULLET_RANK] != "_int4":
            cur.execute(
                f"ALTER TABLE {procedures.TABLE} ALTER COLUMN {BULLET_RANK} "
                f"TYPE integer[] USING CASE WHEN {BULLET_RANK} IS NULL THEN NULL "
                f"ELSE ARRAY[{BULLET_RANK}::integer] END"
            )
    for name, sql_type in BULLET_LIST_COLUMNS:
        cur.execute(
            f"ALTER TABLE {procedures.TABLE} ADD COLUMN IF NOT EXISTS "
            f"{quote_ident(name)} {sql_type}"
        )


def bullet_update(rows: list[BulletRow]) -> tuple:
    """marketing_bullets holds one header line per ZIP row. No ZIP rows stores null."""
    if not rows:
        return (None,) * (1 + len(BULLET_LIST_COLUMNS))
    return ("\n".join(row.header for row in rows),) + tuple(
        [row.value(name) for row in rows] for name in BULLET_LIST_NAMES
    )


def procedure_prices(
    procedure_rows: list[tuple[int, str, object, object]], preferred_year: int
) -> tuple[int, list[str], dict[str, Decimal | None]]:
    """The bullet report year, its procedures, and each fee midpoint."""
    chosen = select_bullet_report_year(preferred_year, procedure_rows)
    names = [name for year, name, _low, _high in procedure_rows if year == chosen]
    prices = {
        name: fee_midpoint(low, high)
        for year, name, low, high in procedure_rows
        if year == chosen
    }
    return chosen, names, prices


def apply_marketing_bullets(
    cur,
    screens: list[PersonaRank],
    procedure_rows: list[tuple[int, str, object, object]],
    preferred_year: int,
    lists: ProcedureLists | None = None,
) -> tuple[int, int]:
    """Write bullet rows on one qualifying report year and blank every other year.

    Surgeon fee columns are not updated. A blank fee stays blank.
    """
    ensure_bullet_columns(cur)
    chosen, names, prices = procedure_prices(procedure_rows, preferred_year)
    assignments = ", ".join(
        f"{quote_ident(name)} = %s" for name in BULLET_CSV_COLUMNS
    )
    cleared = ", ".join(f"{quote_ident(name)} = NULL" for name in BULLET_CSV_COLUMNS)
    cur.execute(
        f"UPDATE {procedures.TABLE} SET {cleared} WHERE report_year <> %s",
        (chosen,),
    )
    if lists is None:
        lists = procedure_lists(names, screens, prices)
    bullets = bullets_from_lists(lists, prices)
    updates = [bullet_update(bullets[name]) + (chosen, name) for name in names]
    if updates:
        cur.executemany(
            f"UPDATE {procedures.TABLE} SET {assignments} "
            "WHERE report_year = %s AND procedure = %s",
            updates,
        )
    return chosen, len(updates)


def save_rank(
    conn,
    screens: list[PersonaRank],
    updated_at: datetime | None = None,
    *,
    preferred_year: int,
) -> tuple[int, int, int, list[dict]]:
    """Replace the rank snapshot and write procedure bullets. Do not drop tables.

    The rank rows are the procedure lists for the bullet report year, so they
    use that year's fee midpoints. They are returned for the CSV.
    """
    if "redfin_real_estate_value" in DELETE_RANK_SQL:
        raise SystemExit("The rank refresh must not delete Redfin rows.")
    updated_at = dataset_stamp.stamp(updated_at)
    stored = RANK_COLUMNS + (dataset_stamp.COLUMN,)
    column_list = ", ".join(quote_ident(name) for name in stored)
    placeholders = ", ".join(["%s"] * len(stored))
    insert_sql = f"INSERT INTO {RANK_TABLE} ({column_list}) VALUES ({placeholders})"
    with conn.transaction():
        with conn.cursor() as cur:
            procedure_rows = fetch_procedure_rows(cur)
            _year, names, prices = procedure_prices(procedure_rows, preferred_year)
            lists = procedure_lists(names, screens, prices)
            rows = flatten(lists)
            _updated_at, stamped = dataset_stamp.stamp_rows(
                [tuple(rank_db_value(name, row) for name in RANK_COLUMNS) for row in rows],
                updated_at,
            )
            ensure_rank_columns(cur)
            cur.execute(DELETE_RANK_SQL)
            ensure_rank_key(cur)
            if stamped:
                cur.executemany(insert_sql, stamped)
            year, bullet_count = apply_marketing_bullets(
                cur, screens, procedure_rows, preferred_year, lists
            )
            cur.execute(f"SELECT COUNT(*) FROM {RANK_TABLE}")
            count = cur.fetchone()[0]
    if count != len(rows):
        raise SystemExit(f"{RANK_TABLE} has {count} rows after loading {len(rows)}.")
    return count, year, bullet_count, rows


def procedure_csv_cell(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return load_california.csv_value(value)


def marketing_bullet_cells(value: object) -> list[str]:
    """One header cell per stored line, one line per ZIP row. Blank text has none."""
    if value is None:
        return []
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def per_row_values(value: object, count: int, column: str) -> list[object]:
    """Element i of a stored same-order list goes with ZIP row i."""
    if value is None:
        return [None] * count
    if not isinstance(value, (list, tuple)):
        raise SystemExit(f"{procedures.TABLE}.{column} is not a list.")
    if len(value) != count:
        raise SystemExit(
            f"{procedures.TABLE}.{column} has {len(value)} values for {count} ZIP rows."
        )
    return list(value)


def bullet_csv_columns(columns: list[str]) -> list[str]:
    """Table columns in table order, then the bullet fields in their fixed order."""
    return [name for name in columns if name not in BULLET_CSV_COLUMNS] + list(
        BULLET_CSV_COLUMNS
    )


def expand_marketing_bullet_rows(columns: list[str], rows: list[tuple]) -> list[tuple]:
    """One row per ranked ZIP, with report_year, procedure, and fees repeated.

    A procedure year with no ZIP rows stays one row with the bullet fields blank.
    Rows follow bullet_csv_columns(columns).
    """
    if procedures.MARKETING_BULLETS not in columns:
        raise SystemExit(
            f"{procedures.TABLE} is missing {procedures.MARKETING_BULLETS}."
        )
    kept = [index for index, name in enumerate(columns) if name not in BULLET_CSV_COLUMNS]
    header_index = columns.index(procedures.MARKETING_BULLETS)
    expanded: list[tuple] = []
    for row in rows:
        if len(row) != len(columns):
            raise SystemExit(
                f"{procedures.OUTPUT_CSV_NAME} row does not match its columns."
            )
        base = [row[index] for index in kept]
        headers = marketing_bullet_cells(row[header_index])
        lists = [
            per_row_values(
                row[columns.index(name)] if name in columns else None,
                len(headers),
                name,
            )
            for name in BULLET_LIST_NAMES
        ]
        if headers and all(value is None for value in lists[0]):
            raise SystemExit(
                f"{procedures.TABLE} has {procedures.MARKETING_BULLETS} without ZIP rows."
            )
        if not headers:
            expanded.append(tuple(base + [None] * len(BULLET_CSV_COLUMNS)))
            continue
        for position, header in enumerate(headers):
            expanded.append(
                tuple(base + [header] + [values[position] for values in lists])
            )
    return expanded


def procedure_fee_columns(cur) -> list[str]:
    """Every column on the fee table after the rank write, in table order."""
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (procedures.TABLE_NAME,),
    )
    columns: list[str] = []
    for (name,) in cur.fetchall():
        column = str(name)
        quote_ident(column)
        columns.append(column)
    if procedures.MARKETING_BULLETS not in columns:
        raise SystemExit(
            f"{procedures.TABLE} is missing {procedures.MARKETING_BULLETS}."
        )
    return columns


def fetch_procedure_fee_rows(cur, columns: list[str]) -> list[tuple]:
    """Read every report year. Do not drop or change the fee table."""
    quoted = ", ".join(quote_ident(name) for name in columns)
    order = [
        quote_ident(name)
        for name in ("report_year", "procedure")
        if name in columns
    ]
    sql = f"SELECT {quoted} FROM {procedures.TABLE}"
    if order:
        sql += " ORDER BY " + ", ".join(order)
    collapsed = " ".join(sql.split()).upper()
    if not collapsed.startswith("SELECT ") or any(
        f" {word} " in f" {collapsed} "
        for word in ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE")
    ):
        raise SystemExit("The procedure fee export must not change the table.")
    cur.execute(sql)
    rows = [tuple(row) for row in cur.fetchall()]
    if not rows:
        raise SystemExit(f"{procedures.TABLE} has no rows.")
    return rows


def write_procedure_fee_csv(
    columns: list[str], rows: list[tuple], path: Path | None = None
) -> int:
    """Write one CSV row per marketing bullet. Cells have no line breaks."""
    path = procedures.OUTPUT_CSV if path is None else path
    expanded = expand_marketing_bullet_rows(columns, rows)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(bullet_csv_columns(columns))
            for row in expanded:
                writer.writerow([procedure_csv_cell(value) for value in row])
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return len(expanded)


def export_procedure_fees(conn, path: Path | None = None) -> int:
    """Write procedure_surgeon_fees.csv from the table the rank just updated."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            columns = procedure_fee_columns(cur)
            rows = fetch_procedure_fee_rows(cur, columns)
            return write_procedure_fee_csv(columns, rows, path)


def database_url() -> str:
    return os.environ.get(DATABASE_URL_ENV, "").strip()


def public_error(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.lower()
    if "://" in text or "postgres" in lowered or "key=" in lowered or "password" in lowered:
        return "Could not rank cash-pay ZIPs."
    if text:
        return text
    return "Could not rank cash-pay ZIPs."


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("Usage: python rank_cash_pay_zips.py JOIN_CSV")
    join_rows = read_join_csv(Path(args[0]))
    preferred_year = join_acs_year(join_rows)
    screens = rank_personas(join_rows)
    summary = ", ".join(f"{len(screen.passed)} for {screen.persona.label}" for screen in screens)
    print(f"ZIPs passing the cash screen: {summary}.")
    if not database_url():
        rows = flatten(procedure_lists(list(PROCEDURE_PERSONAS), screens))
        write_rank_csv(rows)
        print(
            f"{DATABASE_URL_ENV} is not set, so no surgeon fee was read and no list was "
            f"screened for affordability. Wrote {len(rows)} rows to {OUTPUT_CSV_NAME}. "
            f"Left {RANK_TABLE} and {procedures.TABLE} unchanged."
        )
        return 0
    updated_at = dataset_stamp.stamp()
    print(f"dataset_updated_at: {updated_at.isoformat()}")
    try:
        with load_california.connect() as conn:
            count, year, bullet_count, rows = save_rank(
                conn, screens, updated_at, preferred_year=preferred_year
            )
            write_rank_csv(rows)
            fee_count = export_procedure_fees(conn)
    except SystemExit as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    print(f"Wrote {len(rows)} rows to {OUTPUT_CSV_NAME}.")
    print(
        f"Replaced {RANK_TABLE} with {count} rows. "
        f"Wrote {procedures.MARKETING_BULLETS} on {bullet_count} rows "
        f"for report_year {year}. Other report years are blank in that column. "
        f"Wrote {fee_count} rows to {procedures.OUTPUT_CSV_NAME}. "
        f"{load_california.TABLE} was not modified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
