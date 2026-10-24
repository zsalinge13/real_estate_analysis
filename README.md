# real_estate_analysis
analyze california roi and risk

## Natural hazards

`pull_nri_census_tracts.py` keeps California rows and writes attributes only. Each run overwrites `nri_census_tracts_ca.csv` in the repository root. That CSV is not committed.

It queries layer 0 of the [National Risk Index Census Tracts](https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer) feature service. The filter is `STATEABBRV = 'CA'`, `returnGeometry` is false, and results are paged.

Tract fields are `TRACTFIPS`, `COUNTY`, `STATEABBRV`, and `STCOFIPS`. For inland flood (`IFLD`), coastal flood (`CFLD`), heat wave (`HWAV`), strong wind (`SWND`), landslide (`LNDS`), wildfire (`WFIR`), earthquake (`ERQK`), and tsunami (`TSUN`), the file keeps the service fields `*_EALT` (expected annual loss), `*_EALS` (score), `*_EALR` (rating), and `*_AFREQ` (annualized frequency), in that order.

`.github/workflows/pull-nri-census-tracts.yml` runs that script at 16:00 UTC on January 1 and July 1 only (`0 16 1 1,7 *`), and when you start it by hand (`workflow_dispatch`). No other months are scheduled. The script uses the Python standard library, so the workflow does not install packages or read a database secret. Each run overwrites `nri_census_tracts_ca.csv` and does not commit it.
