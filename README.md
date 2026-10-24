# real_estate_analysis
analyze california roi and risk

## California load

`load_california.py` reads the Zip row of Monthly Housing Inventory on [Realtor.com Research](https://www.realtor.com/research/data/) and downloads the Historical Data CSV (not the current-month file). It keeps rows whose `zip_name` ends in `, ca` and upserts them into `zip_inventory_month`, one row per California ZIP per month. A rerun replaces a month when Realtor.com revises history. The script drops the old Zillow tables `metric_observation` and `region` if they exist.

This is regional ZIP inventory, not individual homes. Single-family and condo/townhome listings are pooled. There is no building type, bedroom count, bathroom count, lot acreage, or single-home price. `median_square_feet` is the ZIP's median listing size, not one building's area. Flood and fire risk are not in this file. Source: Realtor.com Economic Research.

Columns loaded from the file, plus `pulled_at` (when this run wrote the row):

- `month_date_yyyymm`, `postal_code`, `zip_name`
- `median_listing_price`, `median_listing_price_mm`, `median_listing_price_yy`
- `active_listing_count`, `active_listing_count_mm`, `active_listing_count_yy`
- `median_days_on_market`, `median_days_on_market_mm`, `median_days_on_market_yy`
- `new_listing_count`, `new_listing_count_mm`, `new_listing_count_yy`
- `price_increased_count`, `price_increased_count_mm`, `price_increased_count_yy`
- `price_increased_share`, `price_increased_share_mm`, `price_increased_share_yy`
- `price_reduced_count`, `price_reduced_count_mm`, `price_reduced_count_yy`
- `price_reduced_share`, `price_reduced_share_mm`, `price_reduced_share_yy`
- `pending_listing_count`, `pending_listing_count_mm`, `pending_listing_count_yy`
- `median_listing_price_per_square_foot`, `median_listing_price_per_square_foot_mm`, `median_listing_price_per_square_foot_yy`
- `median_square_feet`, `median_square_feet_mm`, `median_square_feet_yy`
- `average_listing_price`, `average_listing_price_mm`, `average_listing_price_yy`
- `total_listing_count`, `total_listing_count_mm`, `total_listing_count_yy`
- `pending_ratio`, `pending_ratio_mm`, `pending_ratio_yy`
- `quality_flag`

Blank measure cells are stored as null. The primary key is (`postal_code`, `month_date_yyyymm`).

Tableau connects later with the PostgreSQL connector. That is out of scope here.

### Run once

1. In Supabase, copy the session pooler connection string (host `*.pooler.supabase.com`, port **5432**). SSL is required.
2. Export it as `DATABASE_URL`. Do not commit that string.
3. `pip install -r requirements.txt`
4. `python load_california.py`

### Scheduled refresh

`.github/workflows/load-california.yml` runs at 16:00 UTC on January 1 and July 1, and when you start it by hand (`workflow_dispatch`). Add a repository secret named `DATABASE_URL` with the same session-pooler URL.
