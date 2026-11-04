# eBird preparation

This workflow reads eBird observations and constructs ABAP-style **candidate** cards, either full-protocol or ad hoc. It preserves the input's declared eBird taxonomy version and keeps original checklist and taxon identifiers for provenance. Candidate cards are derived units, not verified ABAP submissions. The shared output definitions are in the [data-package reference](data-package.md).

## Inputs

Supply either a species-level eBird Basic Dataset (EBD) `.txt` or `.txt.gz` file, a release directory containing one species file, or a supported eBird project export. EBD sampling metadata are useful source context but do not replace the species-observation input. Project exports may use different headers and default spatial assumptions from EBD.

Modern general EBD releases also use `PROTOCOL NAME`; this header alone does not establish that a file is a project export. Recognized EBD filenames and general EBD headers select the distance screen. Use `--source-kind ebd` explicitly for a renamed or ambiguous general release, or `--source-kind project` for an atlas project whose containment assumption is justified.

Supply the eBird taxonomy CSV that corresponds to the **input names**, and explicitly declare its version. The observation year does not determine the taxonomy version. The input reference must include species codes, concept IDs, scientific and common names, category, taxonomic order, and `report_as`; `TAXON_CONCEPT_ID` and `bio_concept_code` are supported column names for the reference concept ID. The input and reference provenance are retained in the manifest.

An African EBD release may be downloaded explicitly:

```sh
uv run python scripts/ebird/download.py --year 2026 --month Sep --output /path/to/raw/ebd
```

Alternatively, use an existing release or project export. Neither preparation nor the examples implicitly download source data.

## Prepare and validate

```sh
uv run python scripts/ebird/prepare.py \
  --input /path/to/observations.txt.gz \
  --taxonomy data/taxonomy/ebird_2025.csv \
  --ebird-version 2025 \
  --source-kind ebd \
  --cache data/cache/ebd/africa-sep2026.duckdb \
  --output /path/to/prepared-ebird

uv run python scripts/shared/validate.py /path/to/prepared-ebird
```

Use a new output directory for each source, reference or policy. `--source-kind ebd` or `--source-kind project` overrides automatic header detection. `--exclude-missing-distance` excludes checklists with missing reported distance; the default retains them, while preserving missingness and the spatial diagnostic.

## Running a full Africa release on a laptop

Preparation now reads the raw release once into a compact, persistent DuckDB cache. It selects the columns needed by the converter, preserves original concept/checklist identifiers and available subspecies names, and retains every observation regardless of taxon or checklist eligibility. It does not need to decompress the whole release to a separate text file. Only checklist metadata enter pandas, in batches containing complete histories of representative observers. Observation joins, event–taxon deduplication, presences, and provenance exports run on disk. See the [performance notes](ebird-performance.md) for measurements, scientific constraints, and the planned broader export schema.

The cache is independent of taxonomy and eligibility policy, so reuse `--cache` while choosing a new `--output` for each scenario. Omitting `--cache` stores it at `OUTPUT/ebd.duckdb`. A source path, size, modification time, and cache schema check prevents accidental reuse with a different source; the cache manifest stores the SHA256 calculated during construction. Incomplete caches are rejected. Keep the original source file: the cache deliberately omits unused columns such as comments and is not a replacement archive.

To build or inspect the cache before conversion:

```sh
uv run python scripts/ebird/cache.py \
  --input data/raw/ebird/ebd_AFR_relSep-2026 \
  --cache data/cache/ebd/africa-sep2026.duckdb

uv run python - <<'PY'
import duckdb
with duckdb.connect('data/cache/ebd/africa-sep2026.duckdb', read_only=True) as con:
    print(con.sql('SELECT "CATEGORY", count(*) FROM observations GROUP BY 1').df())
PY
```

