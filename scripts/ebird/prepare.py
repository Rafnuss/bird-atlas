"""Prepare full and ad-hoc eBird card tables using an explicit eBird taxonomy CSV."""
import argparse
from pathlib import Path
from bird_atlas import export_ebird

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--input', required=True, type=Path)
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--taxonomy', required=True, type=Path)
parser.add_argument('--ebird-version', required=True)
parser.add_argument('--source-kind', choices=['ebd', 'project'])
parser.add_argument('--exclude-missing-distance', action='store_true')
parser.add_argument('--cache', type=Path, help='Reusable trimmed DuckDB cache; defaults to OUTPUT/ebd.duckdb')
parser.add_argument('--memory-limit', default='1GB', help='DuckDB memory budget; Python/batch memory is additional')
parser.add_argument('--threads', type=int, default=4)
parser.add_argument('--card-selection', choices=['optimized', 'walk'], default='optimized',
                    help='optimized: maximise full cards then add ad hoc; walk: start each card at the next visit (BirdLasser)')
parser.add_argument('--partitions', type=int, default=32, help='Complete-observer batches; increase to reduce pandas memory')
args = parser.parse_args()
export_ebird(args.input, args.output, taxonomy_file=args.taxonomy, ebird_version=args.ebird_version,
             source_kind=args.source_kind, keep_missing_distance=not args.exclude_missing_distance,
             cache_file=args.cache, memory_limit=args.memory_limit, threads=args.threads,
             partitions=args.partitions, card_selection=args.card_selection, return_tables=False)
