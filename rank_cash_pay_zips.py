"""Rank Los Angeles County ZIPs from the Census-Redfin join.

The input is the join that just succeeded. This script does not rebuild
that join and does not delete Redfin months. Each persona keeps the top 25
ZIPs that pass its cash screen. The same run writes marketing_bullets on
one procedure_surgeon_fees report year. The preferred year is acs_year on
zip_census_redfin_join. A year qualifies when surgeon_fee_low and
surgeon_fee_high are both non-null on more than half of that year's
procedure rows. When the join year qualifies, bullets are written only
there. Otherwise they are written only on the most recent report year that
qualifies. Every other report year is left blank in that column. Fees are
not copied from another year. The fee load owns the column and does not
fill it. The same write fills rank (integer[]) and zip_code (text[]) with one
element per bullet line. A ZIP line holds its rank and 5-digit ZIP, and the
line text has neither. A header line holds null in both.
After that write succeeds, the same run exports every column of
procedure_surgeon_fees, including marketing_bullets, rank, and zip_code, for
the chosen report year and every other report year. The file has one row per
bullet line. report_year and procedure repeat on each of those rows, rank and
zip_code are that line's values or blank, and the bullet text has no line
break inside the cell.
"""

from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import dataset_stamp
import load_california
import pull_procedure_surgeon_fees as procedures

RANK_TABLE_NAME = "cash_pay_zip_rank"
RANK_TABLE = "public.cash_pay_zip_rank"
DELETE_RANK_SQL = f"DELETE FROM {RANK_TABLE}"
OUTPUT_CSV_NAME = "cash_pay_zip_rank.csv"
OUTPUT_CSV = Path(__file__).resolve().parent / OUTPUT_CSV_NAME
MIN_ACTIVE_LISTINGS = 10
TOP_N = 25
DATABASE_URL_ENV = load_california.DATABASE_URL_ENV
BULLET_RANK = "rank"
BULLET_ZIP_CODE = "zip_code"
LEGACY_BULLET_RANK = "bullet_rank"

# Equal age shares are the close case. The higher cash signal ranks first.
# Household income is that signal for working-age personas. Listing price
# per square foot is that signal for ages 55 and older. The female share
# is not a rank key.


@dataclass(frozen=True)
class Persona:
    label: str
    age_columns: tuple[str, ...]
    cash: str
    age_phrase: str


PERSONAS = (
    Persona("18 to 34", ("population_18_34",), "working", "ages 18 to 34"),
    Persona("35 to 44", ("population_35_44",), "working", "ages 35 to 44"),
    Persona("45 to 54", ("population_45_54",), "working", "ages 45 to 54"),
    Persona(
        "55 and older",
        ("population_55_64", "population_65_plus"),
        "older",
        "ages 55 and older",
    ),
)
PERSONA_BY_LABEL = {persona.label: persona for persona in PERSONAS}

# The ZIP rank does not change by procedure. A procedure uses these persona
# lists, and procedures that use the same lists get the same bullet text.
PROCEDURE_PERSONAS = {
    "breast augmentation": ("18 to 34", "35 to 44"),
    "breast lift": ("35 to 44", "45 to 54"),
    "breast reduction": ("18 to 34", "35 to 44", "45 to 54"),
    "liposuction": ("18 to 34", "35 to 44", "45 to 54"),
    "tummy tuck": ("35 to 44", "45 to 54"),
    "arm lift": ("35 to 44", "45 to 54", "55 and older"),
    "thigh lift": ("35 to 44", "45 to 54", "55 and older"),
    "buttock augmentation with fat grafting": ("18 to 34", "35 to 44"),
    "eyelid surgery": ("45 to 54", "55 and older"),
    "facelift": ("45 to 54", "55 and older"),
    "neck lift": ("45 to 54", "55 and older"),
    "rhinoplasty": ("18 to 34", "35 to 44"),
    "forehead lift": ("45 to 54", "55 and older"),
}

RANK_COLUMNS = (
    "persona",
    "rank",
    "zip",
    "zip_name",
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
)
COLUMN_TYPES = {
    "persona": "text",
    "rank": "integer",
    "zip": "text",
    "zip_name": "text",
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
}
NOT_NULL_COLUMNS = {
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
    "population_18_34",
    "population_35_44",
    "population_45_54",
    "population_55_64",
    "population_65_plus",
    "median_listing_price",
    "median_listing_price_per_square_foot",
    "active_listing_count",
    "acs_year",
    "month_date_yyyymm",
    "date_pulled_yyyymmdd",
)
INTEGER_RANK_COLUMNS = {
    name for name, sql_type in COLUMN_TYPES.items() if sql_type == "integer"
}

