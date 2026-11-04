# Full Africa EBD on a laptop

The main change is to separate observation-scale storage from checklist-scale calculation. Reading CSV in chunks only saves memory if those chunks are not subsequently concatenated. The previous exporter concatenated the complete release, made several expanded copies, then concatenated the complete source again to build provenance.

## What the data showed

The local September 2026 Africa release contains 35,533,400 observations, 2,247,959 source checklist IDs, 2,248,139 distinct checklist metadata rows, and 1,708,170 independent events. Shared groups reduce the event count; some source checklist IDs have conflicting metadata, which must remain available for review.

The first 200,000 observations already touched 90,527 checklists. The release is not checklist-contiguous. Processing arbitrary row chunks independently would split shared events and lose the information needed to select cards correctly. Splitting by year or month can also break five-day windows and midnight effort. A 200,000-row normalized pandas chunk occupied 137 MB; extrapolating that to the whole release gives about 24 GB for just one raw DataFrame, before concatenation, sorting and joins. This is an estimate, not a measured full-run baseline.

The raw category distribution also matters for the agreed future all-taxon export:

| Source category | Observation rows |
| --- | ---: |
| species | 33,782,261 |
| issf | 1,454,370 |
| spuh | 130,345 |
| domestic | 107,734 |
| slash | 41,389 |
| form | 10,191 |
| hybrid | 7,027 |
| intergrade | 83 |

These categories are retained in the cache. No scientific-name, taxon-category, date, protocol, completeness, or spatial filter is applied during preprocessing.

## What changed

1. Read the compressed TSV directly into a persistent DuckDB database, projecting the required columns and preserving identifiers as strings. Retain original observation IDs, source concept IDs, counts, categories and available subspecies fields. Comments and other unused columns stay in the original source archive.
2. Deduplicate checklist metadata on disk. Process complete histories of representative observers in batches, keeping shared events intact. Only the current checklist batch enters pandas; the full observation table never does.
3. Merge qualifying time intervals with grouped cumulative end times, then split merged intervals at midnight. This replaces many small pandas operations inside per-observer loops while preserving elapsed effort.
4. Select five-day windows with cumulative effort and date searches. Assign events with a backward date join instead of first generating every checklist × candidate pairing for an observer/pentad.
5. Match and deduplicate observations, construct presences, and write source associations through disk-backed SQL. Stream compressed CSV outputs; use fast gzip compression for the pandas audit batches. Cache and export manifests retain source/taxonomy hashes and processing settings.

