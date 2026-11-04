"""Latest record per eBird reporting species and level-1 QDGC, from ABAP and EBD.

See docs/kenyabirdtrend-pipeline.md. Run from the repository root, e.g.

uv run python scripts/shared/latest_by_qdgc.py \
    --abap data/prepared/abap/downloaded/kenya_2007-07-23_2026-11-03_reporting_parents \
    --ebd data/raw/ebird/ebd_AFR_relSep-2026/ebd_AFR_relSep-2026.txt.gz \
    --taxonomy data/taxonomy/ebird_2026.csv \
    --overrides data/taxonomy/regional_overrides_ebird_v2026.csv \
    --abap-exclude data/curated/abap_kenya_flagged_spp.csv \
    --observers data/curated/observer_crosswalk.csv \
    --output data/prepared/latest_by_qdgc/kenya_2026-11
"""
import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from bird_atlas.card_schema import parse_observers
from bird_atlas.grid import cell2latlng, latlng2qdgc, pentad2qdgc

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--abap', required=True, type=Path, help='prepared ABAP package (eBird 2026)')
parser.add_argument('--ebd', required=True, type=Path, help='raw EBD observation file (.txt.gz)')
parser.add_argument('--taxonomy', required=True, type=Path, help='full eBird reference, e.g. ebird_2026.csv')
parser.add_argument('--overrides', required=True, type=Path, help='regional overrides for retired concepts')
parser.add_argument('--abap-exclude', type=Path, help='curated ABAP Spp codes to exclude and audit, '
                    'e.g. data/curated/abap_kenya_flagged_spp.csv')
parser.add_argument('--observers', type=Path, help='curated names/person links, e.g. data/curated/observer_crosswalk.csv')
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--country', default='KE', help='EBD COUNTRY CODE')
parser.add_argument('--regions', default='kenya,africa', help='override regions applied, comma separated')
parser.add_argument('--level', default=1, type=int, choices=[0, 1, 2], help='QDGC level (1 = Kenya QSD)')
parser.add_argument('--historical-before', default='2009-01-01', help='hist_* columns: last record before this date')
parser.add_argument('--found-from', default='2024-01-01', help='found_date and finders.csv: first record from this date')
parser.add_argument('--precise-km', default=10.0, type=float,
                    help='eBird checklists longer than this place no hist_* or found record (they still count as mid)')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=False)
started = time.time()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(1 << 24):
            digest.update(chunk)
    return digest.hexdigest()


# 1. Reporting-species lookup ------------------------------------------------
# report_as holds a species code; follow it until blank/self, then keep species.
taxonomy = pd.read_csv(args.taxonomy, dtype=str, keep_default_na=False)
by_code = taxonomy.set_index('species_code')
concept_of_code = by_code.bio_concept_code
report_code = by_code.report_as.where(by_code.report_as != '', by_code.index)
final_code = report_code.copy()
for _ in range(5):
    final_code = report_code.reindex(final_code.values).set_axis(final_code.index)
assert final_code.eq(report_code.reindex(final_code.values).values).all(), 'report_as chain did not terminate'
reporting = pd.Series(concept_of_code.reindex(final_code.values).values, index=concept_of_code.values)
species_ids = set(taxonomy.loc[taxonomy.category == 'species', 'bio_concept_code'])
counts = {}


def to_species(concepts, source):
    """Map eBird 2026 concepts to their reporting species concept, or NA."""
    resolved = concepts.map(reporting)
    counts[source]['not_in_taxonomy'] = int(resolved.isna().sum())
    resolved = resolved.where(resolved.isin(species_ids))
    counts[source]['not_species'] = int(resolved.isna().sum()) - counts[source]['not_in_taxonomy']
    return resolved


# 2. ABAP evidence: card start date, pentad -> QDGC ----------------------------
abap_manifest = json.loads((args.abap / 'manifest.json').read_text())
assert abap_manifest['ebird_taxonomy_version'] == '2026' and abap_manifest['taxonomy_sha256'] == sha256(args.taxonomy), \
    'ABAP package was prepared against a different eBird reference'
