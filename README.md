# real_estate_analysis
analyze california roi and risk

## Flood zones

`pull_nfhl_flood_zones.py` keeps Los Angeles County flood-hazard polygons and writes geometry. Each run overwrites `nfhl_flood_zones_los_angeles.geojson` in the repository root. That GeoJSON is not committed.

It queries layer 28 of the [NFHL MapServer](https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28). That layer has no county-name field. The county id is `DFIRM_ID`, Los Angeles County FIPS is `06037`, and the filter is `DFIRM_ID LIKE '06037%'`. `returnGeometry` is true, coordinates are WGS84, and results are paged. Attributes kept are `DFIRM_ID`, `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF`, `STATIC_BFE`, and `DEPTH`. Each feature also has `COUNTY` set to `Los Angeles`.

The script uses the Python standard library. It does not load a database.
