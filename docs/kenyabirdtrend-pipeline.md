# Kenya Bird Trends: latest species records by QSD

For every eBird 2026 reporting species and every Kenya Bird Trends square (QSD), find the latest Kenya Bird Map (ABAP) or eBird record, with a link to its card or checklist and its observer.

- **Taxonomy.** eBird 2026. Each taxon is resolved through `report_as`; only taxa whose reporting taxon has `category == "species"` are kept.
- **Years.** Every year in the selected snapshots. There is no atlas-period cutoff.
- **Meaning.** This is a latest-record inventory, not a trend or absence estimate. Only observed species–square pairs are exported.

The output starts minimal. Fields are added when the website needs them.

## Who owns what

| Repository | Owns |
| --- | --- |
| `bird-atlas` | Sources, taxonomy, grid utilities and `scripts/shared/latest_by_qdgc.py`. Output is keyed by the standard **level-1 QDGC** code; there is no knowledge of `Sq`. |
| `KenyaBirdTrends` | The `Sq ↔ qdgc` mapping (`src/utils/squares.js`, tested in `tests/squares.test.js`) and `npm run import:found`, which rolls species up to comparison concepts and writes `src/assets/found.json` for the Lost & Found page. |

The mapping needs no extra file and no change to KenyaAtlasComparison. All 228 `grid.json` polygons are QDGC level-1 cells. The `Sq` letter is the QDGC quadrant letter in lower case (`51b` = `E037N00B`), and each `Sq` number is one 1° cell (67 in total). KenyaBirdTrends derives the mapping from its own `grid.json` + `map_data.json`. If KenyaAtlasComparison ever changes the grid, the test fails.

## Inputs

| Input | Current choice |
| --- | --- |
| ABAP prepared package | `data/prepared/abap/downloaded/kenya_2007-07-23_2026-11-03_reporting_parents` (eBird 2026) |
| EBD raw release | `data/raw/ebird/ebd_AFR_relSep-2026/ebd_AFR_relSep-2026.txt.gz` (eBird **2025** concepts) |
| eBird taxonomy | `data/taxonomy/ebird_2026.csv`; the script checks it matches the ABAP manifest hash |
| Regional overrides | `data/taxonomy/regional_overrides_ebird_v2026.csv`, regions `kenya` and `africa` |

The prepared eBird package is not used: it is on eBird 2025 and holds candidate cards, not checklist dates.

## Steps

1. **Reporting species.** Follow `report_as` (a species code) through the full 2026 reference until it stops, then keep only `species`. This applies to both sources.
2. **ABAP.** Join records to cards. `Spp` codes listed in `data/curated/abap_kenya_flagged_spp.csv` (`--abap-exclude`) are excluded, not remapped. These are species not expected in Kenya, such as southern African codes used for Kenyan birds. `abap_flagged.csv` lists them with their record, card, pentad and observer counts and date range, for the Kenya Bird Map vetting team. All mapping statuses are kept, including `broad_parent`. In the current package `broad_parent` targets are slashes or spuhs, so step 1 drops them. The date is the card **start date**. The square comes from `pentad2qdgc(pentad, level=1)`. Observer names fill in from full-protocol cards with the same observer ID, because ad hoc cards have no names.
3. **EBD.** DuckDB scans the raw file for `COUNTRY CODE = 'KE'` and `APPROVED = '1'`. The cache cannot be used because it has no country column. A 2025 concept absent from 2026 is remapped only through the regional overrides. Little Swift goes to `litswi2`, which reports as Little Swift. *Corypha africana* (2025 species `avibase-4E923B31` and issf `avibase-6164890C`) goes to the 2026 species `avibase-EB9CE6A2`, following a user decision on 2026-11-04. Any other retired concept is dropped and listed in the manifest. The square comes from `latlng2qdgc(lat, lng, level=1)` on the checklist coordinates.
4. **Latest.** For each `(qdgc, species)`, keep the one record with the latest date. On a same-day tie, eBird wins; after that, the smallest ID wins.
5. **Periods** (for the target/found page). Two cut-off dates are used: `--historical-before` (default `2009-01-01`) and `--found-from` (default `2024-01-01`). Each pair also gets:
   - the last record before the first cut-off: `hist_date`, `hist_source`, `hist_id`
   - the last date between the two cut-offs: `mid_date`
   - the first date on or after the second cut-off: `found_date`
   - the earliest record of any precision: `first_date`, so that "new" can mean no record at all before 2024.

   `finders.csv` lists every observer who recorded the pair on `found_date`, with one row per observer and where the record was made: `lat, lng` of the eBird checklist start, or the KBM `pentad` and its centre.

   **Location precision.** An eBird checklist is placed at its starting point, so the birds on a long travelling checklist may belong to another cell. Records from checklists longer than `--precise-km` (default 10 km) count toward `mid_date`, where they can only remove a target. They are not used for `hist_*`, `found_date` or `finders.csv`, which place a species in a cell. About 30% of Kenya eBird records are from such checklists. Kenya Bird Map cards are pentad-based and always count as precise.

