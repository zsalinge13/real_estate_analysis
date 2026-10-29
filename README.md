# real_estate_analysis
analyze california roi and risk

## California load

`load_california.py` reads the Zip row of Monthly Housing Inventory on [Realtor.com Research](https://www.realtor.com/research/data/) and downloads the Historical Data CSV (not the current-month file). A ZIP is kept when the Census [2020 ZCTA-to-county relationship file](https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt) says Los Angeles County, FIPS `06037`, has strictly more land area in that ZCTA than any other county. Water area breaks a land-area tie. A tie on both is left out. The place name is not used.

A scheduled run, and a run started by hand, replaces `public.redfin_real_estate_value`. The script deletes every existing row in that table, then inserts the current extract. A ZIP or month that is no longer in the file does not remain. Rows are not appended. No other table is dropped or altered.

A run started after Census ZIP population succeeds does not do that replace. It loads only the calendar year of the Census `acs_year`, inserts or updates those months, and does not delete months already stored. That run's `california_zip_inventory.csv` contains that year only. Older months remain in `public.redfin_real_estate_value`.

Each run overwrites `california_zip_inventory.csv` in the repository root. The filename stays `california_zip_inventory.csv`. A scheduled or manual run writes every loaded month. The chained run writes the Census year only. The extract from the 2026-10-23 pull is committed at that path.

This is regional ZIP inventory, not individual homes. Single-family and condo/townhome listings are pooled. There is no building type, bedroom count, bathroom count, lot acreage, or single-home price. Price per square foot is the ZIP's median, not one building's area. Source: Realtor.com Economic Research.

If `public.redfin_real_estate_value` already has an older column type, the script rewrites that table only. An older `month_date_yyyymm` stored as an integer such as `202609`, or as a date, is rewritten to text `2026_09` before the replace.

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
| `dataset_updated_at` | timestamptz | UTC time of the load that wrote the row. Every row in that load has the same value. |

`date_pulled_yyyymmdd` is not in the source CSV. Every row in a pull gets the same value: the calendar date of the run in America/Los_Angeles, with underscores. The January 1 and July 1 runs start at 16:00 UTC, which is morning in Los Angeles, so those pulls store that same calendar date (`2026_01_01` and `2026_07_01` for the 2026 runs). Blank measure cells are stored as null. The primary key is (`postal_code`, `month_date_yyyymm`).

`dataset_updated_at` is not in the source CSV. A full replace writes it on every inserted row. A census-year upsert writes it on the months it inserts or updates and leaves other months, including this timestamp, unchanged.

The four change columns stay on the file's ratio scale. They are not divided or multiplied by 100. Tableau's percentage format displays `0.0318` as 3.18 percent.

### Tableau

`tableau/california-zip-inventory.tds` is a live PostgreSQL data source for `public.redfin_real_estate_value`.

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

`python load_california.py` replaces the table and overwrites `california_zip_inventory.csv`. `python load_california.py --census-year YYYY` loads only that calendar year and does not delete other months. The chained workflow passes the Census `acs_year` there.

### Scheduled refresh

`.github/workflows/load-california.yml` runs at 16:00 UTC on January 1 and July 1 only (`0 16 1 1,7 *`), and when you start it by hand (`workflow_dispatch`). No other months are scheduled. Those runs replace the whole price table. An in-progress run is not cancelled.

A scheduled run and a manual run, after that full load succeeds, download the `census-zip-population` artifact from the latest successful Pull Census ZIP population run on `main` and run the census-year join and rank. Each of those runs uploads `redfin-real-estate-value`, `zip-census-redfin-join`, `cash-pay-zip-rank`, and `procedure-surgeon-fees`.

A successful Pull Census ZIP population run on `main` starts this workflow through `workflow_run`. That run loads one Redfin year and writes the join described under Census and Redfin join. It does not cancel an in-progress load.

Add a repository secret named `DATABASE_URL` with the same session-pooler URL. The workflow writes `california_zip_inventory.csv` in the job workspace. It does not commit a newer extract on its own. After that load succeeds, the job uploads `california_zip_inventory.csv` as the `redfin-real-estate-value` artifact and keeps it for 90 days. The scheduled and manual Redfin artifact is every loaded month. The chained Redfin artifact is the Census year only. The scheduled run and the manual run also upload the join, the rank, and the procedure fees file from that full inventory. If the load step fails, the job exports the current `public.redfin_real_estate_value` rows to that same CSV and uploads the same artifact. An empty or missing table fails the backup and does not invent rows. That failure export still includes older months that were kept in the table.

## Natural hazards

`pull_nri_census_tracts.py` keeps Los Angeles County rows and writes attributes only. Each run overwrites `nri_census_tracts_ca.csv` in the repository root. That CSV is not committed.

It queries layer 0 of the [National Risk Index Census Tracts](https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/National_Risk_Index_Census_Tracts/FeatureServer) feature service. The filter is `STATEABBRV = 'CA' AND STCOFIPS = '06037'` (Los Angeles County), `returnGeometry` is false, and results are paged. Every returned row has `COUNTY` equal to `Los Angeles`.

Columns are `TRACTFIPS`, `COUNTY`, `STATEABBRV`, `STCOFIPS`, then every service field whose name contains `_AFREQ`, in the feature service's field order. Expected annual loss fields (`*_EALT`, `*_EALS`, `*_EALR`) are not written. The annualized frequency columns are `AVLN_AFREQ`, `CFLD_AFREQ`, `CWAV_AFREQ`, `DRGT_AFREQ`, `ERQK_AFREQ`, `HAIL_AFREQ`, `HWAV_AFREQ`, `HRCN_AFREQ`, `ISTM_AFREQ`, `LNDS_AFREQ`, `LTNG_AFREQ`, `IFLD_AFREQ`, `SWND_AFREQ`, `TRND_AFREQ`, `TSUN_AFREQ`, `VLCN_AFREQ`, `WFIR_AFREQ`, and `WNTW_AFREQ`.

When `DATABASE_URL` is set, the script replaces every row in `public.nri_census_tracts_ca` and does not drop or alter any other table. Identifier columns are `text`. Annualized frequency columns are `numeric`. The connection string is that environment variable and is not stored in the repository. Each run also overwrites `nri_census_tracts_ca.csv` and does not commit it. When `DATABASE_URL` is unset, the script still writes that CSV and leaves the table unchanged.

## Flood zones

`pull_nfhl_flood_zones.py` keeps Los Angeles County flood-hazard polygons. Each run overwrites `nfhl_flood_zones_los_angeles.geojson` and `nfhl_flood_zones.csv` in the repository root and replaces every row in `public.nfhl_flood_zones`. The CSV has every loaded column, including `geometry` as GeoJSON text in WGS84. Those files are not committed.

It queries layer 28 of the [NFHL MapServer](https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28). That layer has no county-name field. The county id is `DFIRM_ID`, Los Angeles County FIPS is `06037`, and the filter is `DFIRM_ID LIKE '06037%'`. `returnGeometry` is true, coordinates are WGS84, and results are paged. Attributes kept are `DFIRM_ID`, `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF`, `STATIC_BFE`, and `DEPTH`. Each feature also has `COUNTY` set to `Los Angeles`.

The connection string is the `DATABASE_URL` environment variable, the same as the price load. The script creates `public.nfhl_flood_zones` when that table is missing. Its columns are `dfirm_id`, `fld_zone`, `zone_subty`, `sfha_tf`, `static_bfe`, `depth`, `county`, and `geometry`. `geometry` is the polygon as GeoJSON text. A run deletes and reinserts only that table. Install `psycopg` from `requirements.txt`.

## Census ZIP population

`pull_census_zip_population.py` loads American Community Survey 5-year estimates for the Los Angeles County ZIP codes in the home-value extract. The Census download requests California ZCTAs: codes that have area in a California county in the same ZCTA-to-county relationship file used by `load_california.py`. That request is not limited to the Los Angeles County list. After a batch is downloaded, the script puts it in a dataframe and keeps a ZIP only when it is in `california_zip_inventory.csv` and that relationship file says Los Angeles County, FIPS `06037`, is its majority county by land area. The stored geography is the ZIP Code Tabulation Area, in the `zip` column. The script does not read office, dermatology, or NPI locations.

Each run fetches only the latest published ACS 5-year vintage. Income, age, sex, and age bands are separate requests of about 10 ZIP codes, with a couple of seconds between calls. A failure retries that field group. A closed connection, a dropped connection, or a read timeout is retried several times, and each retry waits longer than the one before it. The read timeout is 45 minutes. Each finished batch is written to `census_zip_population.csv` before the next batch starts. When `DATABASE_URL` is set, that batch is inserted before the next request. A ZIP and year already stored are not inserted again, so rows saved before a timeout stay in the table and a later run fills ZIPs that are still missing. Older years are not deleted. When `DATABASE_URL` is unset, the script still writes the CSV and leaves the table unchanged. That CSV is the one vintage from the run. It is not committed. The estimates request is the step that needs a Census key; without one, no CSV is written. A rejected key stops the run. It is not treated as an unpublished year.

Columns are `acs_year`, `zip`, `median_household_income` (`B19013_001E`), `per_capita_income` (`B19301_001E`), `median_age` (`B01002_001E`), `male_population` and `female_population` from `B01001`, `adult_population` (age 18 and older, summed from the `B01001` age bands), and `population_18_34`, `population_35_44`, `population_45_54`, `population_55_64`, and `population_65_plus`. Census sentinel values are stored as null. The primary key is (`zip`, `acs_year`). Each newly inserted row also stores `dataset_updated_at`, the same timestamptz for every new row in that run. A ZIP already stored for that year is not rewritten, so its timestamp stays as it was.

ACS 5-year ZIP queries request zip code tabulation areas directly and do not send `in=state`. That geography is not nested in a state. Census answers `in=state:06` with an unsupported-hierarchy error, and that error is not a missing ZIP. California is applied by requesting the California ZCTAs and then keeping California and Los Angeles rows in the dataframe. When `CENSUS_API_KEY` is set, the script sends it on the first request and does not print it.

`.github/workflows/pull-census-zip-population.yml` runs once a day at 17:00 UTC (`0 17 * * *`). GitHub cron is UTC and does not follow daylight saving, so that is 10:00 AM Pacific Daylight Time and 9:00 AM Pacific Standard Time. It also runs on `workflow_dispatch`. After a successful load it uploads `census_zip_population.csv` as the `census-zip-population` artifact and keeps it for 90 days. If the load step fails, the job exports the current `public.census_zip_population` rows and uploads the same artifact. The job passes `DATABASE_URL`, `CENSUS_API_KEY`, and the refresh-review webhook secrets. A successful run on `main` starts the California workflow. This workflow does not cancel that run.

## Census and Redfin join

The chained California job reads `acs_year` from the `census-zip-population` artifact of the Pull Census ZIP population run that started it. Realtor.com still publishes one history file. This run keeps months in that census calendar year and does not stage the other years. Those months are inserted or updated in `public.redfin_real_estate_value`. The load does not delete rows, so a month already stored outside this run stays. A scheduled or manual California run reads that same artifact from the latest successful run of that workflow on `main`. It does not repeat this year-only load.

The join uses the latest `month_date_yyyymm` in that year. The Actions log prints that month. It is the maximum month in the file just pulled whose year equals the Census `acs_year`. It is not a fixed month in the code. Earlier months in that year are stored by the pull and are not copied into the join.

A ZIP is kept only when that month's `postal_code` equals the Census `zip`. The grain is one row per ZIP. Census values and prices are copied from those two rows. A blank price stays blank. A ZIP missing on either side is left out.

The file is `zip_census_redfin_join.csv`. Columns are the Census columns, then the Redfin columns, including `acs_year`, `zip`, `postal_code`, and `month_date_yyyymm`. The workflow uploads it as the artifact `zip-census-redfin-join` on every California run, kept for 90 days. Download it from that run's summary. When `DATABASE_URL` is set, the same rows replace `public.zip_census_redfin_join`. That replace writes the same `dataset_updated_at` on every row. Replacing the join table does not delete `public.redfin_real_estate_value`.

A scheduled or manual California run replaces the whole price table and then writes this join. The Redfin file for that join is the full inventory that load just wrote. Months outside the Census `acs_year` stay out of the join.

## Cash-pay ZIP rank

After that join succeeds, the same California job runs `rank_cash_pay_zips.py` on `zip_census_redfin_join.csv`. The script ranks ZIPs already in the join. It does not rebuild the join, and it does not delete `public.redfin_real_estate_value`. A scheduled or manual run ranks in the job that follows the full load.

A ZIP is skipped for a persona when that persona's age count, `adult_population`, `median_household_income`, `per_capita_income`, or `median_listing_price_per_square_foot` is blank, when `adult_population` is zero, or when `active_listing_count` is blank or under 10. Ages 55 and older use `population_55_64` plus `population_65_plus`, and a blank in either band skips the ZIP for that persona. The middles of household income, per-person income, and listing price per square foot are recalculated from the ZIPs that remain. They are not fixed dollar amounts.

Ages 18 to 34, 35 to 44, and 45 to 54 stay on the list when household income and listing price per square foot are both at or above those middles. Ages 55 and older stay when listing price per square foot and per-person income are both at or above those middles. The largest age share is rank 1. An equal share keeps the higher household income first for the working-age personas and the higher listing price per square foot first for ages 55 and older. Each persona keeps 25 ZIPs, or every ZIP that passed when fewer than 25 do. One ZIP can be on more than one persona. The female share is stored and shown. It does not change the rank. `median_age` and `median_listing_price` are stored and do not change the rank.

The file is `cash_pay_zip_rank.csv`. The workflow uploads it as the artifact `cash-pay-zip-rank` on every California run, kept for 90 days. When `DATABASE_URL` is set, those rows replace `public.cash_pay_zip_rank`. The replace writes one `dataset_updated_at` on the rank rows. Replacing the rank table does not drop it and does not modify the flood or risk tables.

The same run writes `marketing_bullets` on `public.procedure_surgeon_fees`. That text column is not part of the primary key. The preferred `report_year` is `acs_year` on `zip_census_redfin_join`. A year qualifies when `surgeon_fee_low` and `surgeon_fee_high` are both non-null on more than half of that year's procedure rows. When the join year qualifies, bullets are written only on that year. When it does not, bullets are written only on the most recent report year that does. Every other report year is left blank in that column. The rank does not fill fee columns. Every row of the chosen year gets bullet text for the personas that procedure uses: the ranked ZIP names and why they passed. Procedures that use the same personas get the same text. The January fee load does not write this text.

After that write succeeds, the same run reads `public.procedure_surgeon_fees` and writes `procedure_surgeon_fees.csv`. The file has every column on the table after the rank, including `marketing_bullets`, for every report year. Each stored bullet line becomes its own row, in that same order. `report_year` and `procedure` repeat on each of those rows. The text in `marketing_bullets` has no line break, so a file reader does not turn one bullet into another row. A header line stays on its own row. A report year with no bullets stays one row with that column blank. Every California load uploads that file as the artifact `procedure-surgeon-fees` and keeps it for 90 days. The January fee workflow is not part of this upload.

## National procedure counts and surgeon fees

`pull_procedure_surgeon_fees.py` loads national aesthetic procedure counts and surgeon fee ranges from the American Society of Plastic Surgeons procedural statistics reports. The table is national. It is not a Los Angeles fee. `fee_basis` says: national surgeon fee only, excludes anesthesia and facility, not Los Angeles. There are no office locations.

Each run tries the official PDF for the current year back through 2024:

`https://www.plasticsurgery.org/documents/news/statistics/{year}/plastic-surgery-statistics-report-{year}.pdf`

It upserts the years it can read and does not delete other years in `public.procedure_surgeon_fees`. Each row upserted in a run gets the same `dataset_updated_at`. A year this run does not write keeps the timestamp it already had. The primary key is (`procedure`, `report_year`). `marketing_bullets` is a text column on this table. This load adds it when the table is created or when the column is missing, and the upsert does not change it. A new report year is inserted with that column blank. The census-year rank writes `marketing_bullets` on the join's `acs_year` when that year has surgeon fees on more than half of its rows, and otherwise on the most recent report year that does. A checked-in seed is used only when that year's official PDF cannot be fetched. The 2024 URL currently returns 404, so `seeds/procedure_surgeon_fees_2024.csv` supplies the 2024 counts and fee ranges read from that report. When the URL works again, the PDF replaces the seed. The 2025 report publishes counts and does not publish surgeon fees, so 2025 fee columns stay null. 2024 fees are not copied onto 2025.

2024 publishes separate upper and lower blepharoplasty fee ranges. Those two ranges are not combined into one eyelid-surgery fee, so `eyelid surgery` keeps its published count and null fee columns.

The CSV `procedure_surgeon_fees.csv` is written even when `DATABASE_URL` is unset. It is not committed. That file from this load does not include `marketing_bullets`. `.github/workflows/pull-procedure-surgeon-fees.yml` runs at 16:00 UTC on January 1 and on `workflow_dispatch`. The job installs `poppler-utils` for `pdftotext`. After success it uploads `procedure_surgeon_fees.csv` as the `procedure-surgeon-fees` artifact for 90 days. If the load fails, it exports the current table to that same CSV and uploads the same artifact. The job passes `DATABASE_URL` and the refresh-review webhook secrets. The census-year rank writes a separate `procedure_surgeon_fees.csv` that does include `marketing_bullets`, and every California load uploads it.
