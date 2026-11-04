"""Combine prepared tables using the same exact eBird reference and retain row grain."""
import argparse
import hashlib
import json
from pathlib import Path
import pandas as pd
from bird_atlas.datapackage import write_datapackage

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('inputs', nargs='+', type=Path)
parser.add_argument('--output', required=True, type=Path)
args = parser.parse_args()
versions, hashes, manifests = [], [], []
for path in args.inputs:
    manifest = json.loads((path / 'manifest.json').read_text())
    manifests.append(manifest)
    versions.append(manifest.get('ebird_taxonomy_version', manifest.get('target_ebird_version')))
    if 'taxonomy_sha256' in manifest:
        hashes.append(manifest['taxonomy_sha256'])
    else:
        candidates = [value for name, value in manifest['input_and_code_sha256'].items() if Path(name).name == 'ebird_2026.csv']
        hashes.append(candidates[0] if candidates else None)
if len(set(versions)) != 1 or not all(hashes) or len(set(hashes)) != 1:
    raise ValueError('Inputs require the same declared taxonomy version and reference hash; cross-version harmonization is not implemented.')
args.output.mkdir(parents=True, exist_ok=False)
for table in ['cards', 'records', 'species']:
    filename = table + ('.csv.gz' if table == 'records' else '.csv')
    frames = [pd.read_csv(path / filename, dtype=str, keep_default_na=False) for path in args.inputs]
    result = pd.concat(frames, ignore_index=True)
    if table == 'species':
        columns = ['scientific_name', 'common_name', 'category', 'ebird_species_code', 'ebird_taxon_order', 'report_as']
        conflicts = result.groupby('ebird_avibase_id')[columns].nunique(dropna=False).gt(1).any(axis=1)
        if conflicts.any():
            raise ValueError(f'Conflicting eBird taxon fields: {conflicts[conflicts].index.tolist()}')
        result = result.drop(columns=['parent_spp', 'parent_avibase_id'], errors='ignore').drop_duplicates('ebird_avibase_id')
        if missing := set(result.report_as) - {''} - set(result.ebird_avibase_id):
            raise ValueError(f'Reporting taxa missing from input species tables: {sorted(missing)}')
    elif result.duplicated(['source', 'card_id' if table == 'cards' else 'record_id']).any():
        raise ValueError(f'Duplicate {table} keys within a source; choose distinct inputs or namespace them first.')
    result.to_csv(args.output / filename, index=False)
checklist_paths = [path / 'checklist.csv' for path in args.inputs if (path / 'checklist.csv').exists()]
if checklist_paths:
    checklists = pd.concat([pd.read_csv(path, dtype=str, keep_default_na=False) for path in checklist_paths], ignore_index=True)
    if checklists.duplicated(['checklist_id', 'card_id']).any():
        raise ValueError('Duplicate checklist keys; choose distinct inputs or namespace them first.')
    checklists.to_csv(args.output / 'checklist.csv', index=False)
(args.output / 'manifest.json').write_text(json.dumps({'source': 'combined', 'ebird_taxonomy_version': versions[0], 'taxonomy_sha256': hashes[0], 'record_grain': 'mixed; see source manifests', 'inputs': [str(path.resolve()) for path in args.inputs], 'input_manifest_sha256': [hashlib.sha256((path / 'manifest.json').read_bytes()).hexdigest() for path in args.inputs], 'source_record_grains': [m['record_grain'] for m in manifests]}, indent=2) + '\n')

write_datapackage(args.output)