Start with the defaults `--memory-limit 1GB --threads 4 --partitions 32`. The memory budget controls DuckDB; Python and the current checklist batch need additional memory. Increase `--partitions` to reduce the batch size, or reduce threads and DuckDB's budget on a smaller machine. All events assigned to a representative observer remain together, so one unusually prolific observer sets a lower bound on batch size. Intermediate data spill beside the working database in the output directory; use a local SSD with spare disk space. The working database is removed after a successful export, and the cache remains reusable.

The Python API retains its four-table return value for small interactive examples. For a complete release, call `export_ebird(..., return_tables=False)` to return the manifest without reloading `records.csv.gz` into memory. The command-line preparation script already uses this setting. `read_EBD()` remains an in-memory convenience function for small datasets.

## How cards are made

1. **Independent events:** shared submissions are collapsed to one event, as `auk_unique()` does. The lowest checklist ID supplies the date, start time and location; species are combined across members; all member checklist and observer IDs are kept. When members disagree on effort, the event takes the maximum duration and distance, counts as complete if any member is complete, and uses the most frequent protocol (a tie keeps the lowest checklist's). Such events keep `SHARED METADATA CONFLICT` for review but are not excluded. A representative observer groups effort; membership does not prove equal participation.
2. **Pentads and spatial screen:** reported coordinates determine the pentad. The screen compares reported location and distance with its boundaries; it cannot reconstruct a track. Project exports default to assuming containment, while general EBD uses the documented distance screen. This assumption can be overridden through `--source-kind`.
3. **Qualifying effort:** complete Stationary or Traveling checklists with a known start, positive duration and accepted spatial status contribute effort. Shared activities count once, and overlapping intervals for an observer and pentad are combined instead of summed twice. Incomplete or incidental checklists may supplement an established card without adding qualifying effort.
4. **Card windows:** every event that passes the spatial screen joins exactly one card for its observer and pentad, spanning at most five calendar days; cards for one observer and pentad never overlap. A card is **full** (`protocol` = `full`) with at least 120 minutes of qualifying elapsed effort, otherwise **ad hoc** (`adhoc`), mirroring BirdLasser/ABAP. `--card-selection optimized` (default) first chooses the full windows that give the most full cards, then walks the remaining events into ad-hoc cards; an ad-hoc card ends early instead of overlapping a following full card. `--card-selection walk` starts each card at the observer's next visit, as BirdLasser does, and then classifies it, which can give fewer full cards. The lower-level functions allow research sensitivity settings; a lower threshold is not standard ABAP card eligibility.
   Events that fail the spatial screen join no card. `audit/checklist_quality.csv.gz` records each event's `CARD` or its `UNASSIGNED REASON`, and the manifest totals them under `event_outcomes`.
5. **Taxonomy and records:** every taxon is kept, whatever its category (species, subspecies groups, forms, spuhs, slashes, hybrids, domestics). Each source concept ID is matched to the declared eBird reference by concept ID, or, if the reference lacks it, by a unique scientific name; `taxonomy_status` records which. Taxa are not rolled up: `report_as` in `species.csv` links each to its reporting taxon for optional later aggregation. The original observation's concept ID is retained separately from the matched eBird concept. Taxa absent from the reference are written to an exclusions file; their exclusion does not erase checklist effort. Retained card–taxon presences are deduplicated within each card.

The detailed columns, original-ID associations, audits and differences from ABAP detections are documented once in the [data-package reference](data-package.md).

## Limits and provenance

Do not use prefix samples or arbitrary row/date partitions to approximate complete cards: they can truncate shared groups, overlap checks, or five-day windows. Reported distance and coordinates are a screening basis, not proof of route containment; exact detection order, positional accuracy, habitat coverage and nocturnal effort remain unknown where the input does not establish them.

The [eBird notebook](../notebook/02_ebird_pipeline.ipynb) illustrates the preparation. Its locally selected project input and illustrative taxonomy version must be verified before research use. Combined datasets require compatible eBird reference provenance; cross-version taxonomy mapping is not implemented.
