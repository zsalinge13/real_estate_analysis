# real_estate_analysis
analyze california roi and risk

## Flood zones

`pull_nfhl_flood_zones.py` keeps Los Angeles County flood-hazard polygons. Each run overwrites `nfhl_flood_zones_los_angeles.geojson` in the repository root and replaces every row in `public.nfhl_flood_zones`. That GeoJSON file is not committed.

It queries layer 28 of the [NFHL MapServer](https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28). That layer has no county-name field. The county id is `DFIRM_ID`, Los Angeles County FIPS is `06037`, and the filter is `DFIRM_ID LIKE '06037%'`. `returnGeometry` is true, coordinates are WGS84, and results are paged. Attributes kept are `DFIRM_ID`, `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF`, `STATIC_BFE`, and `DEPTH`. Each feature also has `COUNTY` set to `Los Angeles`.

The connection string is the `DATABASE_URL` environment variable, the same as the price load. The script creates `public.nfhl_flood_zones` when that table is missing. Its columns are `dfirm_id`, `fld_zone`, `zone_subty`, `sfha_tf`, `static_bfe`, `depth`, `county`, and `geometry`. `geometry` is the polygon as GeoJSON text. A run deletes and reinserts only that table. Install `psycopg` from `requirements.txt`. There is no schedule.