if set(PROCEDURE_PERSONAS) != set(procedures.PROCEDURES):
    raise SystemExit("Every procedure needs a persona list.")
if any(label not in PERSONA_BY_LABEL for labels in PROCEDURE_PERSONAS.values() for label in labels):
    raise SystemExit("A procedure lists an unknown persona.")
if "DELETE" in DELETE_RANK_SQL.upper() and "redfin_real_estate_value" in DELETE_RANK_SQL:
    raise SystemExit("The rank refresh must not delete Redfin rows.")


@dataclass
class PersonaRank:
    persona: Persona
    household_income_middle: Decimal | None
    per_capita_income_middle: Decimal | None
    listing_price_middle: Decimal | None
    rows: list[dict]


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
    integral = value.to_integral_value()
    if value == integral:
        return f"${int(integral):,}"
    return f"${value:,.2f}"


def format_percent(share: Decimal) -> str:
    points = (share * Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{points}%"


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
    return {
        "zip": zip_code,
        "zip_name": str(row.get("zip_name") or "").strip(),
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
    }


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
    )


def rank_personas(rows: list[dict[str, str]]) -> list[PersonaRank]:
    """Rank each persona from the join rows. Do not add or drop a ZIP by name."""
    zips = [str(row.get("zip") or "").strip() for row in rows]
    if any(not zip_code for zip_code in zips):
        raise SystemExit("The join has a row without a ZIP.")
    if len(zips) != len(set(zips)):
        raise SystemExit("The join has more than one row for a ZIP.")
    return [rank_persona(rows, persona) for persona in PERSONAS]


@dataclass(frozen=True)
class BulletLine:
    """One marketing bullet line. Header lines have no rank and no ZIP."""

    text: str
    rank: int | None = None
    zip_code: str | None = None


def place_name(row: dict) -> str:
    return str(row.get("zip_name") or "").strip()


def screen_header(screen: PersonaRank) -> str:
    persona = screen.persona
    if screen.household_income_middle is None:
        return (
            f"{persona.label}. No ZIP had a published age count, adult population, "
            "household income, per-person income, listing price per square foot, and at least "
            f"{MIN_ACTIVE_LISTINGS} active listings."
        )
    if persona.cash == "working":
        middles = (
            f"household income middle {format_money(screen.household_income_middle)}, "
            f"listing price per square foot middle {format_money(screen.listing_price_middle)}"
        )
        rule = "Household income and the listing price per square foot are both at or above"
        failed = (
            "No ZIP has both household income and listing price per square foot at or above "
            "the middle of Los Angeles County ZIPs in this screen"
        )
    else:
        middles = (
            f"listing price per square foot middle {format_money(screen.listing_price_middle)}, "
            f"per-person income middle {format_money(screen.per_capita_income_middle)}"
        )
        rule = "Listing price per square foot and per-person income are both at or above"
        failed = (
            "No ZIP has both listing price per square foot and per-person income at or above "
            "the middle of Los Angeles County ZIPs in this screen"
        )
    if not screen.rows:
        return f"{persona.label}. {failed} ({middles})."
    return (
        f"{persona.label}. {rule} the middle of Los Angeles County ZIPs in this "
        f"screen ({middles}). Ranked by the share of adults {persona.age_phrase}."
    )


def zip_bullet(screen: PersonaRank, row: dict) -> str:
    share = format_percent(row["age_share"])
    if screen.persona.cash == "working":
        reason = (
            f"{share} of adults are {screen.persona.age_phrase}. "
            f"Household income {format_money(Decimal(row['median_household_income']))} "
            f"and listing price per square foot {format_money(row['median_listing_price_per_square_foot'])} "
            "are at or above those middles."
        )
    else:
        reason = (
            f"{share} of adults are {screen.persona.age_phrase}. "
            f"Listing price per square foot {format_money(row['median_listing_price_per_square_foot'])} and "
            f"per-person income {format_money(Decimal(row['per_capita_income']))} "
            "are at or above those middles."
        )
    if row.get("female_share") is not None:
        reason += f" Women are {format_percent(row['female_share'])} of residents."
    name = place_name(row)
    if not name:
        return reason
    return f"{name}: {reason}"