## Output

`data/prepared/latest_by_qdgc/<run-id>/`:

| File | Content |
| --- | --- |
| `latest.csv` | `qdgc, species_id, date, source, id, observer_id, observer_name`, plus `hist_date, hist_source, hist_id, mid_date, found_date, first_date` |
| `abap_flagged.csv` | One flagged ABAP `Spp` code: reason, likely Kenyan species, records, cards, pentads, observers, first/last date |
| `finders.csv` | `qdgc, species_id, date, source, id, observer_id, observer_name, person, lat, lng, pentad`: everyone on the first found date, with the record's location |
| `species.csv` | `species_id, species_code, common_name, scientific_name, taxon_order` |
| `manifest.json` | Inputs and hashes, policies, row counts per source, unmapped retired concepts, date ranges |

`id` is the ABAP `card_id` or the eBird checklist ID (`https://ebird.org/checklist/<id>`). EBD has no observer names.

## Run

```sh
uv run python scripts/shared/latest_by_qdgc.py \
    --abap data/prepared/abap/downloaded/kenya_2007-07-23_2026-11-03_reporting_parents \
    --ebd data/raw/ebird/ebd_AFR_relSep-2026/ebd_AFR_relSep-2026.txt.gz \
    --taxonomy data/taxonomy/ebird_2026.csv \
    --overrides data/taxonomy/regional_overrides_ebird_v2026.csv \
    --abap-exclude data/curated/abap_kenya_flagged_spp.csv \
    --observers data/curated/observer_crosswalk.csv \
    --output data/prepared/latest_by_qdgc/<run-id>
```

Then, in KenyaBirdTrends:

```sh
npm run import:found -- ../bird-atlas/data/prepared/latest_by_qdgc/<run-id>
```

## Observer names

`scripts/shared/resolve_observers.py` ranks finders by the pairs they found that had no record in the mid period, and fills names for the top 100 into `data/curated/observer_crosswalk.csv`. The crosswalk gains a `source` column. `--extra` takes a CSV of `source,observer_id,checklist_id` to look up as well; use it with the site's actual top 100, whose ranking can differ. The script keeps the `person` column.
- **ABAP:** names come from full-protocol cards. Kenya Bird Map has no observer-name endpoint.
- **eBird:** the eBird API checklist view (`userDisplayName`) is called once every 20 seconds. This needs a personal key in `EBIRD_API_KEY` or `~/.config/ebird/api_key`. The script stops on 401, 403, 429 or 5xx errors and resumes where it left off.

ebird.org `robots.txt` disallows `/checklist`, so checklist pages are not scraped.

The crosswalk's `person` column links one person's ABAP and eBird IDs. Currently there are three links: Colin Jackson, Mustafa Adamjee and James Omenya. A linked person is shown under the `person` value whatever the source. With `--observers`, `latest_by_qdgc.py` fills `observer_name` from the crosswalk and writes a `person` key in `latest.csv` and `finders.csv`: the linked name, or `source:observer_id`. A linked person counts once per pair in `finders.csv`.

## Later, if needed

- Rufous-naped Lark: ABAP `Spp` 458 and the eBird 2025 concepts are mapped to the 2026 *Corypha africana* through `kenya` overrides (`user_candidate`). eBird 2026 also has Sentinel Lark (*C. athi*), so older records of *athi* filed as Rufous-naped would be misassigned. Revisit if this matters.
- Linking eBird species to the site's comparison concepts in `sp_base.json`, which can group several modern species (for example, Ostrich covers Common and Somali Ostrich).
- ABAP card URLs, once the Kenya Bird Map card route has been checked.
- Adding `COUNTRY CODE` to the EBD cache, if this is run often.