cards = pd.read_csv(args.abap / 'cards.csv', dtype=str, keep_default_na=False)
records = pd.read_csv(args.abap / 'records.csv.gz', dtype=str, keep_default_na=False,
                      usecols=['source', 'card_id', 'ebird_avibase_id', 'source_spp'])
abap = records.merge(cards[['source', 'card_id', 'pentad_id', 'start_date', 'observer_id', 'observer_name']],
                     on=['source', 'card_id'], how='left', validate='many_to_one')
assert abap.start_date.ne('').all() and abap.pentad_id.ne('').all()
counts['abap'] = {'records': len(abap)}
# Flagged source codes (out of range in Kenya) are excluded, not remapped, and
# listed with their counts for the Kenya Bird Map vetting team.
if args.abap_exclude:
    flagged = pd.read_csv(args.abap_exclude, dtype=str, keep_default_na=False)
    hit = abap[abap.source_spp.isin(flagged.Spp)]
    audit = hit.groupby('source_spp').agg(
        records=('card_id', 'size'), cards=('card_id', 'nunique'), pentads=('pentad_id', 'nunique'),
        observers=('observer_id', 'nunique'), first_date=('start_date', 'min'), last_date=('start_date', 'max'))
    # Counts are recomputed here; a stored snapshot in the curated file is not reused.
    audit = flagged.drop(columns=[c for c in audit.columns if c in flagged]) \
        .merge(audit, left_on='Spp', right_index=True, how='left').fillna({'records': 0})
    audit.sort_values('records', ascending=False).to_csv(args.output / 'abap_flagged.csv', index=False)
    abap = abap[~abap.source_spp.isin(flagged.Spp)]
    counts['abap']['flagged_excluded'] = len(hit)
abap['species_id'] = to_species(abap.ebird_avibase_id, 'abap')
abap = abap.dropna(subset=['species_id'])
pentads = abap.pentad_id.unique()
abap['qdgc'] = abap.pentad_id.map(dict(zip(pentads, pentad2qdgc(pentads, level=args.level))))
# Cards are located by pentad: keep its ID and centre.
pentad_lat, pentad_lng = cell2latlng(list(pentads))
abap['lat'] = abap.pentad_id.map(dict(zip(pentads, pentad_lat)))
abap['lng'] = abap.pentad_id.map(dict(zip(pentads, pentad_lng)))
abap = abap.rename(columns={'pentad_id': 'pentad'})
# Ad hoc cards carry no names; reuse the name an observer ID has on full-protocol cards.
abap['observer_id'] = abap.observer_id.map(lambda value: '|'.join(parse_observers(value)))
names = cards[cards.observer_name != ''].assign(
    observer_id=lambda d: d.observer_id.map(lambda value: '|'.join(parse_observers(value))))
names = names.groupby('observer_id').observer_name.agg(lambda s: s.mode().iloc[0])
abap['observer_name'] = abap.observer_name.where(abap.observer_name != '', abap.observer_id.map(names)).fillna('')
abap = abap.rename(columns={'start_date': 'date', 'card_id': 'id'}).assign(source='abap', precise=True)
counts['abap']['kept'] = len(abap)

# 3. EBD evidence: approved Kenya observations, checklist coordinates -> QDGC --
con = duckdb.connect()
ebd = con.execute("""
    SELECT "TAXON CONCEPT ID" AS concept, "SCIENTIFIC NAME" AS scientific_name,
           "OBSERVATION DATE" AS date, "SAMPLING EVENT IDENTIFIER" AS id, "OBSERVER ID" AS observer_id,
           CAST("LATITUDE" AS DOUBLE) AS lat, CAST("LONGITUDE" AS DOUBLE) AS lng, "APPROVED" AS approved,
           TRY_CAST("EFFORT DISTANCE KM" AS DOUBLE) AS km
    FROM read_csv(?, delim='\t', header=true, all_varchar=true)
    WHERE "COUNTRY CODE" = ?""", [str(args.ebd), args.country]).df()