def render_screen(screen: PersonaRank) -> list[BulletLine]:
    lines = [BulletLine(screen_header(screen))]
    for row in screen.rows:
        lines.append(
            BulletLine(zip_bullet(screen, row), int(row["rank"]), str(row["zip"]))
        )
    return lines


def bullet_lines(procedure: str, screens: list[PersonaRank]) -> list[BulletLine]:
    """Bullet lines for one procedure's persona lists. Shared lists repeat."""
    labels = PROCEDURE_PERSONAS.get(procedure)
    if not labels:
        return [BulletLine(f"{procedure} has no cash-pay persona list.")]
    by_label = {screen.persona.label: screen for screen in screens}
    lines: list[BulletLine] = []
    for label in labels:
        lines.extend(render_screen(by_label[label]))
    return lines


def marketing_bullets(procedure: str, screens: list[PersonaRank]) -> str:
    return "\n".join(line.text for line in bullet_lines(procedure, screens))


def bullets_by_procedure(
    procedures_in_year: list[str], screens: list[PersonaRank]
) -> dict[str, list[BulletLine]]:
    cache: dict[tuple[str, ...], list[BulletLine]] = {}
    bullets: dict[str, list[BulletLine]] = {}
    for name in procedures_in_year:
        labels = PROCEDURE_PERSONAS.get(name)
        if labels is None:
            bullets[name] = bullet_lines(name, screens)
            continue
        if labels not in cache:
            cache[labels] = bullet_lines(name, screens)
        bullets[name] = cache[labels]
    return bullets


def flatten(screens: list[PersonaRank]) -> list[dict]:
    rows: list[dict] = []
    for screen in screens:
        rows.extend(screen.rows)
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
        "    PRIMARY KEY (persona, zip)\n"
        ");\n"
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
    """Add rank and zip_code beside marketing_bullets. An old bullet_rank becomes rank.

    Each holds one element per marketing_bullets line, in line order. Header
    lines hold null.
    """
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
    cur.execute(
        f"ALTER TABLE {procedures.TABLE} ADD COLUMN IF NOT EXISTS "
        f"{quote_ident(BULLET_RANK)} integer[]"
    )
    cur.execute(
        f"ALTER TABLE {procedures.TABLE} ADD COLUMN IF NOT EXISTS "
        f"{quote_ident(BULLET_ZIP_CODE)} text[]"
    )


def apply_marketing_bullets(
    cur,
    screens: list[PersonaRank],
    procedure_rows: list[tuple[int, str, object, object]],
    preferred_year: int,
) -> tuple[int, int]:
    """Write bullet text on one qualifying report year and blank every other year.

    rank and zip_code are written with the text, one element per line.
    Surgeon fee columns are not updated. A blank fee stays blank.
    """
    ensure_bullet_columns(cur)
    chosen = select_bullet_report_year(preferred_year, procedure_rows)
    cur.execute(
        f"UPDATE {procedures.TABLE} SET {procedures.MARKETING_BULLETS} = NULL, "
        f"{BULLET_RANK} = NULL, {BULLET_ZIP_CODE} = NULL "
        "WHERE report_year <> %s",
        (chosen,),
    )
    names = [name for year, name, _low, _high in procedure_rows if year == chosen]
    bullets = bullets_by_procedure(names, screens)
    updates = [
        (
            "\n".join(line.text for line in bullets[name]),
            [line.rank for line in bullets[name]],
            [line.zip_code for line in bullets[name]],
            chosen,
            name,
        )
        for name in names
    ]
    if updates:
        cur.executemany(
            f"UPDATE {procedures.TABLE} SET {procedures.MARKETING_BULLETS} = %s, "
            f"{BULLET_RANK} = %s, {BULLET_ZIP_CODE} = %s "
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
) -> tuple[int, int, int]:
    """Replace the rank snapshot and write procedure bullets. Do not drop tables."""
    if "redfin_real_estate_value" in DELETE_RANK_SQL:
        raise SystemExit("The rank refresh must not delete Redfin rows.")
    updated_at = dataset_stamp.stamp(updated_at)
    rows = flatten(screens)
    stored = RANK_COLUMNS + (dataset_stamp.COLUMN,)
    column_list = ", ".join(quote_ident(name) for name in stored)
    placeholders = ", ".join(["%s"] * len(stored))
    insert_sql = f"INSERT INTO {RANK_TABLE} ({column_list}) VALUES ({placeholders})"
    _updated_at, stamped = dataset_stamp.stamp_rows(
        [tuple(rank_db_value(name, row) for name in RANK_COLUMNS) for row in rows],
        updated_at,
    )
    with conn.transaction():
        with conn.cursor() as cur:
            ensure_rank_columns(cur)
            cur.execute(DELETE_RANK_SQL)
            if stamped:
                cur.executemany(insert_sql, stamped)
            procedure_rows = fetch_procedure_rows(cur)
            year, bullet_count = apply_marketing_bullets(
                cur, screens, procedure_rows, preferred_year
            )
            cur.execute(f"SELECT COUNT(*) FROM {RANK_TABLE}")
            count = cur.fetchone()[0]
    if count != len(rows):
        raise SystemExit(f"{RANK_TABLE} has {count} rows after loading {len(rows)}.")
    return count, year, bullet_count


