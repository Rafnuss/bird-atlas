# ABAP to eBird

This workflow downloads African Bird Atlas Project (ABAP) checklist detections from BirdMap, cleans and links cards, and maps source taxa to an explicitly selected eBird reference. The active curated crosswalk and regional overrides target **eBird 2026**. The exported names and taxonomy fields come from that eBird reference; AviList is not a runtime dependency. A source mapping can be provisional or broader than a modern eBird species, so an exported ID does not by itself prove that the underlying observations are taxonomically equivalent.

## 1. Obtain an ABAP snapshot

```sh
uv run python scripts/abap/download_abap.py --country kenya --start-date 2024-01-01 --end-date 2024-01-31
```

Omit the dates for the available country history, or use `--country` for another BirdMap country. The default destination is `data/raw/abap/downloaded/<country>_<start>_<end>/`; `--output-dir` chooses another location. `--resume` reuses an existing snapshot's saved request and responses. A completed snapshot has a `manifest.json`; its presence indicates that the downloader completed its checks. Repeating an incomplete request reuses cached responses. `--refresh` replaces the corresponding cached query, so preserve a snapshot first if it is needed for reproducibility.

The downloader requests both full-protocol and ad-hoc detections. Full-protocol records use date-partitioned API pages; ad-hoc records are fetched by species and year and then filtered to the requested dates. It retains original compressed responses, the combined `records.csv`, source catalogue, request metadata, counts and a completion manifest. See `scripts/abap/download_abap.py --help` for options, including concurrency.

**Coverage:** these are detections exposed by the public full-protocol and ad-hoc endpoints. Standalone incidental records are not included, and non-detections are excluded. The date filter uses checklist **start dates**, not individual bird observation dates; a card can span several days. The two endpoints do not necessarily supply identical metadata. Keep source IDs as strings, including `Spp`, `CardNo` and `Pentad`.

A separately supplied historical ABAP export is a different input format. The existing four-table file set is preserved locally, but its importer is not implemented; do not pass it to the API-snapshot preparation command.

## 2. Select taxonomy inputs

- **ABAP source catalogue:** `uv run python scripts/abap/download_abap_taxonomy.py` writes a dated BirdMap catalogue to `data/taxonomy/`. `Spp` is the source species code used in detections. The catalogue supplies source names and is not an eBird crosswalk.
- **eBird reference:** supply the eBird taxonomy CSV for the version targeted by the crosswalk. The current default is `data/taxonomy/ebird_2026.csv`. It must include `bio_concept_code`, `species_code`, scientific and common names, category, taxonomic order, and `report_as`. The eBird CSV is an input and is not automatically downloaded or updated by preparation.
- **Curated crosswalk:** `data/taxonomy/abap_avibase_crosswalk.csv` maps ABAP `Spp` codes to source Avibase concepts and eBird 2026 targets, with statuses and notes. Preserve the original code and mapping evidence.
- **Regional decisions:** `data/taxonomy/regional_overrides_ebird_v2026.csv` contains target alternatives, keyed by the source Avibase concept and an explicit region. These decisions are assumptions, not new identifications of individual observations.

The original BirdMap catalogue does not itself establish all Avibase IDs. The crosswalk and its historical audit incorporate external assignments and notes; their redistribution terms are separate from the software's MIT license and remain under review. Do not infer an original ID where the source and reviewed mapping do not support one.

## 3. Resolve mappings and regional overrides

The preparation script reads the source `Spp` as a string and looks it up in the curated crosswalk. For `ebird_v2026_status=exact`, the target is the crosswalk's `avibase_id`; otherwise the script uses `ebird_v2026_avibase_id` if supplied. A missing target stays unresolved. It checks that declared targets exist in the supplied eBird reference and retains mapping statuses and notes in `audit.csv`.

Regional overrides are applied **from broadest to narrowest**: Africa, then the selected subregion, then the country. For example, `kenya` applies `africa → east_africa → kenya`; `southern_africa` applies `africa → southern_africa`; `world` applies no regional override. The supported regions are `world`, `africa`, `east_africa`, `west_africa`, `southern_africa`, and `kenya`. Kenya defaults to `kenya`; other countries default to `africa`. Select a region explicitly when a different hierarchy is appropriate.

An override is keyed by the original source Avibase concept, not the worldwide fallback target. All source ABAP codes mapped to that same original concept inherit the applicable override. Africa-wide and Kenya decisions include provisional range-based assumptions: vagrancy, misidentification and broad source concepts can make individual observations differ. An absent override does not validate the worldwide match. `--region` chooses taxonomic assumptions; it **does not spatially filter** the input records.

For these decisions, `east_africa` covers Burundi, the Democratic Republic of the Congo, Djibouti, Eritrea, Ethiopia, Kenya, Rwanda, Somalia, South Sudan, Tanzania and Uganda. This is the pinned scope of the mapping assumptions, not a guarantee of species occurrence or a live political-membership definition.

## 4. Prepare and validate

```sh
uv run python scripts/abap/prepare.py \
  --snapshot data/raw/abap/downloaded/kenya_2024-01-01_2024-01-31 \
  --taxonomy data/taxonomy/ebird_2026.csv \
  --region kenya \
  --output-dir data/prepared/abap/downloaded/kenya_2024-01-01_2024-01-31

uv run python scripts/shared/validate.py data/prepared/abap/downloaded/kenya_2024-01-01_2024-01-31
```

Use a new output directory for each input snapshot or mapping policy. `--crosswalk` and `--overrides` can select explicit alternatives. The current implementation expects the eBird 2026 crosswalk's columns; supplying a different eBird CSV does not automatically produce a valid cross-version mapping.

The exporter retains full-protocol and ad-hoc detections. When the same source card number occurs in both, it merges the card metadata, gives full-protocol fields precedence, removes duplicate detections by source `Spp`, and records metadata conflicts and duplicate counts in the audit. It retains source taxa appearing in only one protocol. Source IDs and mapping statuses remain available for review.

The prepared files, common columns, provenance fields, and validation commands are documented in the [data-package reference](data-package.md). Source card dates describe a survey interval and the normalized columns do not add precision to the original data.

Validation checks data-package fields and relationships; it cannot verify bird identifications, taxonomic equivalence, coordinate accuracy or survey effort. Preserve the source snapshot and audit alongside the prepared data. The manifest records the selected reference and source provenance; update or reconcile mappings deliberately when changing taxonomy versions.

## Sources and reuse

ABAP records and source taxonomy come from the [BirdMap API](https://api.birdmap.africa/sabap2/v2/); consult its attribution and reuse terms. The [eBird taxonomy](https://www.birds.cornell.edu/clementschecklist/download/) is a separately supplied reference with its own citation and terms. The curated crosswalk and historical audit incorporate third-party assignments and are not covered by the software license without further review.
