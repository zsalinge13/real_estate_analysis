# real_estate_analysis
analyze california roi and risk

## Practice area

`practice.py` holds the one setting for where the practice is: `PRACTICE_ZIP_CODE = "90210"`. It also holds the 50-mile cutoff and the two counties, Los Angeles (FIPS `06037`) and Orange (FIPS `06059`).

A ZIP is in the practice area when both of these are true:
- The Census [2020 ZCTA-to-county relationship file](https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt) gives Los Angeles or Orange County strictly more land area in that ZCTA than any other county. Water area breaks a land-area tie, and a tie on both is left out. The place name is not used. This is the rule the Los Angeles load always used.
- `distance_from_practice_miles` is 50 or less. It is the haversine great-circle distance between the [Census 2024 Gazetteer](https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip) ZCTA internal points (`INTPTLAT`, `INTPTLONG`) of that ZIP and the practice ZIP. It uses a 3,958.8-mile Earth radius and is rounded to two decimals, and the cutoff applies to the rounded value.

On the 2024 Gazetteer, 294 of the 295 majority-Los-Angeles ZCTAs and 70 of the 88 majority-Orange ZCTAs are within 50 miles of 90210. The farthest kept is Irvine 92618 at 49.21 miles. Lancaster 93535 (51.00 miles) and 18 south Orange County ZCTAs, from 51.79 to 63.49 miles, are left out.

`public.redfin_real_estate_value`, `public.zip_census_redfin_join`, `public.cash_pay_zip_rank`, and the bullet lists on `public.procedure_surgeon_fees` hold only practice-area ZIPs, as do their CSV artifacts. Each carries `practice_zip_code`, `county` (`Los Angeles` or `Orange`), and `distance_from_practice_miles`. `public.census_zip_population` is never pruned: it keeps every row it already stored, including ZIPs outside the area, and the join leaves those out. To move the practice, change `PRACTICE_ZIP_CODE` and run the California load by hand. The full replace rewrites every row with the new distances.

## California load

