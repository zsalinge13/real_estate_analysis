"""Weights for the composite ZIP score, the one place to change how ZIPs rank.

Every procedure and persona list is ranked by composite_score, from 0 to 100,
after the hard screens: the income and listing-price medians and the
discretionary affordability screen. Each factor is a percentile rank among the
eligible ZIPs within 50 miles of the practice: 0 for the lowest value, 1 for
the highest, ties share the middle rank, and a blank value counts as 0.5. The
weights a list uses are scaled to add up to 1, so

    composite_score = 100 * sum(weight * percentile)

and score_<factor> is that factor's part of the sum.

Defaults:
- age_share, the persona's share of adults: 0.30, the largest weight.
- women_share_of_age_range: 0.20 for breast procedures, 0.10 for body
  procedures, 0.05 for face procedures and rhinoplasty.
- affordable_households_share: 0.15. affordable_households: 0.10. A percentile
  rank is the same for a count and its log, so the count is used. Both are
  left out when the procedure has no surgeon fee.
- mothers per adult, for MOTHER_PROCEDURES only: children under 18 0.04,
  children under 6 0.03, recent births 0.03.
- owner_occupied_share_age_range: 0.10.
- women_management_professional_share: 0.10.
- women_arts_entertainment_media_share: 0.05, for face procedures and
  rhinoplasty only.

Race, ethnicity, education, and lifestyle segment are never factors.
"""

from __future__ import annotations

from decimal import Decimal

BREAST_PROCEDURES = frozenset({"breast augmentation", "breast lift", "breast reduction"})
BODY_PROCEDURES = frozenset(
    {"liposuction", "tummy tuck", "arm lift", "thigh lift", "buttock augmentation with fat grafting"}
)
FACE_PROCEDURES = frozenset(
    {"rhinoplasty", "eyelid surgery", "facelift", "neck lift", "forehead lift"}
)
MOTHER_PROCEDURES = frozenset(
    {"tummy tuck", "breast lift", "breast augmentation", "breast reduction", "liposuction"}
)

AGE_SHARE_WEIGHT = Decimal("0.30")
WOMEN_SHARE_WEIGHTS = {
    "breast": Decimal("0.20"),
    "body": Decimal("0.10"),
    "face": Decimal("0.05"),
}
AFFORDABILITY_WEIGHTS = {
    "affordable_households_share": Decimal("0.15"),
    "affordable_households": Decimal("0.10"),
}
MOTHER_WEIGHTS = {
    "mothers_with_children_under_18": Decimal("0.04"),
    "mothers_with_children_under_6": Decimal("0.03"),
    "recent_births": Decimal("0.03"),
}
HOUSEHOLD_WEIGHTS = {
    "owner_occupied_share_age_range": Decimal("0.10"),
    "women_management_professional_share": Decimal("0.10"),
}
ARTS_WEIGHT = Decimal("0.05")

# Score column order. Each is written as score_<factor>.
FACTORS = (
    "age_share",
    "women_share_of_age_range",
    "affordable_households_share",
    "affordable_households",
    "mothers_with_children_under_18",
    "mothers_with_children_under_6",
    "recent_births",
    "owner_occupied_share_age_range",
    "women_management_professional_share",
    "women_arts_entertainment_media_share",
)
PER_ADULT_FACTORS = frozenset(MOTHER_WEIGHTS)

# Plain words for the header and the insight. {group} is the persona's adult
# phrase, such as "ages 25 to 44". {householder} is its householder phrase,
# such as "25–44".
FACTOR_LABELS = {
    "age_share": "share of adults {group}",
    "women_share_of_age_range": "women's share of adults {group}",
    "affordable_households_share": "share of households that can afford the fee",
    "affordable_households": "number of households that can afford the fee",
    "mothers_with_children_under_18": "mothers of children under 18 per adult",
    "mothers_with_children_under_6": "mothers of children under 6 per adult",
    "recent_births": "recent births per adult",
    "owner_occupied_share_age_range": "home ownership among households headed by someone {householder}",
    "women_management_professional_share": "share of employed women in management and professional jobs",
    "women_arts_entertainment_media_share": "share of employed women in arts, entertainment, and media jobs",
}
DRIVER_PHRASES = {
    "age_share": "a large share of adults {group}",
    "women_share_of_age_range": "a high share of women among adults {group}",
    "affordable_households_share": "a high share of households that can afford the fee",
    "affordable_households": "many households that can afford the fee",
    "mothers_with_children_under_18": "many mothers of children under 18",
    "mothers_with_children_under_6": "many mothers of young children",
    "recent_births": "many recent births",
    "owner_occupied_share_age_range": "high home ownership among households headed by someone {householder}",
    "women_management_professional_share": "many women in management and professional jobs",
    "women_arts_entertainment_media_share": "many women in arts, entertainment, and media jobs",
}

EXCLUDED_WORDS = (
    "race",
    "ethnic",
    "hispanic",
    "latino",
    "white",
    "black",
    "asian",
    "education",
    "lifestyle",
    "tapestry",
    "esri",
)
if any(word in factor for factor in FACTORS for word in EXCLUDED_WORDS):
    raise SystemExit("Race, ethnicity, education, and lifestyle segment are not score factors.")
if set(FACTOR_LABELS) != set(FACTORS) or set(DRIVER_PHRASES) != set(FACTORS):
    raise SystemExit("Every score factor needs a label and a driver phrase.")


def procedure_group(procedure: str) -> str:
    if procedure in BREAST_PROCEDURES:
        return "breast"
    if procedure in BODY_PROCEDURES:
        return "body"
    if procedure in FACE_PROCEDURES:
        return "face"
    raise SystemExit(f"{procedure!r} has no score weights.")


def raw_weights(procedure: str, priced: bool) -> dict[str, Decimal]:
    """The default weights that apply to one procedure, before scaling."""
    group = procedure_group(procedure)
    weights = {
        "age_share": AGE_SHARE_WEIGHT,
        "women_share_of_age_range": WOMEN_SHARE_WEIGHTS[group],
    }
    if priced:
        weights.update(AFFORDABILITY_WEIGHTS)
    if procedure in MOTHER_PROCEDURES:
        weights.update(MOTHER_WEIGHTS)
    weights.update(HOUSEHOLD_WEIGHTS)
    if group == "face":
        weights["women_arts_entertainment_media_share"] = ARTS_WEIGHT
    return {factor: weights[factor] for factor in FACTORS if factor in weights}


def weights(procedure: str, priced: bool) -> dict[str, Decimal]:
    """Weights for one procedure list, scaled to add up to 1, in FACTORS order."""
    found = raw_weights(procedure, priced)
    total = sum(found.values())
    return {factor: weight / total for factor, weight in found.items()}
