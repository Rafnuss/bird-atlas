"""Export ABAP detections as linked species, pentad, card and record tables."""

import argparse
import csv
import gzip
import hashlib
from datetime import datetime, timezone
from decimal import Decimal
import json
from collections import Counter
from pathlib import Path

from bird_atlas.datapackage import write_datapackage
from bird_atlas.card_schema import CARD_COLUMNS
from bird_atlas.grid import pentad2latlng, pentad2polygon
from bird_atlas.taxonomy import reporting_taxa
from pyproj import Geod


ROOT = Path(__file__).resolve().parents[2]
DOWNLOADS = ROOT / 'data/raw/abap/downloaded'
CROSSWALK = ROOT / 'data/taxonomy/abap_avibase_crosswalk.csv'
OVERRIDES = ROOT / 'data/taxonomy/regional_overrides_ebird_v2026.csv'
TAXONOMY = ROOT / 'data/taxonomy/ebird_2026.csv'
EXPORTS = ROOT / 'data/prepared/abap/downloaded'
REGIONS = {
    'world': [],
    'africa': ['africa'],
    'east_africa': ['africa', 'east_africa'],
    'west_africa': ['africa', 'west_africa'],
    'southern_africa': ['africa', 'southern_africa'],
    'kenya': ['africa', 'east_africa', 'kenya'],
}

def normalize_time(value):
    for fmt in ('%H:%M:%S', '%H:%M', '%H%M'):
        try:
            return datetime.strptime(value, fmt).strftime('%H:%M:%S')
        except ValueError:
            pass
    return ''