DuckDB is the one added dependency: its disk-backed sorting and joins let the large intermediates spill to disk. The default budget is 1 GB and four threads; the budget is not a cap on the entire Python process. See [DuckDB's workload tuning guide](https://duckdb.org/docs/1.5/guides/performance/how_to_tune_workloads.html) for its memory/spill behavior.

## Measured full-release run

Measurements were taken on 4 November 2026 on this 12-core Mac with 36 GiB physical RAM, using DuckDB 1.5.6, a 1 GB DuckDB budget, four threads, and 32 observer batches. The physical machine was not an 8 GB laptop; the measured process footprints are the evidence for fitting a smaller laptop. Timings include local disk I/O and are indicative rather than a speedup against an attempted full-memory baseline.

| Stage | Elapsed | Peak resident memory |
| --- | ---: | ---: |
| Build current cache, including hashing | 4 min 59 sec | 1.07 GB |
| Convert using reusable cache, final rerun | 2 min 17 sec | 1.36 GB |
| Validate complete prepared package, final rerun | 42 sec | 0.80 GB |

The current cache occupies 2.22 GB. A smaller initial cache without the additional source/subspecies fields took 3 min 28 sec and occupied 1.59 GB; retaining that provenance is a deliberate storage tradeoff. Across the full-release conversion runs, elapsed time was 2 min 17 sec to 2 min 46 sec and peak resident memory was 1.36–1.56 GB; later runs also benefit from the operating system's file cache. The prepared candidate dataset occupies about 305 MB (291 MiB). Allow additional SSD space for the working database and temporary spill files during conversion. They are removed when their work completes; the reusable cache remains.

The complete run produced 65,156 full-candidate cards, 2,200,093 taxon presences, 2,278 exported taxa, and 4,399,251 original-observation associations. There were 131,030 excluded event–taxon rows under the existing reference-eligibility policy. Among all independent events, 436,936 qualified to contribute effort and 7,825 had shared metadata conflicts. These figures describe the current candidate-only schema, not a future all-event export.

The complete output passed the shared validator. A before/after comparison on the full local project export agreed on cards, species, pentads, source checklist metadata, presences, and all original-observation associations. Synthetic checks also compare different batch counts, preserve shared submissions and five-day boundaries, retain effort from unmatched taxa, handle zero-candidate outputs, and check the vectorized interval union against an independent minute-by-minute calculation.

## Reproduce the run

```sh
uv sync --frozen
uv run python scripts/ebird/prepare.py \
  --input data/raw/ebird/ebd_AFR_relSep-2026 \
  --taxonomy data/taxonomy/ebird_2025.csv \
  --ebird-version 2025 \
  --source-kind ebd \
  --cache data/cache/ebd/africa-sep2026-v2.duckdb \
  --memory-limit 1GB --threads 4 --partitions 32 \
  --output data/prepared/ebird/ebd_AFR_relSep-2026

uv run python scripts/shared/validate.py data/prepared/ebird/ebd_AFR_relSep-2026
```

Choose a different output directory when retaining multiple runs of the same input. On macOS, prefix either command with `/usr/bin/time -l` to measure elapsed time and maximum resident memory; Linux uses `/usr/bin/time -v`. Reusing the same cache skips source parsing and hashing. For less pandas memory, increase partitions; for less DuckDB memory, lower its budget and thread count. More threads can increase memory and disk contention, so measure before increasing them.

Modern general EBD files use `PROTOCOL NAME`, which the old reader mistakenly interpreted as a project-export marker. That could bypass the distance screen. The reader now recognizes modern general EBD files; the explicit `--source-kind ebd` above documents the intended policy. On this release, 541,605 events had a `possible_boundary_crossing` diagnostic, so this distinction materially affects eligibility. An explicit project containment assumption should be confined to an appropriate project source.

## Schema change: all taxa and ad-hoc events

Implemented after the measurements above; see the [eBird preparation guide](ebird-preparation.md#how-cards-are-made). The original plan follows. On 4 November 2026, the same machine and settings (1 GB DuckDB budget, four threads, 32 batches, reusing the cache) converted the full release in 4 min 29 sec with 1.8 GB peak resident memory: 65,605 full and 604,339 ad-hoc cards, 10,193,011 taxon presences across 3,773 taxa, and no taxon left unmatched. Of the 1,708,170 independent events, 1,172,081 joined a card; the other 536,089 fail the spatial screen (mostly Traveling lists, median 5.4 km against a median 1.0 km to the nearest pentad edge), so 19.7 million observation rows remain outside cards. The final presence and provenance joins run per observer batch so they stay within the DuckDB budget. Validation took 24 sec. Full windows are checked against the effort of their own events, so an overnight list from an earlier card cannot make the next window full. The agreed broader output changes were deliberately separate from this performance comparison: retain all taxa without automatic rollup, preserve unresolved records with statuses, export the events not assigned to full candidates as independent ad-hoc units, and serialize observer IDs with a pipe consistently across both sources. The current exporter still uses its previous candidate-only, eligible-reference name-matching policy and JSON observer arrays. The cache preserves the source information needed to implement the new policy without changing batching or reparsing discarded observations.

Taxon matching must change along with removing the species filter. For example, `Ploceus cucullatus` appears in this release with five different concept IDs, spanning species and subspecific concepts. Its `SCIENTIFIC NAME` is the same parent name, while `SUBSPECIES SCIENTIFIC NAME` identifies the source detail. Matching every taxon solely on the parent name would lose that distinction or multiply rows. For the declared matching taxonomy, match original concept IDs deliberately, keep source and matched IDs separate, retain `report_as` for optional later aggregation, and flag unresolved or conflicting evidence. Do not infer cross-version equivalence.

An additional full-cache check found all 3,932 distinct source concept IDs in the local 2025 reference, covering all 35,533,400 rows. This supports a concept-based implementation for this matching reference; it does not establish cross-version equivalence or justify dropping the source identifiers.

Ad-hoc assignment should complement the full-candidate assignments by `EVENT ID`, with exactly one unit per remaining independent event. Keep all shared members and source associations. The existing complete-observer partitions remain valid; the observation-scale joins and outputs must stay on disk because the future source-association table can approach all 35.5 million rows. Do not collect those records into pandas at the end: the CLI already uses `return_tables=False`.
