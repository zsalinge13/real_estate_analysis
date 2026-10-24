# real_estate_analysis
analyze california roi and risk

## Natural hazards

These scripts keep California rows and write attributes only. Each run overwrites the same filename in the repository root. The CSV files are not committed.

### National Risk Index census tracts

`pull_nri_census_tracts.py` queries layer 0 of the [National Risk Index Census Tracts](https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer) feature service. The filter is `STATEABBRV = 'CA'`, `returnGeometry` is false, and results are paged. Output: `nri_census_tracts_ca.csv`.

Tract fields are `TRACTFIPS`, `COUNTY`, `STATEABBRV`, and `STCOFIPS`. For inland flood (`IFLD`), coastal flood (`CFLD`), heat wave (`HWAV`), strong wind (`SWND`), landslide (`LNDS`), wildfire (`WFIR`), earthquake (`ERQK`), and tsunami (`TSUN`), the file keeps the service fields `*_EALT` (expected annual loss), `*_EALS` (score), and `*_EALR` (rating).

### Extreme heat

`pull_heat_census_tracts.py` reads the county list on the [California Heat Assessment Tool download page](https://www.cal-heat.org/download) and downloads each county's historical census-tract CSV. Output: `heat_census_tracts_ca.csv`.

Location columns are `geoid_long`, `census_county`, `census_city`, and `census_tract`. The historical files also include `time_of_year` and `socioeconomic_group`, which identify the heat-measure row, plus the heat measures in the file: `avg_event_rh_max_perc`, `avg_event_rh_min_perc`, `tmax`, `tmin`, `hist_avg_annual_events`, and `hist_avg_duration`.

### Earthquakes

`pull_earthquake_tracts.py` queries layer 1 of the [FEMA P-366 tract feature service](https://services.arcgis.com/XG15cJAlne2vxtgt/ArcGIS/rest/services/FEMA_P366_3_14_23_gdb/FeatureServer/1). The filter is `ST_ABR = 'CA'`, `returnGeometry` is false, and results are paged. Output: `earthquake_tracts_ca.csv`.

Columns are `Tract`, `CountyName`, `ST_ABR`, `StateFips`, `CountyFips`, and `AEL_1mil`.
