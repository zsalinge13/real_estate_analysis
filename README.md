# real_estate_analysis
analyze california roi and risk

## California load

`load_california.py` reads the Zip row of Monthly Housing Inventory on [Realtor.com Research](https://www.realtor.com/research/data/) and downloads the Historical Data CSV (not the current-month file). It keeps a row when `zip_name` contains `, ca` (case-insensitive, so `, CA` matches) and upserts it into `zip_inventory_month`, one row per California ZIP per month. A rerun replaces a month when Realtor.com revises history. If an older wide table is already there, extra source columns are dropped. The script also drops the old Zillow tables `metric_observation` and `region` if they exist.

This is regional ZIP inventory, not individual homes. Single-family and condo/townhome listings are pooled. There is no building type, bedroom count, bathroom count, lot acreage, or single-home price. Price per square foot is the ZIP's median, not one building's area. Source: Realtor.com Economic Research.

Columns loaded from the file, in this order:

1. `month_date_yyyymm`
2. `postal_code`
3. `zip_name`
4. `median_listing_price`
5. `median_listing_price_mm`
6. `median_listing_price_yy`
7. `active_listing_count`
8. `median_listing_price_per_square_foot`
9. `median_listing_price_per_square_foot_mm`
10. `median_listing_price_per_square_foot_yy`

`pulled_at` is not in the CSV. It is the timestamp of the load that wrote the row. Blank measure cells are stored as null. The primary key is (`postal_code`, `month_date_yyyymm`).

Tableau connects later with the PostgreSQL connector. That is out of scope here.

### Run once

1. In Supabase, copy the session pooler connection string (host `*.pooler.supabase.com`, port **5432**). SSL is required.
2. Export it as `DATABASE_URL`. Do not commit that string.
3. `pip install -r requirements.txt`
4. `python load_california.py`

### Scheduled refresh

`.github/workflows/load-california.yml` runs at 16:00 UTC on January 1 and July 1 only (`0 16 1 1,7 *`), and when you start it by hand (`workflow_dispatch`). No other months are scheduled. Add a repository secret named `DATABASE_URL` with the same session-pooler URL.
