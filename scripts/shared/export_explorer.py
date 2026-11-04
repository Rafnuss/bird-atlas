"""Write the Parquet tables that bird-atlas-explorer opens, from a prepared eBird package."""
import argparse
from pathlib import Path

from bird_atlas.explorer_export import export_explorer, zip_explorer

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('package', type=Path, help='prepared package folder containing datapackage.json')
parser.add_argument('--output', type=Path, help='output folder (default: <package>/explorer)')
parser.add_argument('--zip', action='store_true', help='also write <package>-explorer.zip next to the package, for sharing')
parser.add_argument('--memory-limit', default='4GB', help='DuckDB memory limit (default 4GB); larger joins spill to disk')
parser.add_argument('--threads', type=int, help='DuckDB threads (default: all cores)')
args = parser.parse_args()

index = export_explorer(args.package, args.output, memory_limit=args.memory_limit, threads=args.threads)
out = args.output or args.package / 'explorer'
size = sum(path.stat().st_size for path in out.iterdir())
print(f"Wrote {out} ({size / 1e6:.0f} MB, {index['counts']['cards']:,} cards) in {index['seconds']} s")
if args.zip:
    archive = zip_explorer(out, args.package.parent / f'{args.package.name}-explorer.zip')
    print(f'Wrote {archive} ({archive.stat().st_size / 1e6:.0f} MB)')
