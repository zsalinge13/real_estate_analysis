# real_estate_analysis
analyze california roi and risk

## California load

`load_california.py` reads the Zip row of Monthly Housing Inventory on [Realtor.com Research](https://www.realtor.com/research/data/) and downloads the Historical Data CSV (not the current-month file). It keeps a row when `zip_name` contains `, ca` (case-insensitive, so `, CA` matches).

Each run replaces `zip_inventory_month`. The script deletes every existing row, then inserts the current California extract. A ZIP or month that is no longer in the file does not remain. Rows are not appended to the previous pull.

The same extract is written to `california_zip_inventory.csv` in the repository root. Every run overwrites that file. The filename stays `california_zip_inventory.csv`. It is listed in `.gitignore` and is not committed.

This is regional ZIP inventory, not individual homes. Single-family and condo/townhome listings are pooled. There is no building type, bedroom count, bathroom count, lot acreage, or single-home price. Price per square foot is the ZIP's median, not one building's area. Source: Realtor.com Economic Research.

If an older table is already there, the script drops extra columns, including `pulled_at`. It also drops the old Zillow tables `metric_observation` and `region` if they exist. An older `month_date_yyyymm` stored as an integer such as `202609`, or as a date, is rewritten to text `2026_09` before the replace.

Columns, in this order:

| Column | Postgres type | Stored value |
| --- | --- | --- |
| `month_date_yyyymm` | text | `yyyy_mm`, month always two digits. Source `202609` becomes `2026_09`. |
| `postal_code` | text | ZIP code from the file |
| `zip_name` | text | Place name from the file |
| `median_listing_price` | numeric | Plain number from the file. No currency symbol. |
| `median_listing_price_mm` | numeric | Ratio from the file. `0.0318` is a 3.18 percent change. |
| `median_listing_price_yy` | numeric | Ratio from the file, same scale as the month-over-month change. |
| `active_listing_count` | integer | Whole listing count. A fractional source value is rounded. |
| `median_listing_price_per_square_foot` | numeric | Plain number from the file. No currency symbol. |
| `median_listing_price_per_square_foot_mm` | numeric | Ratio from the file. |
| `median_listing_price_per_square_foot_yy` | numeric | Ratio from the file. |
| `date_pulled_yyyymmdd` | text | `yyyy_mm_dd`. Example `2026_10_23`. |

`date_pulled_yyyymmdd` is not in the source CSV. Every row in a pull gets the same value: the calendar date of the run in America/Los_Angeles, with underscores. The January 1 and July 1 runs start at 16:00 UTC, which is morning in Los Angeles, so those pulls store that same calendar date (`2026_01_01` and `2026_07_01` for the 2026 runs). Blank measure cells are stored as null. The primary key is (`postal_code`, `month_date_yyyymm`).

The four change columns stay on the file's ratio scale. They are not divided or multiplied by 100. Tableau's percentage format displays `0.0318` as 3.18 percent.

### Tableau

`tableau/california-zip-inventory.tds` is a live PostgreSQL data source for `zip_inventory_month`.

1. Open the `.tds` file in Tableau Desktop.
2. Set the server to the Supabase session pooler host (`*.pooler.supabase.com`), database `postgres`, and user `postgres.` plus the project ref.
3. Keep port `5432` and SSL required. Tableau asks for the password. Do not put the password in the file or in git.

In that data source:

- `month_date_yyyymm` is a text dimension in `yyyy_mm` form, such as `2026_09`. It has no date role.
- `date_pulled_yyyymmdd` is a text dimension in `yyyy_mm_dd` form, such as `2026_10_23`. It has no date role.
- `active_listing_count` is an integer.
- `median_listing_price` and `median_listing_price_per_square_foot` are plain numbers with no currency format.
- The four change fields use percentage format `p0.00%`.

### Run once

1. In Supabase, copy the session pooler connection string (host `*.pooler.supabase.com`, port **5432**). SSL is required.
2. Export it as `DATABASE_URL`. Do not commit that string.
3. `pip install -r requirements.txt`
4. `python load_california.py`

The command replaces the table and overwrites `california_zip_inventory.csv`.

### Scheduled refresh

`.github/workflows/load-california.yml` runs at 16:00 UTC on January 1 and July 1 only (`0 16 1 1,7 *`), and when you start it by hand (`workflow_dispatch`). No other months are scheduled. Add a repository secret named `DATABASE_URL` with the same session-pooler URL. The workflow writes `california_zip_inventory.csv` in the job workspace and does not commit it.