if __name__ == '__main__':
    # Choose a completed download and destination ---------------------------
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, help='Completed download directory (default: most recently completed).')
    parser.add_argument('--output-dir', type=Path, help='Export directory (default: data/prepared/abap/downloaded/<snapshot>).')
    parser.add_argument('--region', choices=REGIONS, help='Taxonomic overrides, not a spatial filter (default: kenya for Kenya, africa for other countries).')
    parser.add_argument('--crosswalk', type=Path, default=CROSSWALK)
    parser.add_argument('--overrides', type=Path, default=OVERRIDES)
    parser.add_argument('--taxonomy', type=Path, default=TAXONOMY, help='eBird v2026 reference CSV.')
    args = parser.parse_args()
    CROSSWALK, OVERRIDES, TAXONOMY = args.crosswalk, args.overrides, args.taxonomy
    manifests = list(DOWNLOADS.glob('*/manifest.json'))
    snapshot = args.snapshot or max(manifests, key=lambda path: json.loads(path.read_text())['completed_at']).parent
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    request_path = snapshot / 'request.json'
    request = json.loads(request_path.read_text()) if request_path.exists() else {}
    country = manifest.get('country') or request.get('country') or snapshot.name.split('_')[0]
    if country[0].isdigit() and manifest.get('source') == 'Kenya Bird Map':
        country = 'kenya'
    if not country or country[0].isdigit():
        parser.error('Snapshot must identify its country in manifest.json, request.json, or the directory name.')
    default_region = 'kenya' if country == 'kenya' else 'africa'
    args.region = args.region or default_region
    if args.output_dir is None:
        suffix = '' if args.region == default_region else '_' + args.region
        args.output_dir = EXPORTS / f"{snapshot.name}{suffix}"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Read the crosswalk and eBird taxonomy --------------------------------
    with CROSSWALK.open(encoding='utf-8', newline='') as f:
        crosswalk_rows = list(csv.DictReader(f))
    crosswalk = {row['Spp']: row for row in crosswalk_rows}
    if len(crosswalk) != len(crosswalk_rows):
        raise ValueError('The curated crosswalk contains duplicate Spp codes.')
    with TAXONOMY.open(encoding='utf-8-sig', newline='') as f:
        taxonomy = {row['bio_concept_code']: row for row in csv.DictReader(f) if row['bio_concept_code']}
    taxonomy_codes = {row['species_code']: row['bio_concept_code'] for row in taxonomy.values()}
    with OVERRIDES.open(encoding='utf-8', newline='') as f:
        override_rows = list(csv.DictReader(f))
    overrides = {(row['region'], row['avibase_id']): row for row in override_rows}
    if len(overrides) != len(override_rows):
        raise ValueError('Regional overrides contain duplicate region and Avibase ID pairs.')
    invalid = {row['ebird_v2026_avibase_id'] for row in crosswalk_rows + override_rows if row['ebird_v2026_avibase_id'] and row['ebird_v2026_avibase_id'] not in taxonomy}
    invalid.update(row['avibase_id'] for row in crosswalk_rows if row['ebird_v2026_status'] == 'exact' and row['avibase_id'] not in taxonomy)
    if invalid:
        raise ValueError(f'Crosswalk targets absent from eBird v2026: {sorted(invalid)}')
    if any(row['region'] not in REGIONS or row['region'] == 'world' or not row['avibase_id'] or not row['ebird_v2026_avibase_id'] for row in override_rows):
        raise ValueError('Regional overrides must have a known region, source Avibase ID and eBird v2026 target.')

    # Resolve worldwide and regional mappings for each source taxon ---------
    resolved = {}
    for code, mapping in crosswalk.items():
        target = mapping['avibase_id'] if mapping['ebird_v2026_status'] == 'exact' else mapping['ebird_v2026_avibase_id']
        regional = {}
        for region in REGIONS[args.region]:
            regional = overrides.get((region, mapping['avibase_id']), regional)
        resolved[code] = (regional.get('ebird_v2026_avibase_id', target), regional)

    # Inspect checklist identities before resolving overlapping API sources --
    card_fields = ['StartDate', 'EndDate', 'StartTime', 'ObserverNo', 'ObserverName', 'TotalHours', 'Hour1', 'Hour2', 'Hour3', 'Hour4', 'Hour5', 'Hour6', 'Hour7', 'Hour8', 'Hour9', 'Hour10', 'TotalSpp', 'InclNight', 'AllHabitats']
    cards, pentads, species = {}, set(), {}
    card_numbers = {}
    with (snapshot / 'records.csv').open(encoding='utf-8', newline='') as source:
        for row in csv.DictReader(source):
            card_id = row['Protocol'] + ':' + row['CardNo']
            card = {'card_id': card_id, 'protocol': row['Protocol'], 'pentad_id': row['Pentad'], **{key: row[key] for key in card_fields}}
            if card_id in cards:
                for key, value in card.items():
                    if cards[card_id][key] and value and cards[card_id][key] != value:
                        raise ValueError(f'Conflicting {key} for card {card_id}.')
                    if not cards[card_id][key]:
                        cards[card_id][key] = value
            else:
                cards[card_id] = card
                card_numbers.setdefault(row['CardNo'], []).append(card_id)
    overlaps = {number: ids for number, ids in card_numbers.items() if len(ids) > 1}
    detections = {card_id: Counter() for ids in overlaps.values() for card_id in ids}
    with (snapshot / 'records.csv').open(encoding='utf-8', newline='') as source:
        for row in csv.DictReader(source):
            card_id = row['Protocol'] + ':' + row['CardNo']
            if card_id in detections:
                detections[card_id][row['Spp']] += 1
    merged, card_audit = {}, []
    for number in sorted(overlaps):
        full_id, adhoc_id = 'full_protocol:' + number, 'adhoc:' + number
        full, adhoc = cards[full_id], cards[adhoc_id]
        differences = [key for key in ['pentad_id'] + card_fields if full[key] and adhoc[key] and full[key] != adhoc[key]]
        removed = sum((detections[full_id] & detections[adhoc_id]).values())
        merged[adhoc_id] = full_id
        for key in ['pentad_id'] + card_fields:
            if not full[key]:
                full[key] = adhoc[key]
        del cards[adhoc_id]
        card_audit.append({'card_no': number, 'duplicates_removed': removed, 'conflicts': differences})
    # Merge shared card numbers, deduplicating source Spp regardless of sequence --
    duplicate_budget = {card_id: detections[card_id] & detections[target] for card_id, target in merged.items()}
    species_counts = Counter()
    counts = Counter()
    temporary = args.output_dir / 'records.csv.gz.tmp'
    with (snapshot / 'records.csv').open(encoding='utf-8', newline='') as source, gzip.open(temporary, 'wt', encoding='utf-8', newline='', compresslevel=5) as destination:
        writer = csv.DictWriter(destination, fieldnames=['record_id', 'source', 'card_id', 'ebird_avibase_id', 'avibase_id', 'source_spp', 'taxonomy_status', 'sequence'])
        writer.writeheader()
        for record_id, row in enumerate(csv.DictReader(source), 1):
            card_id = row['Protocol'] + ':' + row['CardNo']
            counts[row['Protocol']] += 1
            if card_id in merged:
                detection = row['Spp']
                if duplicate_budget[card_id][detection]:
                    duplicate_budget[card_id][detection] -= 1
                    continue
                card_id = merged[card_id]
            pentads.add(row['Pentad'])
            species.setdefault(row['Spp'], row)
            target, regional = resolved.get(row['Spp'], ('', {}))
            species_counts[row['Spp']] += 1
            writer.writerow({'source': 'abap', 'record_id': record_id, 'card_id': card_id, 'ebird_avibase_id': target, 'avibase_id': crosswalk.get(row['Spp'], {}).get('avibase_id', ''), 'source_spp': row['Spp'], 'taxonomy_status': ('regional_' + regional.get('status', 'override')) if regional else crosswalk.get(row['Spp'], {}).get('ebird_v2026_status', 'missing_crosswalk'), 'sequence': row['Sequence']})

    if sum(counts.values()) != manifest['records'] or any(counts[protocol] != expected for protocol, expected in manifest['records_by_protocol'].items()):
        temporary.unlink()
        raise ValueError('Source record counts do not match the completed download manifest.')

    # Write each dimension once using the keys referenced by records -------
    optional_fields = ['protocol', 'observer_name', 'unparsed_start_time', 'reported_species_count', 'includes_night', 'all_habitats'] + [f'hour_{i}' for i in range(1, 11)]
    with (args.output_dir / 'cards.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CARD_COLUMNS + optional_fields)
        writer.writeheader()
        for key in sorted(cards):
            card = cards[key]
            start_time = normalize_time(card['StartTime'])
            writer.writerow({'source': 'abap', 'card_id': card['card_id'], 'pentad_id': card['pentad_id'], 'observer_id': card['ObserverNo'] or '', 'start_date': card['StartDate'], 'end_date': card['EndDate'], 'start_time': start_time, 'duration_minutes': str(Decimal(card['TotalHours']) * 60) if card['TotalHours'] else '', 'protocol': card['protocol'], 'observer_name': card['ObserverName'], 'unparsed_start_time': '' if start_time else card['StartTime'], 'reported_species_count': card['TotalSpp'], 'includes_night': card['InclNight'], 'all_habitats': card['AllHabitats'], **{f'hour_{i}': card[f'Hour{i}'] for i in range(1, 11)}})
    pentad_ids = sorted(pentads)
    normalized_ids = [pentad.lower() for pentad in pentad_ids]
    latitudes, longitudes = pentad2latlng(normalized_ids)
    polygons = pentad2polygon(normalized_ids)
    geod = Geod(ellps='WGS84')
    with (args.output_dir / 'pentads.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['pentad_id', 'latitude', 'longitude', 'west', 'south', 'east', 'north', 'area_km2'])
        writer.writeheader()
        for pentad_id, latitude, longitude, polygon in zip(pentad_ids, latitudes, longitudes, polygons):
            west, south, east, north = polygon.bounds
            area, _ = geod.geometry_area_perimeter(polygon)
            writer.writerow({'pentad_id': pentad_id, 'latitude': latitude, 'longitude': longitude, 'west': west, 'south': south, 'east': east, 'north': north, 'area_km2': abs(area) / 1e6})
    # Group source codes by their resolved eBird concept -------------------
    groups = {}
    for code in species:
        target, regional = resolved.get(code, ('', {}))
        if target:
            group = groups.setdefault(target, {'spp': set(), 'avibase': set()})
            group['spp'].add(code)
            if crosswalk.get(code, {}).get('avibase_id'):
                group['avibase'].add(crosswalk[code]['avibase_id'])
    included = reporting_taxa((taxonomy[target]['species_code'] for target in groups), {row['species_code']: row['report_as'] for row in taxonomy.values()})
    for code in included:
        groups.setdefault(taxonomy_codes[code], {'spp': set(), 'avibase': set()})
    with (args.output_dir / 'species.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['ebird_avibase_id', 'scientific_name', 'common_name', 'category', 'ebird_species_code', 'ebird_taxon_order', 'report_as', 'parent_spp', 'parent_avibase_id'])
        writer.writeheader()
        for target, group in sorted(groups.items()):
            taxon = taxonomy[target]
            writer.writerow({'ebird_avibase_id': target, 'scientific_name': taxon['sci_name'], 'common_name': taxon['primary_com_name'], 'category': taxon['category'], 'ebird_species_code': taxon['species_code'], 'ebird_taxon_order': taxon['taxon_order'], 'report_as': taxonomy_codes[taxon['report_as']] if taxon['report_as'] and taxon['report_as'] != taxon['species_code'] else '', 'parent_spp': json.dumps(sorted(group['spp'], key=lambda code: (not code.isdigit(), int(code) if code.isdigit() else code))), 'parent_avibase_id': json.dumps(sorted(group['avibase']))})

    # Keep mapping uncertainties and their evidence available for review ---
    audit = args.output_dir / 'audit.csv'
    with audit.open('w', encoding='utf-8', newline='') as f:
        fields = ['section', 'id', 'name', 'count', 'issue', 'details']
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for card in card_audit:
            writer.writerow({'section': 'card', 'id': card['card_no'], 'count': card['duplicates_removed'], 'issue': json.dumps(['merged_protocols']), 'details': json.dumps([f'Conflicting fields: {name}' for name in card['conflicts']])})
        for code in sorted(species, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value)):
            mapping = crosswalk.get(code, {})
            target, regional = resolved.get(code, ('', {}))
            issues = []
            if not mapping: issues.append('missing_crosswalk')
            elif mapping['avibase_status'] in ('provisional', 'provisional_complex', 'provisional_broader', 'unresolved'): issues.append('avibase_' + mapping['avibase_status'])
            if mapping and mapping['ebird_v2026_status'] != 'exact': issues.append('ebird_' + mapping['ebird_v2026_status'])
            if regional: issues.append('regional_' + regional['status'])
            if not target: issues.append('unresolved_target')
            if issues:
                details = [f"Source Avibase: {mapping.get('avibase_id', '')}", f"Resolved: {target} {taxonomy.get(target, {}).get('sci_name', '')}"]
                if regional: details.append(f"Override: {regional['region']} ({regional['status']})")
                details.extend(x for x in [mapping.get('note', ''), regional.get('note', '')] if x)
                writer.writerow({'section': 'taxonomy', 'id': code, 'name': mapping.get('abap_name', ''), 'count': species_counts[code], 'issue': json.dumps(issues), 'details': json.dumps(details)})
    temporary.replace(args.output_dir / 'records.csv.gz')
    print(f'Snapshot: {snapshot}')
    print(f'Taxonomic region: {args.region} (not a spatial filter)')
    print(f'Output: {args.output_dir}')
    print(f'Records: {sum(species_counts.values()):,}; cards: {len(cards):,}; pentads: {len(pentads):,}; source species codes: {len(species):,}; resolved taxa: {len(groups):,}; unresolved records: {sum(n for code, n in species_counts.items() if not resolved.get(code, ("", {}))[0]):,}')
    print('Source protocols before deduplication: ' + ', '.join(f'{key}: {value:,}' for key, value in sorted(counts.items())))
    print(f'Card overlaps: {len(overlaps)}; merged: {len(merged)}; duplicate detections removed: {sum(row["duplicates_removed"] for row in card_audit):,}')

    # Record mapping and source versions after successful export validation.
    inputs = [snapshot / 'manifest.json', snapshot / 'records.csv', CROSSWALK, OVERRIDES,
              TAXONOMY, Path(__file__)]
    hashes = {}
    for path in inputs:
        with path.open('rb') as stream:
            hashes[str(path.resolve())] = hashlib.file_digest(stream, 'sha256').hexdigest()
    prepared = {'schema_version': 1, 'source': 'abap', 'record_grain': 'source_detection', 'snapshot': str(snapshot.resolve()),
                'source_manifest': manifest, 'taxonomic_region': args.region,
                'target_ebird_version': '2026', 'ebird_taxonomy_version': '2026', 'taxonomy_sha256': hashes[str(TAXONOMY.resolve())], 
                'input_and_code_sha256': hashes,
                'completed_at': datetime.now(timezone.utc).isoformat()}
    (args.output_dir / 'manifest.json').write_text(json.dumps(prepared, indent=2) + '\n')
    write_datapackage(args.output_dir)
