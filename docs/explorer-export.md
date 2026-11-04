# Explorer export

`scripts/shared/export_explorer.py` turns a prepared eBird package into the folder opened by [bird-atlas-explorer](https://github.com/Rafnuss/bird-atlas-explorer). The folder is self-contained, so it can be zipped and shared on its own.

```sh
uv run python scripts/shared/export_explorer.py data/prepared/ebird/<package> --zip
```

- Output: `<package>/explorer/` (or `--output DIR`), and with `--zip`, `<package>-explorer.zip` next to the package.
- The export reads the package's CSV files with DuckDB and needs the `cards`, `checklist`, `records` and `species` resources. `pentads.csv` and `audit/record_sources.csv.gz` are used when present.
- `--memory-limit` (default `4GB`) caps DuckDB's memory; larger joins spill to a temporary folder inside the output.
- The folder is written to `<output>.tmp` and renamed when complete, so a failed run never leaves a partial explorer folder.

For `ebd_AFR_relSep-2026-adhoc` (669,944 cards, 1.5 million checklists, 10.2 million records) the export took 16 seconds with a 3.9 GB peak memory footprint, and wrote 113 MB.

## Files

All tables are Parquet with zstd compression. Text columns keep the package's values; empty strings become nulls.

| File | One row per | Sorted by | Notes |
|---|---|---|---|
| `explorer.json` | — | — | `format: "bird-atlas-explorer"`, `version`, `name`, file names, row counts, card protocols, date range, and the package's `manifest.json` as `provenance` |
| `cards.parquet` | card | `card_index` | `card_index` is the row number (cards sorted by pentad, start date and card ID). Card fields, plus `event_count` (shared submissions count once), and `<table>_offset` / `<table>_count` giving the card's rows in `checklists`, `card_taxa` and `checklist_taxa` |
| `card_taxa.parquet` | distinct (card, taxon) | card, taxon | `taxon_index` refers to `taxa.parquet` |
| `taxa.parquet` | eBird taxon | `taxon_index` | From `species.csv`, in eBird taxon order; taxa used by records but absent from `species.csv` come last |
| `checklists.parquet` | source checklist in a card | card, group, checklist | Effort and location fields, `card_effort_eligible` as a boolean |
| `checklist_taxa.parquet` | distinct (card, checklist, taxon) | card, checklist, taxon | From `audit/record_sources.csv.gz`; links each card taxon to the checklists that recorded it. Absent when the audit file is missing |
| `checklist_lookup.parquet` | (checklist, card) | upper-case checklist ID | Small row groups, so one checklist is found by reading one row group chosen from the column statistics |
| `pentads.parquet` | pentad used by a card | `pentad_id` | Bounds and centre from `pentads.csv`; pentads without bounds are placed at the mean of their checklist locations |

The explorer reads `cards`, `card_taxa`, `taxa` and `pentads` whole when a folder opens, and reads a card's rows of `checklists` and `checklist_taxa` by offset when the card is opened.

## Sharing

The data are derived from the eBird Basic Dataset. Share the zip privately, only with people who have accepted the eBird Data Access Terms; do not host it publicly without permission from the Cornell Lab of Ornithology.