counts['ebd'] = {'rows': len(ebd), 'not_approved': int((ebd.approved != '1').sum())}
ebd = ebd[ebd.approved == '1'].drop(columns='approved')
counts['ebd']['records'] = len(ebd)
# The EBD can use an older reference. Retired concepts use reviewed overrides only.
overrides = pd.read_csv(args.overrides, dtype=str, keep_default_na=False)
override = overrides[overrides.region.isin(args.regions.split(','))].set_index('avibase_id').ebird_v2026_avibase_id
retired = ~ebd.concept.isin(taxonomy.bio_concept_code)
unmapped = ebd[retired & ~ebd.concept.isin(override.index)]
unmapped = unmapped.groupby('concept').scientific_name.agg(['first', 'size']).reset_index()
counts['ebd']['retired_overridden'] = int(retired.sum() - unmapped['size'].sum())
ebd.loc[retired, 'concept'] = ebd.loc[retired, 'concept'].map(override)
ebd['species_id'] = to_species(ebd.concept.fillna(''), 'ebd')
ebd = ebd.dropna(subset=['species_id'])
points = ebd[['lat', 'lng']].drop_duplicates()
points['qdgc'] = latlng2qdgc(points.lat.values, points.lng.values, level=args.level)
ebd = ebd.merge(points, on=['lat', 'lng'], how='left').assign(source='ebird', observer_name='', pentad='')
# A travelling checklist is located at its start: its birds may be in another cell.
ebd['precise'] = ~(ebd.km > args.precise_km)
counts['ebd']['imprecise_records'] = int((~ebd.precise).sum())
counts['ebd']['kept'] = len(ebd)

# 4. Latest record per (qdgc, species); eBird wins a same-day tie ------------
columns = ['qdgc', 'species_id', 'date', 'source', 'id', 'observer_id', 'observer_name', 'precise',
           'lat', 'lng', 'pentad']
evidence = pd.concat([abap[columns], ebd[columns]], ignore_index=True)
# Curated display names, and person keys linking one observer's ABAP and eBird IDs.
evidence['person'] = evidence.source + ':' + evidence.observer_id
if args.observers:
    crosswalk = pd.read_csv(args.observers, dtype=str, keep_default_na=False)
    key = crosswalk.source + ':' + crosswalk.observer_id
    names = pd.Series(crosswalk.display_name.str.strip().values, index=key)
    persons = pd.Series(crosswalk.get('person', pd.Series('', index=crosswalk.index)).values, index=key)
    evidence['observer_name'] = evidence.person.map(names[names != '']).fillna(evidence.observer_name)
    # A linked person shows under one name (the `person` value) whatever the source.
    linked = evidence.person.map(persons[persons != ''])
    evidence['observer_name'] = linked.fillna(evidence.observer_name)
    evidence['person'] = linked.fillna(evidence.person)
evidence = (evidence.assign(rank=evidence.source.map({'ebird': 0, 'abap': 1}))
            .sort_values(['date', 'rank', 'id'], ascending=[False, True, True])
            .drop(columns='rank'))
pair = ['qdgc', 'species_id']
latest = evidence.drop_duplicates(pair).sort_values(pair)

# 5. Periods: last historical record, last mid-period date, first find --------
# historical: date < --historical-before; mid: up to --found-from; found: after.
# hist and found place a species in a cell, so they need a precise location; any record
# (precise or not) shows presence in the mid period, which can only remove a target.
historical = evidence[evidence.precise & (evidence.date < args.historical_before)].drop_duplicates(pair)
historical = historical[pair + ['date', 'source', 'id']].rename(
    columns={'date': 'hist_date', 'source': 'hist_source', 'id': 'hist_id'})
mid = evidence[(evidence.date >= args.historical_before) & (evidence.date < args.found_from)]
mid = mid.drop_duplicates(pair)[pair + ['date']].rename(columns={'date': 'mid_date'})
found = evidence[evidence.precise & (evidence.date >= args.found_from)]
found_date = found.groupby(pair).date.min().rename('found_date').reset_index()
# Earliest record of any precision: "new" means no record at all before found_from.
first_date = evidence.groupby(pair).date.min().rename('first_date').reset_index()
latest = latest.merge(historical, on=pair, how='left').merge(mid, on=pair, how='left') \
    .merge(found_date, on=pair, how='left').merge(first_date, on=pair, how='left')