`load_california.py` reads the Zip row of Monthly Housing Inventory on [Realtor.com Research](https://www.realtor.com/research/data/) and downloads the Historical Data CSV (not the current-month file). The download covers every state. A ZIP is kept only when it is in the practice area above, so the table and `california_zip_inventory.csv` hold Los Angeles and Orange County ZIPs within 50 miles of the practice ZIP. Each row gets that ZIP's `practice_zip_code`, `county`, and `distance_from_practice_miles`.

A scheduled run, and a run started by hand, replaces `public.redfin_real_estate_value`. The script deletes every existing row in that table, then inserts the current extract. A ZIP or month that is no longer in the file does not remain. Rows are not appended. No other table is dropped or altered.

A run started after Census ZIP population succeeds does not do that replace. It loads only the calendar year of the Census `acs_year` and inserts or updates those months. It does not delete stored months of a practice-area ZIP, and it brings their practice fields up to date. It does delete every stored month of a ZIP outside the practice area. That deletion goes by ZIP, never by month. That run's `california_zip_inventory.csv` contains that year only. Older months remain in `public.redfin_real_estate_value`.

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
| `practice_zip_code` | text | The practice ZIP from `practice.py`, `90210` |
| `county` | text | `Los Angeles` or `Orange`, the ZIP's majority county by land area |
| `distance_from_practice_miles` | numeric | Miles from the practice ZIP, two decimals. 50 or less. |
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

`pull_census_zip_population.py` loads American Community Survey 5-year estimates for the practice-area ZIP codes in the home-value extract. Those are the Los Angeles and Orange County ZIPs within 50 miles of the practice ZIP. The extract is the committed statewide `california_zip_inventory.csv` (the 2026-10-23 pull). The Census download requests only the California ZCTAs that are in both, codes with area in a California county in the same ZCTA-to-county relationship file. After a batch is downloaded, the script puts it in a dataframe and keeps a ZIP only when it is in that set. The stored geography is the ZIP Code Tabulation Area, in the `zip` column. The script does not read office, dermatology, or NPI locations.

Each run fetches only the latest published ACS 5-year vintage. Income, age, sex, age bands, race and ethnicity, education, household income brackets, mothers and births, housing tenure, and occupation are separate requests of about 10 ZIP codes, with a couple of seconds between calls. A failure retries that field group. A closed connection, a dropped connection, or a read timeout is retried several times, and each retry waits longer than the one before it. The read timeout is 45 minutes. Each finished batch is written to `census_zip_population.csv` before the next batch starts. When `DATABASE_URL` is set, that batch is inserted before the next request. A ZIP and year already stored are not inserted again, so rows saved before a timeout stay in the table and a later run fills ZIPs that are still missing. Added columns come in two sets: race, ethnicity, and education, and then the profile columns below. A stored ZIP gets a set filled in only when every column in that set is blank, so rows stored before a set existed pick it up on the next run. Nothing already stored is overwritten. Older years are not deleted. When `DATABASE_URL` is unset, the script still writes the CSV and leaves the table unchanged. That CSV is the one vintage from the run. It is not committed. The estimates request is the step that needs a Census key; without one, no CSV is written. A rejected key stops the run. It is not treated as an unpublished year.

Columns are `acs_year`, `zip`, `median_household_income` (`B19013_001E`), `per_capita_income` (`B19301_001E`), `median_age` (`B01002_001E`), `male_population` and `female_population` from `B01001`, `adult_population` (age 18 and older, summed from the `B01001` age bands), and `population_18_34`, `population_35_44`, `population_45_54`, `population_55_64`, and `population_65_plus`.

Race and ethnicity come from `B03002` (Hispanic or Latino origin by race), counted over every resident. `hispanic_or_latino_population` is Hispanic or Latino of any race (`B03002_012E`). The rest are not Hispanic or Latino: `non_hispanic_white_population` (`B03002_003E`, White alone), `non_hispanic_black_population` (`B03002_004E`, Black or African American alone), `non_hispanic_asian_population` (`B03002_006E`, Asian alone), and `non_hispanic_other_or_multiracial_population`, the sum of American Indian and Alaska Native alone, Native Hawaiian and Other Pacific Islander alone, some other race alone, and two or more races (`B03002_005E`, `_007E`, `_008E`, `_009E`). Those four are each small in Los Angeles County ZIPs, so they share one group. The five groups add up to the ZIP's total population.

Education comes from `B15003` (educational attainment for the population 25 years and over). `population_25_plus` is the sum of the five groups. `education_less_than_high_school` is no schooling through 12th grade with no diploma (`B15003_002E` to `_016E`). `education_high_school` is a regular diploma or a GED or alternative credential (`_017E`, `_018E`). `education_some_college_or_associate` is some college with no degree or an associate's degree (`_019E` to `_021E`). `education_bachelors` is a bachelor's degree (`_022E`). `education_graduate_or_professional` is a master's, professional school, or doctorate degree (`_023E` to `_025E`). A group is blank when any code in it is blank. Each run checks the Census labels for every code before downloading, and a changed label stops the run.

The profile columns are the second set.

- **Sex by age.** `male_population_18_34`, `female_population_18_34`, and the same pair for `35_44`, `45_54`, `55_64`, and `65_plus`. They are the male and female halves of the `B01001` age bands above, so no extra request is made. The 55-and-older persona adds `55_64` and `65_plus`.
- **Household income.** `households` (`B19001_001E`) and 16 brackets from `B19001`, `households_income_under_10000` through `households_income_200000_plus`. Each column name gives its bracket. The brackets add up to `households`.
- **Mothers.** From `B23003`, women 20 to 64: `women_20_64` (`_001E`), `mothers_with_children_under_18` (`_002E`, with own children of the householder under 18), and `mothers_with_children_under_6` (`_003E` under 6 only, plus `_010E` under 6 and 6 to 17).
- **Births.** From `B13016`, women 15 to 50: `women_15_50` (`_001E`), `recent_births` (`_002E`, women with a birth in the past 12 months), `recent_births_15_34` (`_003E` to `_006E`), `recent_births_35_44` (`_007E`, `_008E`), and `recent_births_45_50` (`_009E`). The youngest ACS band starts at 15, so 15 to 34 is the closest fit to the 18-to-34 persona.
- **Tenure.** `owner_occupied_households` and `renter_occupied_households` from `B25003`. From `B25007` (tenure by age of householder), `owner_occupied_householder_*` and `renter_occupied_householder_*` for four bands. `15_34` is householders 15 to 24 plus 25 to 34; its youngest band starts at 15, so this is the closest fit to 18 to 34. `35_44` and `45_54` match the personas exactly. `55_plus` is 55 to 59, 60 to 64, 65 to 74, 75 to 84, and 85 and over.
- **Occupation.** From `C24010` (sex by occupation, civilian employed 16 and older), female side: `employed_women` (`_038E`), `women_management_business_science_arts` (`_039E`), and `women_arts_design_entertainment_sports_media` (`_051E`, "Arts, design, entertainment, sports, and media occupations"). That line is a clean occupation split, so the industry table `C24030` is not used.

Census sentinel values are stored as null. The primary key is (`zip`, `acs_year`). Each newly inserted row also stores `dataset_updated_at`, the same timestamptz for every new row in that run. A ZIP already stored for that year is not rewritten, so its timestamp stays as it was. The one exception is a ZIP whose race, ethnicity, and education columns are filled in. That row gets the run's timestamp when either set is filled in. If the load fails, the backup export leaves out any of those columns that the table does not have yet.

ACS 5-year ZIP queries request zip code tabulation areas directly and do not send `in=state`. That geography is not nested in a state. Census answers `in=state:06` with an unsupported-hierarchy error, and that error is not a missing ZIP. California is applied by requesting California ZCTAs and then keeping the practice-area rows in the dataframe. When `CENSUS_API_KEY` is set, the script sends it on the first request and does not print it.

`.github/workflows/pull-census-zip-population.yml` runs once a day at 17:17 UTC (`17 17 * * *`). GitHub cron is UTC and does not follow daylight saving, so that is 10:17 AM Pacific Daylight Time and 9:17 AM Pacific Standard Time. It also runs on `workflow_dispatch`. After a successful load it uploads `census_zip_population.csv` as the `census-zip-population` artifact and keeps it for 90 days. If the load step fails, the job exports the current `public.census_zip_population` rows and uploads the same artifact. The job passes `DATABASE_URL`, `CENSUS_API_KEY`, and the refresh-review webhook secrets. A successful run on `main` starts the California workflow. This workflow does not cancel that run.

## Census and Redfin join

The chained California job reads `acs_year` from the `census-zip-population` artifact of the Pull Census ZIP population run that started it. Realtor.com still publishes one history file. This run keeps months in that census calendar year and does not stage the other years. Those months are inserted or updated in `public.redfin_real_estate_value`. The load does not delete rows, so a month already stored outside this run stays. A scheduled or manual California run reads that same artifact from the latest successful run of that workflow on `main`. It does not repeat this year-only load.

The join uses the latest `month_date_yyyymm` in that year. The Actions log prints that month. It is the maximum month in the file just pulled whose year equals the Census `acs_year`. It is not a fixed month in the code. Earlier months in that year are stored by the pull and are not copied into the join.

A ZIP is kept only when that month's `postal_code` equals the Census `zip`, and only when its Redfin row is in the practice area: `practice_zip_code` is the current practice ZIP, `county` is Los Angeles or Orange, and `distance_from_practice_miles` is 50 or less. ZIPs farther out are not kept in the join. A Redfin file without those three columns is refused. The grain is one row per ZIP. Census values and prices are copied from those two rows. A blank price stays blank. A ZIP missing on either side is left out.

The file is `zip_census_redfin_join.csv`. Columns are the Census columns, including the race, ethnicity, and education counts, then the Redfin columns, including `acs_year`, `zip`, `postal_code`, and `month_date_yyyymm`. The workflow uploads it as the artifact `zip-census-redfin-join` on every California run, kept for 90 days. Download it from that run's summary. When `DATABASE_URL` is set, the same rows replace `public.zip_census_redfin_join`. That replace writes the same `dataset_updated_at` on every row. Replacing the join table does not delete `public.redfin_real_estate_value`. Before the replace, any Census column the table is missing is added. A Census artifact from before an added column existed still joins, and that column stays blank.

A scheduled or manual California run replaces the whole price table and then writes this join. The Redfin file for that join is the full inventory that load just wrote. Months outside the Census `acs_year` stay out of the join.

## Cash-pay ZIP rank

After that join succeeds, the same California job runs `rank_cash_pay_zips.py` on `zip_census_redfin_join.csv`. The script ranks ZIPs already in the join. A join row outside the practice area is skipped as well. `cash_pay_zip_rank` carries `practice_zip_code`, `county`, and `distance_from_practice_miles` after `zip_name`. It does not rebuild the join, and it does not delete `public.redfin_real_estate_value`. A scheduled or manual run ranks in the job that follows the full load.

A ZIP is skipped for a persona when that persona's age count, `adult_population`, `median_household_income`, `per_capita_income`, or `median_listing_price_per_square_foot` is blank, when `adult_population` is zero, or when `active_listing_count` is blank or under 10. Ages 55 and older use `population_55_64` plus `population_65_plus`, and a blank in either band skips the ZIP for that persona. The middles of household income, per-person income, and listing price per square foot are recalculated from the ZIPs that remain, which are the eligible Los Angeles and Orange County ZIPs within 50 miles of the practice. They are not fixed dollar amounts. The text calls them the medians of ZIP codes within 50 miles of the practice.

Ages 18 to 34, 35 to 44, and 45 to 54 stay on the list when household income and listing price per square foot are both at or above those middles. Ages 55 and older stay when listing price per square foot and per-person income are both at or above those middles. The largest age share is rank 1. An equal share keeps the higher household income first for the working-age personas and the higher listing price per square foot first for ages 55 and older. Each persona keeps 25 ZIPs, or every ZIP that passed when fewer than 25 do. One ZIP can be on more than one persona. The female share is stored and shown. It does not change the rank. Race, ethnicity, education, sex by age, affordable households, mothers and births, home ownership, and occupation are never a screen or a rank key. `median_age` and `median_listing_price` are stored and do not change the rank.

The file is `cash_pay_zip_rank.csv`. The workflow uploads it as the artifact `cash-pay-zip-rank` on every California run, kept for 90 days. When `DATABASE_URL` is set, those rows replace `public.cash_pay_zip_rank`. The replace writes one `dataset_updated_at` on the rank rows. Replacing the rank table does not drop it and does not modify the flood or risk tables.

The same run writes `marketing_bullets` on `public.procedure_surgeon_fees`. That text column is not part of the primary key. The preferred `report_year` is `acs_year` on `zip_census_redfin_join`. A year qualifies when `surgeon_fee_low` and `surgeon_fee_high` are both non-null on more than half of that year's procedure rows. When the join year qualifies, bullets are written only on that year. When it does not, bullets are written only on the most recent report year that does. Every other report year is left blank in that column. The rank does not fill fee columns. Every row of the chosen year gets the ranked ZIPs for the personas that procedure uses. Procedures that use the same personas get the same ZIPs. The January fee load does not write this text.

Each procedure then gets an affordability screen. A ZIP's estimated yearly discretionary spending is that persona's share times the ZIP's median household income, for every persona. BLS shares are per household. The 55-and-older cash screen still uses per-person income; only this step uses household income. The share comes from the [BLS Consumer Expenditure Surveys, 2025, Table 1300 (age of reference person)](https://www.bls.gov/cex/tables/calendar-year/mean-item-share-average-standard-error/reference-person-age-ranges-2025.xlsx). BLS has no discretionary line, so discretionary spending here is food away from home, alcoholic beverages, apparel and services, entertainment, personal care products and services, and reading, divided by mean income before taxes. Two-band personas are weighted by consumer units. The shares are 10.7% for 18 to 34 (under 25 plus 25 to 34), 9.5% for 35 to 44, 12.3% for 45 to 54, and 10.8% for 55 and older (55 to 64 plus 65 and older). The price is the midpoint of `surgeon_fee_low` and `surgeon_fee_high` on the chosen report year. A ZIP whose discretionary spending is below that price is dropped from that procedure's list. The rest are re-ranked from the persona's full passing list, then cut to 25, so rankings differ by procedure. A procedure with a blank fee, such as 2024 eyelid surgery, is not screened. Its header still recommends the age group and says affordability wasn't checked. `cash_pay_zip_rank` keeps the persona ranking without this screen.

The stored form is one entry per ranked ZIP, in persona-list order. `marketing_bullets` has one line per ZIP, holding that ZIP's age-group header sentence. `rank` (`integer[]`), `city`, `state`, `zip_code`, `practice_zip_code`, `county`, `distance_from_practice_miles` (`numeric[]`), and `insight` (the rest `text[]`) are same-order lists with one element per ZIP. `city` and `state` come from the Realtor.com `zip_name`, split at its last comma. The header opens with a recommendation and then gives the reasons, for example `Market arm lift to adults ages 35 to 44 in the ZIP codes below. Why: median household income and median listing price per square foot in these ZIP codes are at or above the Los Angeles County medians of $96,355 and $616, and a typical household's discretionary spending of about 9.5% of household income covers the $8,250 midpoint surgeon fee. ZIP codes are ranked by their share of adults ages 35 to 44. Race, ethnicity and education describe these ZIP codes and are not used to rank them. Source: BLS Consumer Expenditure Surveys, 2025.` The source sentence is left off when the procedure has no fee. `insight` is the reason to target that ZIP, for example `Why Marina del Rey (90292): 28.9% of adults are ages 35 to 44, the highest share in this list. Median household income is $146,167 and median listing price per square foot is $858, both at or above the county median. About $13,954 a year in discretionary spending covers the $8,250 fee. Women are 53.8% of residents. Non-Hispanic White residents are the largest group at 60.8%, and 72.1% of adults 25 and older have a bachelor's degree or higher.` The race and ethnicity share is out of all residents. The degree share is bachelor's plus graduate or professional, out of adults 25 and older. When one is blank, the sentence gives only the other, and when both are blank there is no sentence. The city is title-cased in the text only; the `city` field keeps the Realtor.com case. Dollars in the text are rounded to whole dollars, with half a dollar rounding up. The stored numbers are not rounded. `household_income` (`integer[]`), `individual_income` (`integer[]`, the ZIP's ACS `per_capita_income`), `median_listing_price_per_square_foot` (`numeric[]`), `age_range` (`text[]`, the persona band such as `35 to 44` or `55 and older`), and `sex` (`text[]`: `woman` when the female share the insight prints is 50.0% or more, otherwise `man`, blank when the share is missing) are five more same-order lists. `race_and_ethnicity` (`text[]`) is the largest of the five race and ethnicity groups: `Hispanic or Latino`, `Non-Hispanic White`, `Non-Hispanic Black`, `Non-Hispanic Asian`, or `Non-Hispanic other or multiracial`. `education_level` (`text[]`) is the largest of the five attainment groups among adults 25 and older: `Less than high school`, `High school`, `Some college or associate degree`, `Bachelor's degree`, or `Graduate or professional degree`. A tie goes to the group listed first. Either label is blank when any of its counts is blank.

After `education_level` come the profile columns. Each is blank when a count it needs is blank. Every percent is percentage points to one decimal, such as `52.8`.

- `women_in_age_range` and `men_in_age_range` (`integer[]`) are the ZIP's women and men in the row's `age_range`. `women_share_of_age_range` (`numeric[]`) is the women's percent of that band. `sex` is unchanged.
- `income_needed` (`integer[]`) is the fee midpoint divided by the persona's BLS discretionary share, rounded to whole dollars. It is the household income whose discretionary spending just covers the fee.
- `affordable_households` (`integer[]`) is the estimated number of households with income at or above `income_needed`. `affordable_households_share` (`numeric[]`) is that number's percent of all households.
  - Brackets above the threshold count in full. Inside the bracket that holds the threshold, households are assumed to be spread evenly, so the part at or above the threshold is linear.
  - For a threshold of $200,000 or more, the open top bracket uses a Pareto tail. The shape is alpha = ln(N150 / N200) / ln(200,000 / 150,000), where N150 and N200 are the households at or above $150,000 and $200,000. The estimate is N200 × (200,000 / threshold)^alpha. It is blank when no household falls between $150,000 and $200,000, because the tail cannot be fitted.
  - These use all households in the ZIP, not only the persona's age group.
- All three affordability columns are blank for a procedure with no fee.
- `mothers_with_children_under_18`, `mothers_with_children_under_6`, and `recent_births` (`integer[]`) are the ZIP's counts.
- `home_ownership` (`text[]`) is `own` when `owner_occupied_share` is 50.0% or more of all households, otherwise `rent`. `owner_occupied_share_age_range` (`numeric[]`) is the owners' percent among householders in the row's age band, from `B25007`.
- `women_management_professional_share` and `women_arts_entertainment_media_share` (`numeric[]`) are percents of employed women 16 and older.

Each insight adds these sentences:
- After the discretionary sentence: `About 63.9% of households here (8,597 households) earn at least $112,602, enough to cover the $10,750 fee.`
- The female sentence also gives the age band: `Women are 53.8% of residents and 52.8% of adults ages 35 to 44.`
- After race and education: `Here, 71.1% of households rent their home, and 68.8% of employed women work in management, business, science, or arts jobs.`
- Only for tummy tuck, breast lift, breast augmentation, breast reduction, and liposuction: `About 1,320 women ages 20 to 64 here are raising children under 18, including 920 with a child under 6.` These lists come from the same values the insight states, and the insight still prints the female percentage. `household_income` is the ZIP's median household income for every persona. The header and insight always say "median listing price per square foot", and every screen, middle, and tie-break uses `median_listing_price_per_square_foot`. An age group with no ranked ZIP adds no entry. A procedure year with no ranked ZIP, and every other report year, keeps all of these columns null. An older `bullet_rank` column is renamed to `rank`.

After that write succeeds, the same run reads `public.procedure_surgeon_fees` and writes `procedure_surgeon_fees.csv`. The file has every column on the table for every report year. The bullet fields come last, in this order: `marketing_bullets`, `rank`, `city`, `state`, `zip_code`, `insight`, `household_income`, `individual_income`, `median_listing_price_per_square_foot`, `age_range`, `sex`, `race_and_ethnicity`, `education_level`, then the profile columns in the order listed above. Each ranked ZIP becomes its own row, in the stored order. `marketing_bullets` on that row is its group's header sentence, so the header repeats on every ZIP row in that group. There are no header-only rows. `report_year`, `procedure`, and the fee columns repeat on each row. A procedure year with no ranked ZIP stays one row with the bullet fields blank. No cell has a line break. Every California load uploads that file as the artifact `procedure-surgeon-fees` and keeps it for 90 days. The January fee workflow is not part of this upload.

## National procedure counts and surgeon fees

`pull_procedure_surgeon_fees.py` loads national aesthetic procedure counts and surgeon fee ranges from the American Society of Plastic Surgeons procedural statistics reports. The table is national. It is not a Los Angeles fee. `fee_basis` says: national surgeon fee only, excludes anesthesia and facility, not Los Angeles. There are no office locations.

Each run tries the official PDF for the current year back through 2024:

`https://www.plasticsurgery.org/documents/news/statistics/{year}/plastic-surgery-statistics-report-{year}.pdf`

It upserts the years it can read and does not delete other years in `public.procedure_surgeon_fees`. Each row upserted in a run gets the same `dataset_updated_at`. A year this run does not write keeps the timestamp it already had. The primary key is (`procedure`, `report_year`). `marketing_bullets` is a text column on this table. This load adds it when the table is created or when the column is missing, and the upsert does not change it. A new report year is inserted with that column blank. The census-year rank writes `marketing_bullets` on the join's `acs_year` when that year has surgeon fees on more than half of its rows, and otherwise on the most recent report year that does. A checked-in seed is used only when that year's official PDF cannot be fetched. The 2024 URL currently returns 404, so `seeds/procedure_surgeon_fees_2024.csv` supplies the 2024 counts and fee ranges read from that report. When the URL works again, the PDF replaces the seed. The 2025 report publishes counts and does not publish surgeon fees, so 2025 fee columns stay null. 2024 fees are not copied onto 2025.

2024 publishes separate upper and lower blepharoplasty fee ranges. Those two ranges are not combined into one eyelid-surgery fee, so `eyelid surgery` keeps its published count and null fee columns.

The CSV `procedure_surgeon_fees.csv` is written even when `DATABASE_URL` is unset. It is not committed. That file from this load does not include `marketing_bullets`. `.github/workflows/pull-procedure-surgeon-fees.yml` runs at 16:00 UTC on January 1 and on `workflow_dispatch`. The job installs `poppler-utils` for `pdftotext`. After success it uploads `procedure_surgeon_fees.csv` as the `procedure-surgeon-fees` artifact for 90 days. If the load fails, it exports the current table to that same CSV and uploads the same artifact. The job passes `DATABASE_URL` and the refresh-review webhook secrets. The census-year rank writes a separate `procedure_surgeon_fees.csv` that does include `marketing_bullets`, and every California load uploads it.

## Competition and search trends

`pull_competition_trends.py` writes one row per ZIP in `zip_census_redfin_join` for each `procedure` in `procedure_surgeon_fees`, Los Angeles and Orange County ZIPs within 50 miles of the practice (see [Practice area](#practice-area)). A join row whose `practice_zip_code`, `county`, or `distance_from_practice_miles` puts it outside that area is left out. Those three columns follow `city` and are copied from the join. In Tableau, join it to `procedure_surgeon_fees.csv` on `zip_code` and `procedure`. Both are text, and the procedure names are the same lowercase names, such as `facelift`. The file is `competition_trends.csv`. When `DATABASE_URL` is set, the same rows replace `public.competition_trends`, primary key (`zip_code`, `procedure`). No other table is modified.

### Competitors

Competitor counts come from the free [CMS NPPES NPI Registry API](https://npiregistry.cms.hhs.gov/api-page). A competitor is an active individual NPI (type NPI-1, status `A`) counted at the ZIP of its primary practice location address. The mailing address and secondary practice locations are not used. A provider counts in a specialty when any taxonomy on its record matches:

| Column prefix | Taxonomy codes |
| --- | --- |
| `plastic_surgeon` | every code starting `2082` (plastic surgery, head and neck, hand) |
| `dermatologist` | every code starting `207N` (dermatology and its subspecialties) |
| `facial_plastic_surgeon` | `207YS0123X` otolaryngology facial plastic surgery, plus `207YX0905X` and `207YX0007X`, the other otolaryngology facial plastic codes |
| `oculoplastic_surgeon` | `207WX0200X` ophthalmic plastic and reconstructive surgery |

`2086S0122X` (general surgery, plastic and reconstructive) and registered nurses with a plastic surgery taxonomy are not counted. One doctor with two matching taxonomies counts once in each of those specialties.

`*_count` is the number in the ZIP. `*_count_5mi` adds every Census ZCTA whose internal point (2024 Census Gazetteer) is within 5 miles of this ZIP's point, this ZIP included, using the same haversine distance as `distance_from_practice_miles`. That includes ZCTAs in any county and ZCTAs not in the join, such as across the Ventura or San Bernardino county line or beyond the 50-mile cutoff, and NPPES is read for every 3-digit prefix those ZCTAs use. `adult_population` is the join's ACS adults 18 and older. `adult_population_5mi` is the sum over those same ZCTAs; for a ZCTA outside the join, the adults come from the ACS 5-year table B01001 for the join's `acs_year`, summed over the same age bands. When any of those ZCTAs has no adult count, or the ACS request fails, `adult_population_5mi` and the 5-mile densities are blank while the 5-mile counts are kept. `*_per_10k_adults` is the count times 10,000 divided by the adults, rounded to two decimals. It is blank when the adults are blank or zero. A ZIP with few residents, such as a campus ZIP, can show a very high ZIP density; the 5-mile density is steadier.

`procedure_competitor_count`, `procedure_competitor_count_5mi`, and `procedure_competitors_per_10k_adults_5mi` count the specialties named in `procedure_competitor_specialties`, once per NPI. Body and breast procedures use plastic surgeons. Facelift, neck lift, and rhinoplasty add facial plastic surgeons. Eyelid surgery and forehead lift also add ophthalmic plastic surgeons. Dermatologists are shown in their own columns and are not in the procedure count.

Med spas, nurse injectors, and other businesses without a physician NPI are not in NPPES and are not counted. NPPES addresses are self-reported and can be stale. Search interest stays on the Los Angeles DMA 803, which includes Orange County. `competition_limitations` repeats this on every row.

### Search interest

Search interest comes from Google Trends through `pytrends`, web search, the Los Angeles CA market (DMA 803, `US-CA-803`), over the past five years. `trends_search_term` is the phrase used for that procedure: the procedure name, except `brazilian butt lift` for buttock augmentation with fat grafting, `blepharoplasty` for eyelid surgery, and `brow lift` for forehead lift.

- `trends_dma_interest_12mo_avg` is the average weekly index over the latest 52 complete weeks. `trends_dma_interest_prior_12mo_avg` is the 52 weeks before that. Google scales each request so its busiest week is 100. Every request includes `liposuction`, and the other requests are rescaled to the first one through that term, so procedures in the same run compare with each other.
- `trends_dma_interest_change_vs_prior_12mo` is the change between those two averages on the ratio scale. `0.25` is 25 percent. Tableau's percentage format shows it as a percent.
- `trends_city_interest_5yr` is Google's city index for this ZIP's city, over five years: 100 is the city in the Los Angeles market with the most interest for that term. The ZIP's city is the `city` column from the Realtor.com `zip_name`, matched to Google's city name ignoring case and hyphens; `trends_city_name` is the Google name that matched. Google shows a city only when it has enough searches. A 12-month window was empty for every procedure tried, which is why this is five years, and on the October 2026 pull only breast augmentation and rhinoplasty had city values; the other procedures are blank in these columns. A neighborhood the Realtor name lists separately, such as a part of the City of Los Angeles, often has no Google city and stays blank. A blank is not zero interest.
- `trends_pulled_at` and `trends_city_pulled_at` are when those values were pulled from Google. `trends_status` and `trends_city_status` say `new` (pulled this run), `reused recent pull` (pulled under six days ago, so Google was not asked again), `kept last good value` (this run's pull failed), or `unavailable`.

Google Trends is unofficial and rate-limited. Requests are spaced 12 to 20 seconds apart, a failed request waits about one, two, then four minutes before it tries again, and the whole Trends step stops after 25 minutes. A Trends error never fails the run. The last good values are kept in `competition_trends_cache.json`; when that file is missing they are read back from `public.competition_trends`.

`competition_pulled_at` is when NPPES was read. `dataset_updated_at` is the same on every row of a run. `competition_source`, `centroid_source`, and `trends_source` name the sources on every row.

### Schedule

`.github/workflows/pull-competition-trends.yml` runs every Monday at 18:43 UTC (`43 18 * * 1`, 11:43 AM Pacific Daylight Time and 10:43 AM Pacific Standard Time) and on `workflow_dispatch`. It is its own workflow, not a step in the daily Census chain: Google Trends is asked once a week, NPPES changes slowly, and a Trends or NPPES problem cannot hold up the join. It downloads `zip-census-redfin-join` and `procedure-surgeon-fees` from the latest successful `Load California ZIP inventory` run on `main` that still has both. When none does, the script reads `public.zip_census_redfin_join` and `public.procedure_surgeon_fees` instead, and falls back to `seeds/procedure_surgeon_fees_2024.csv` for the procedure names. It uploads `competition_trends.csv` as the artifact `competition-trends` and the cache as `competition-trends-cache`, both kept 90 days. The next run reads that cache. If the NPPES or gazetteer download fails, the run fails without replacing the table, and the job exports the current `public.competition_trends` rows as the `competition-trends` artifact. It uses `DATABASE_URL` and `CENSUS_API_KEY`, the same key as the Census pull, for the adults of ZCTAs outside the join. It does not post the refresh review webhook.

Run it by hand:

```
pip install -r requirements.txt
python pull_competition_trends.py --join zip_census_redfin_join.csv --procedures procedure_surgeon_fees.csv
```

`--skip-trends` keeps the last good Trends values without asking Google.