def procedure_csv_cell(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return load_california.csv_value(value)


def marketing_bullet_cells(value: object) -> list[str]:
    """One cell per stored line. A cell does not contain a line break.

    Blank text stays one empty cell. Header lines stay, in order, and each
    ZIP bullet stays on its own cell in that same rank order.
    """
    if value is None:
        return [""]
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if text.strip() == "":
        return [""]
    cells = [line.strip() for line in text.split("\n") if line.strip()]
    if not cells:
        return [""]
    if any("\n" in cell or "\r" in cell for cell in cells):
        raise SystemExit("A marketing bullet still contains a line break.")
    return cells


def per_line_values(value: object, count: int, column: str) -> list[object]:
    """Split a stored rank or zip_code list so element i goes with bullet line i."""
    if value is None:
        return [None] * count
    if not isinstance(value, (list, tuple)):
        raise SystemExit(f"{procedures.TABLE}.{column} is not a list.")
    if len(value) != count:
        raise SystemExit(
            f"{procedures.TABLE}.{column} has {len(value)} values for {count} bullet lines."
        )
    return list(value)


def expand_marketing_bullet_rows(columns: list[str], rows: list[tuple]) -> list[tuple]:
    """Repeat report_year and procedure once per bullet line, with its rank and ZIP."""
    if procedures.MARKETING_BULLETS not in columns:
        raise SystemExit(
            f"{procedures.TABLE} is missing {procedures.MARKETING_BULLETS}."
        )
    index = columns.index(procedures.MARKETING_BULLETS)
    paired = [
        columns.index(name)
        for name in (BULLET_RANK, BULLET_ZIP_CODE)
        if name in columns
    ]
    expanded: list[tuple] = []
    for row in rows:
        if len(row) != len(columns):
            raise SystemExit(
                f"{procedures.OUTPUT_CSV_NAME} row does not match its columns."
            )
        cells = marketing_bullet_cells(row[index])
        values = {
            column: per_line_values(row[column], len(cells), columns[column])
            for column in paired
        }
        for position, cell in enumerate(cells):
            copied = list(row)
            copied[index] = cell
            for column in paired:
                copied[column] = values[column][position]
            expanded.append(tuple(copied))
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
            writer.writerow(columns)
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
    rows = flatten(screens)
    write_rank_csv(rows)
    summary = ", ".join(f"{len(screen.rows)} for {screen.persona.label}" for screen in screens)
    print(f"Ranked cash-pay ZIPs: {summary}. Wrote {len(rows)} rows to {OUTPUT_CSV_NAME}.")
    if not database_url():
        print(
            f"{DATABASE_URL_ENV} is not set. "
            f"Left {RANK_TABLE} and {procedures.TABLE} unchanged."
        )
        return 0
    updated_at = dataset_stamp.stamp()
    print(f"dataset_updated_at: {updated_at.isoformat()}")
    try:
        with load_california.connect() as conn:
            count, year, bullet_count = save_rank(
                conn, screens, updated_at, preferred_year=preferred_year
            )
            fee_count = export_procedure_fees(conn)
    except SystemExit as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(public_error(exc), file=sys.stderr)
        return 1
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