latest.drop(columns=['lat', 'lng', 'pentad']).to_csv(args.output / 'latest.csv', index=False)
# Everyone recording the pair on its first found date; one row per observer.
finders = (found.merge(found_date, on=pair)
           .query('date == found_date')
           .sort_values(pair + ['person', 'source', 'id'])
           .drop_duplicates(pair + ['person'])
           [pair + ['date', 'source', 'id', 'observer_id', 'observer_name', 'person', 'lat', 'lng', 'pentad']])
finders.to_csv(args.output / 'finders.csv', index=False, float_format='%.5f')

species = (taxonomy[taxonomy.bio_concept_code.isin(latest.species_id)]
           .rename(columns={'bio_concept_code': 'species_id', 'primary_com_name': 'common_name',
                            'sci_name': 'scientific_name'})
           [['species_id', 'species_code', 'common_name', 'scientific_name', 'taxon_order']])
species.to_csv(args.output / 'species.csv', index=False)

# 5. Manifest ------------------------------------------------------------------
manifest = {
    'created_at': datetime.now(timezone.utc).isoformat(),
    'grain': 'one row per (qdgc, reporting species): latest record plus period summaries',
    'qdgc_level': args.level,
    'taxonomy': {'path': str(args.taxonomy), 'sha256': sha256(args.taxonomy), 'version': '2026'},
    'inputs': {
        'abap': {'path': str(args.abap), 'manifest_sha256': sha256(args.abap / 'manifest.json'),
                 'coverage': [abap_manifest['source_manifest']['start_date'],
                              abap_manifest['source_manifest']['end_date']]},
        'ebd': {'path': str(args.ebd), 'sha256': sha256(args.ebd), 'country': args.country},
        'overrides': {'path': str(args.overrides), 'sha256': sha256(args.overrides), 'regions': args.regions},
        'abap_exclude': {'path': str(args.abap_exclude), 'sha256': sha256(args.abap_exclude)}
        if args.abap_exclude else None,
        'observers': {'path': str(args.observers), 'sha256': sha256(args.observers)} if args.observers else None,
    },
    'script_sha256': sha256(__file__),
    'policies': {
        'taxonomy': 'report_as resolved through the full reference; only category == species kept; '
                    'all ABAP mapping statuses kept',
        'abap_date': 'card start date',
        'ebd_quality': 'APPROVED == 1; no exotic or review filter',
        'ebd_retired_concepts': 'regional override when listed, otherwise dropped',
        'tie_break': 'latest date, then eBird before ABAP, then smallest id',
        'historical_before': args.historical_before,
        'found_from': args.found_from,
        'precise_km': f'eBird checklists over {args.precise_km} km count for mid only, not for hist or found',
        'finders': 'every person (observer, or linked ABAP+eBird IDs) with a record of the pair on its first date on or after found_from; lat/lng is the eBird checklist start or the KBM pentad centre (pentad)',
    },
    'counts': counts,
    'ebd_unmapped_retired_concepts': unmapped.rename(
        columns={'first': 'scientific_name', 'size': 'rows'}).to_dict('records'),
    'date_range': {source: [group.date.min(), group.date.max()] for source, group in evidence.groupby('source')},
    'output': {'pairs': len(latest), 'species': len(species), 'cells': int(latest.qdgc.nunique()),
               'pairs_by_source': latest.source.value_counts().to_dict(),
               'pairs_historical': int(latest.hist_date.notna().sum()), 'pairs_mid': int(latest.mid_date.notna().sum()),
               'pairs_found': int(latest.found_date.notna().sum()), 'finder_rows': len(finders)},
    'seconds': round(time.time() - started),
}
(args.output / 'manifest.json').write_text(json.dumps(manifest, indent=1, default=int))
print(json.dumps(manifest['counts'] | {'output': manifest['output']}, indent=1, default=int))
