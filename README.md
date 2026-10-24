# real_estate_analysis
analyze california roi and risk

## Natural hazards

`pull_nri_census_tracts.py` keeps California rows and writes attributes only. Each run overwrites `nri_census_tracts_ca.csv` in the repository root. That CSV is not committed.

It queries layer 0 of the [National Risk Index Census Tracts](https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer) feature service. The filter is `STATEABBRV = 'CA'`, `returnGeometry` is false, and results are paged.

Tract fields are `TRACTFIPS`, `COUNTY`, `STATEABBRV`, and `STCOFIPS`. For inland flood (`IFLD`), coastal flood (`CFLD`), heat wave (`HWAV`), strong wind (`SWND`), landslide (`LNDS`), wildfire (`WFIR`), earthquake (`ERQK`), and tsunami (`TSUN`), the file keeps the service fields `*_EALT` (expected annual loss), `*_EALS` (score), and `*_EALR` (rating).
