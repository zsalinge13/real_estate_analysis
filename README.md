# real_estate_analysis
analyze california roi and risk

## California load

`load_california.py` reads the CSV download links on [Zillow Research](https://www.zillow.com/research/data/) and loads California rows into Supabase. If that page blocks the request, the script uses `research_hrefs.txt` (those same hrefs, saved from the page) and still skips a URL that returns 404. Building type is the file's home-type cut (all homes, single-family, or condo). A bedroom file keeps that bedroom slice; every other file is `all`. Values are numbers, blanks are dropped, and a rerun upserts so a revised month replaces the old value. Month is the month-end date Zillow uses as the column header.

This dataset has no building square footage, bathrooms, lot acreage, or single-home price. It is a regional series. Source: Zillow Research public CSVs.

Tableau connects later with the PostgreSQL connector. That is out of scope here.

### Run once

1. In Supabase, copy the session pooler connection string (host `*.pooler.supabase.com`, port **5432**). SSL is required.
2. Export it as `DATABASE_URL`. Do not commit that string.
3. `pip install -r requirements.txt`
4. `python load_california.py`

The script creates `region` (`region_id`, `region_name`, `region_type`, `state_code`) and `metric_observation` (`region_id`, `metric`, `building_type`, `bedrooms`, `month`, `value`, `pulled_at`) if they are missing. The observation primary key is (`region_id`, `metric`, `building_type`, `bedrooms`, `month`). Metro rows use Zillow's region type `msa`.

### Scheduled refresh

`.github/workflows/load-california.yml` runs at 16:00 UTC on January 1 and July 1, and when you start it by hand (`workflow_dispatch`). Add a repository secret named `DATABASE_URL` with the same session-pooler URL.
