# real_estate_analysis
analyze california roi and risk

## Natural hazards

`pull_nri_census_tracts.py` keeps Los Angeles County rows and writes attributes only. Each run overwrites `nri_census_tracts_ca.csv` in the repository root. That CSV is not committed.

It queries layer 0 of the [National Risk Index Census Tracts](https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer) feature service. The filter is `STATEABBRV = 'CA' AND STCOFIPS = '06037'` (Los Angeles County), `returnGeometry` is false, and results are paged. Every returned row has `COUNTY` equal to `Los Angeles`.

Columns are `TRACTFIPS`, `COUNTY`, `STATEABBRV`, `STCOFIPS`, then every service field whose name contains `_AFREQ`, in the feature service's field order. Expected annual loss fields (`*_EALT`, `*_EALS`, `*_EALR`) are not written. The annualized frequency columns are `AVLN_AFREQ`, `CFLD_AFREQ`, `CWAV_AFREQ`, `DRGT_AFREQ`, `ERQK_AFREQ`, `HAIL_AFREQ`, `HWAV_AFREQ`, `HRCN_AFREQ`, `ISTM_AFREQ`, `LNDS_AFREQ`, `LTNG_AFREQ`, `IFLD_AFREQ`, `SWND_AFREQ`, `TRND_AFREQ`, `TSUN_AFREQ`, `VLCN_AFREQ`, `WFIR_AFREQ`, and `WNTW_AFREQ`.

`.github/workflows/pull-nri-census-tracts.yml` runs that script at 16:00 UTC on January 1 and July 1 only (`0 16 1 1,7 *`), and when you start it by hand (`workflow_dispatch`). No other months are scheduled. The script uses the Python standard library, so the workflow does not install packages or read a database secret. Each run overwrites `nri_census_tracts_ca.csv` and does not commit it.
