"""Prepare full and ad-hoc eBird cards with checklist-sized batches and disk-backed joins."""

import gzip
import hashlib
import json
from pathlib import Path
import time
import warnings

import duckdb
import numpy as np
import pandas as pd
from pyproj import Geod

from .ebd_cache import prepare_ebd_cache
from .ebird2card import ebd2chk, select_card_windows, valid_card2chk_card, chk_card2card_chk, _resolve_EBD_file
from .checklists import collapse_event_metadata
from .datapackage import write_datapackage
from .card_schema import CARD_COLUMNS, format_observers
from .grid import pentad2latlng, pentad2polygon
from .taxonomy import reporting_taxa


def _append_csv(frame, path):
    """Append bounded batches, including a header for empty exports."""
    header = not path.exists()
    opener = gzip.open if path.suffix == '.gz' else open
    options = {'compresslevel': 1} if path.suffix == '.gz' else {}
    with opener(path, 'at', encoding='utf-8', newline='', **options) as stream:
        frame.to_csv(stream, index=False, header=header)


def _candidate_tables(checklists, members, card_selection='optimized'):
    starts = select_card_windows(checklists, method=card_selection)
    assignments = valid_card2chk_card(checklists, starts)
    cards = chk_card2card_chk(assignments, starts)
    checklist_table = assignments[['EVENT ID', 'CARD', 'QUALIFIES EFFORT']].merge(members, on='EVENT ID', how='inner', validate='one_to_many')
    checklist_table = checklist_table.rename(columns={'CARD': 'card_id', 'EVENT ID': 'checklist_group_id', 'SAMPLING EVENT IDENTIFIER': 'checklist_id', 'OBSERVER ID': 'observer_id', 'OBSERVATION DATE': 'start_date', 'DURATION MINUTES': 'duration_minutes', 'EFFORT DISTANCE KM': 'distance_km', 'PROTOCOL TYPE': 'protocol', 'ALL SPECIES REPORTED': 'all_species_reported', 'LATITUDE': 'latitude', 'LONGITUDE': 'longitude', 'QUALIFIES EFFORT': 'card_effort_eligible'})
    checklist_table['checklist_group_id'] = checklist_table['checklist_group_id'].where(checklist_table['checklist_group_id'].ne(checklist_table['checklist_id']), '')
    checklist_table['start_time'] = checklist_table['OBSERVATION DATETIME'].dt.strftime('%H:%M:%S')
    checklist_table['start_date'] = checklist_table['start_date'].dt.strftime('%Y-%m-%d')
    checklist_table = checklist_table[['checklist_id', 'checklist_group_id', 'card_id', 'observer_id', 'start_date', 'start_time', 'duration_minutes', 'distance_km', 'protocol', 'all_species_reported', 'latitude', 'longitude', 'card_effort_eligible']]
    card_table = cards.rename(columns={'CARD': 'card_id', 'PENTAD': 'pentad_id', 'OBSERVATION DATE': 'start_date', 'DURATION MINUTES_sum': 'duration_minutes', 'SOURCE OBSERVER IDS': 'observer_ids', 'OBSERVATION DATETIME_min': 'first_observation_datetime', 'OBSERVATION DATETIME_max': 'last_activity_datetime'})
    card_table['start_date'] = card_table['start_date'].dt.strftime('%Y-%m-%d')
    card_table['end_date'] = card_table['last_activity_datetime'].dt.strftime('%Y-%m-%d')
    card_table['start_time'] = card_table['first_observation_datetime'].dt.strftime('%H:%M:%S')
    card_table['observer_id'] = card_table['observer_ids']
    card_table['protocol'] = card_table['PROTOCOL']
    card_table.insert(0, 'source', 'ebird')
    card_table = card_table[CARD_COLUMNS + ['protocol']]
    return card_table, checklist_table, assignments[['EVENT ID', 'CARD']]


def export_ebird(input_file, output_dir, *, taxonomy_file, ebird_version, source_kind=None,
                 keep_missing_distance=True, cache_file=None, memory_limit='1GB', threads=4,
                 partitions=32, card_selection='optimized', return_tables=True):
    """Export full releases without loading observation-scale tables into pandas.

    The taxonomy-independent cache can be reused across policy/reference runs.
    Batches contain complete histories of representative observers. DuckDB's
    memory limit applies to its operators, not the entire Python process.
    Use return_tables=False for large runs; outputs remain inspectable CSVs.

    Every accepted event joins exactly one card: 'full' (at least two hours of
    qualifying effort in five days) or 'adhoc'. card_selection is 'optimized'
    (maximise full cards, then ad hoc) or 'walk' (start each card at the next
    visit, as BirdLasser does). Observations of every taxon are kept: each source
    concept is matched to the declared reference by concept ID, else by
    scientific name; anything unmatched is listed in excluded_observations.
    """
    started = time.perf_counter()
    input_file = _resolve_EBD_file(input_file)
    taxonomy_file, output_dir = Path(taxonomy_file), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if (output_dir / 'cards.csv').exists() or (output_dir / '.work.duckdb').exists():
        raise ValueError('Choose a new output directory for each preparation run.')
    (output_dir / 'audit').mkdir(exist_ok=True)
    cache_file = Path(cache_file) if cache_file else output_dir / 'ebd.duckdb'
    cache = prepare_ebd_cache(input_file, cache_file, memory_limit=memory_limit, threads=threads)
    source_kind = source_kind or cache['source_kind']
    reference = pd.read_csv(taxonomy_file, encoding='utf-8-sig', dtype=str)
    reference.columns = reference.columns.str.lower()
    reference = reference.rename(columns={'taxon_concept_id': 'bio_concept_code'})
    reference['report_as'] = reference['report_as'].fillna('')
    work = output_dir / '.work.duckdb'
    card_tables = []
    with duckdb.connect(str(work), config={'memory_limit': memory_limit, 'threads': threads}) as con:
        con.execute('SET preserve_insertion_order = false')
        cache_path = str(cache_file.resolve()).replace("'", "''")
        con.execute(f"ATTACH '{cache_path}' AS input (READ_ONLY)")
        con.register('taxonomy_frame', reference[['sci_name', 'bio_concept_code']])
        con.execute('CREATE TABLE taxonomy AS SELECT * FROM taxonomy_frame')
        # One match per source (concept, name): the concept ID if the reference has it, else a unique scientific name.
        con.execute('''CREATE TABLE taxon_match AS
            WITH source AS (SELECT DISTINCT coalesce("TAXON CONCEPT ID", '') AS concept, coalesce("SCIENTIFIC NAME", '') AS name FROM input.observations),
            names AS (SELECT sci_name, min(bio_concept_code) AS code FROM taxonomy GROUP BY sci_name HAVING count(*) = 1)
            SELECT s.concept, s.name,
                coalesce(c.bio_concept_code, n.code) AS ebird_avibase_id,
                CASE WHEN c.bio_concept_code IS NOT NULL THEN 'concept_id_match'
                     WHEN n.code IS NOT NULL THEN 'scientific_name_match' END AS taxonomy_status
            FROM source s LEFT JOIN taxonomy c ON c.bio_concept_code = s.concept
            LEFT JOIN names n ON n.sci_name = s.name''')
        # Partition by the representative observer, never by row/date/taxon.
        con.execute(f'''CREATE TABLE batches AS
            SELECT r."EVENT ID", hash(min(m."OBSERVER ID")) % {int(partitions)} AS batch
            FROM input.representatives r JOIN input.members m
              ON r."EVENT ID" = m."EVENT ID" AND r.representative_checklist_id = m."SAMPLING EVENT IDENTIFIER"
            GROUP BY r."EVENT ID"''')
        for batch in range(partitions):
            batch_started = time.perf_counter()
            members = con.execute('''SELECT m.* FROM input.members m JOIN batches b USING ("EVENT ID")
                WHERE b.batch = ? ORDER BY m."SAMPLING EVENT IDENTIFIER", m."OBSERVER ID",
                    m."OBSERVATION DATE", m."OBSERVATION DATETIME", m."LATITUDE", m."LONGITUDE",
                    m."PROTOCOL TYPE", m."DURATION MINUTES", m."EFFORT DISTANCE KM", m."ALL SPECIES REPORTED"''', [batch]).df()
            if members.empty:
                continue
            members['OBSERVATION DATE'] = pd.to_datetime(members['OBSERVATION DATE'])
            events = collapse_event_metadata(members)
            checklists = ebd2chk(events, source_kind=source_kind, keep_missing_distance=keep_missing_distance)
            # A few source checklists carry inconsistent metadata rows (e.g. two start times). The card
            # table keeps the first (earliest) row per checklist; the members audit keeps every variant.
            unique_members = members.drop_duplicates('SAMPLING EVENT IDENTIFIER')
            card_table, checklist_table, assignments = _candidate_tables(checklists, unique_members, card_selection)
            card_tables.append(card_table)
            _append_csv(card_table.assign(observer_id=card_table['observer_id'].map(format_observers)), output_dir / 'cards.csv')
            _append_csv(checklist_table, output_dir / 'checklist.csv')
            con.register('assignment_frame', assignments)
            con.execute('CREATE TABLE IF NOT EXISTS assignments AS SELECT * FROM assignment_frame WHERE false')
            con.execute('INSERT INTO assignments SELECT * FROM assignment_frame')
            for column in ['SOURCE CHECKLIST IDS', 'SOURCE OBSERVER IDS']:
                checklists[column] = checklists[column].map(json.dumps)
            # Every event either joins a card or records why it could not.
            card_of = assignments.drop_duplicates('EVENT ID').set_index('EVENT ID')['CARD']
            checklists['CARD'] = checklists['EVENT ID'].map(card_of).fillna('')
            checklists['UNASSIGNED REASON'] = np.where(checklists['CARD'].ne(''), '',
                np.where(checklists['KEEP PENTAD'], 'unassigned', 'spatial_screen_' + checklists['SPATIAL STATUS'].astype(str)))
            con.register('quality_frame', checklists)
            con.execute('CREATE TABLE IF NOT EXISTS quality AS SELECT * FROM quality_frame WHERE false')
            con.execute('INSERT INTO quality SELECT * FROM quality_frame')
            members = members.rename(columns={'EVENT ID': 'checklist_group_id', 'SAMPLING EVENT IDENTIFIER': 'checklist_id'})
            members['checklist_group_id'] = members['checklist_group_id'].where(members['checklist_group_id'].ne(members['checklist_id']), '')
            members = members[['checklist_id', 'checklist_group_id'] + [column for column in members if column not in ['checklist_id', 'checklist_group_id']]]
            _append_csv(members, output_dir / 'audit/checklist_members.csv.gz')
            quality = checklists.rename(columns={'EVENT ID': 'checklist_group_id', 'SAMPLING EVENT IDENTIFIER': 'representative_checklist_id', 'SOURCE CHECKLIST IDS': 'checklist_ids', 'SOURCE OBSERVER IDS': 'observer_ids'})
            quality['checklist_group_id'] = quality['checklist_group_id'].where(quality['checklist_group_id'].ne(quality['representative_checklist_id']), '')
            quality = quality[['representative_checklist_id', 'checklist_group_id'] + [column for column in quality if column not in ['representative_checklist_id', 'checklist_group_id']]]
            _append_csv(quality, output_dir / 'audit/checklist_quality.csv.gz')
            print(f'Batch {batch + 1}/{partitions}: {len(events):,} events, {len(card_table):,} cards ({time.perf_counter() - batch_started:.1f}s)', flush=True)

        print('Collapsing event taxa on disk ...', flush=True)
        con.execute('''CREATE TABLE canonical AS SELECT source_row FROM input.observations
            QUALIFY row_number() OVER (PARTITION BY "EVENT ID", "TAXON CONCEPT ID"
                ORDER BY "SAMPLING EVENT IDENTIFIER", source_row) = 1''')
        print('Writing presences and original-observation provenance ...', flush=True)
        # Bounded joins: every card belongs to one observer batch, so per-batch DISTINCT is exact.
        con.execute('CREATE TABLE presences (card_id VARCHAR, ebird_avibase_id VARCHAR)')
        batch_observations = '''FROM input.observations o JOIN batches b USING ("EVENT ID")
            JOIN assignments a USING ("EVENT ID")
            JOIN taxon_match t ON t.concept = coalesce(o."TAXON CONCEPT ID", '') AND t.name = coalesce(o."SCIENTIFIC NAME", '')
            WHERE b.batch = ? AND t.ebird_avibase_id IS NOT NULL'''
        for batch in range(partitions):
            con.execute(f'INSERT INTO presences SELECT DISTINCT a."CARD", t.ebird_avibase_id {batch_observations}', [batch])
        # A matched taxon is a name match if any source concept reached it by scientific name.
        con.execute('''CREATE TABLE match_status AS SELECT ebird_avibase_id,
            CASE WHEN bool_and(taxonomy_status = 'concept_id_match') THEN 'concept_id_match' ELSE 'scientific_name_match' END AS taxonomy_status
            FROM taxon_match WHERE ebird_avibase_id IS NOT NULL GROUP BY 1''')
        con.execute('''CREATE TABLE records AS SELECT row_number() OVER (ORDER BY card_id, ebird_avibase_id) AS record_id,
            'ebird' AS source, card_id, ebird_avibase_id, '' AS avibase_id, '' AS source_spp,
            taxonomy_status, '' AS sequence FROM presences JOIN match_status USING (ebird_avibase_id)''')
        con.execute("COPY (SELECT * FROM records ORDER BY record_id) TO ? (HEADER, COMPRESSION GZIP)", [str(output_dir / 'records.csv.gz')])
        con.execute('''CREATE TABLE record_sources (record_id BIGINT, checklist_group_id VARCHAR, checklist_id VARCHAR,
            avibase_id VARCHAR, scientific_name VARCHAR)''')
        with_records = batch_observations.replace(
            'WHERE', 'JOIN records r ON a."CARD" = r.card_id AND t.ebird_avibase_id = r.ebird_avibase_id WHERE')
        for batch in range(partitions):
            con.execute('''INSERT INTO record_sources SELECT r.record_id,
                CASE WHEN o."EVENT ID" = o."SAMPLING EVENT IDENTIFIER" THEN '' ELSE o."EVENT ID" END,
                o."SAMPLING EVENT IDENTIFIER", o."TAXON CONCEPT ID", o."SCIENTIFIC NAME" ''' + with_records, [batch])
        con.execute('COPY (SELECT * FROM record_sources ORDER BY record_id, checklist_id) TO ? (HEADER, COMPRESSION GZIP)',
                    [str(output_dir / 'audit/record_sources.csv.gz')])
        con.execute('''CREATE TABLE excluded AS SELECT
            o."SAMPLING EVENT IDENTIFIER" AS checklist_id,
            CASE WHEN o."EVENT ID" = o."SAMPLING EVENT IDENTIFIER" THEN '' ELSE o."EVENT ID" END AS checklist_group_id,
            o."OBSERVER ID" AS original_observer_id, o."TAXON CONCEPT ID" AS avibase_id,
            o."SCIENTIFIC NAME" AS scientific_name, o."CATEGORY" AS category,
            q."SAMPLING EVENT IDENTIFIER" AS representative_checklist_id,
            q."SOURCE CHECKLIST IDS" AS checklist_ids, q."SOURCE OBSERVER IDS" AS observer_ids,
            q."SHARED METADATA CONFLICT" AS shared_metadata_conflict,
            'taxon_not_in_declared_reference' AS reason
            FROM canonical c JOIN input.observations o USING (source_row)
            JOIN taxon_match t ON t.concept = coalesce(o."TAXON CONCEPT ID", '') AND t.name = coalesce(o."SCIENTIFIC NAME", '')
            JOIN quality q USING ("EVENT ID") WHERE t.ebird_avibase_id IS NULL''')
        con.execute('COPY (SELECT * FROM excluded) TO ? (HEADER, COMPRESSION GZIP)', [str(output_dir / 'excluded_observations.csv.gz')])
        excluded_count = con.execute('SELECT count(*) FROM excluded').fetchone()[0]
        event_outcomes = dict(con.execute('''SELECT CASE WHEN "CARD" <> '' THEN 'in_card' ELSE "UNASSIGNED REASON" END, count(*)
            FROM quality GROUP BY 1''').fetchall())
        unplaced_rows = con.execute('''SELECT count(*) FROM input.observations o JOIN quality q USING ("EVENT ID")
            WHERE q."CARD" = '' ''').fetchone()[0]
        record_count = con.execute('SELECT count(*) FROM records').fetchone()[0]
        record_taxa = con.execute('SELECT DISTINCT ebird_avibase_id FROM records').df()['ebird_avibase_id']
    work.unlink()
    if excluded_count:
        warnings.warn(f'{excluded_count:,} event–taxon rows have no eligible eBird taxon; see excluded_observations.csv.gz', stacklevel=2)
    # Only the comparatively small card and taxonomy tables are collected here.
    cards = pd.concat(card_tables, ignore_index=True) if card_tables else pd.DataFrame(columns=CARD_COLUMNS + ['protocol'])
    records = pd.DataFrame({'ebird_avibase_id': record_taxa})
    observed = reference.loc[reference['bio_concept_code'].isin(records['ebird_avibase_id']), 'species_code']
    included = reporting_taxa(observed, reference.set_index('species_code')['report_as'].to_dict())
    species = reference.loc[reference['species_code'].isin(included), ['bio_concept_code', 'sci_name', 'primary_com_name', 'category', 'species_code', 'taxon_order', 'report_as']].rename(columns={'bio_concept_code': 'ebird_avibase_id', 'sci_name': 'scientific_name', 'primary_com_name': 'common_name', 'species_code': 'ebird_species_code', 'taxon_order': 'ebird_taxon_order'})
    target = species['report_as'].ne('') & species['report_as'].ne(species['ebird_species_code'])
    species.loc[~target, 'report_as'] = ''
    taxonomy_codes = reference.set_index('species_code')['bio_concept_code']
    species.loc[target, 'report_as'] = taxonomy_codes.loc[species.loc[target, 'report_as']].to_numpy()
    card_table = cards
    ids = sorted(card_table['pentad_id'].unique())
    latitudes, longitudes = pentad2latlng(ids)
    polygons = pentad2polygon(ids)
    geod = Geod(ellps='WGS84')
    pentads = []
    for ident, latitude, longitude, polygon in zip(ids, latitudes, longitudes, polygons):
        west, south, east, north = polygon.bounds
        area, _ = geod.geometry_area_perimeter(polygon)
        pentads.append({'pentad_id': ident, 'latitude': latitude, 'longitude': longitude, 'west': west, 'south': south, 'east': east, 'north': north, 'area_km2': abs(area) / 1e6})
    pentad_table = pd.DataFrame(pentads, columns=['pentad_id', 'latitude', 'longitude', 'west', 'south', 'east', 'north', 'area_km2'])
    species['parent_spp'] = '[]'
    species['parent_avibase_id'] = '[]'
    species.to_csv(output_dir / 'species.csv', index=False)
    pentad_table.to_csv(output_dir / 'pentads.csv', index=False)
    with taxonomy_file.open('rb') as stream:
        taxonomy_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {'source': 'ebird', 'ebird_taxonomy_version': str(ebird_version), 'input_sha256': cache['input_sha256'],
                'taxonomy_sha256': taxonomy_hash, 'source_kind': source_kind, 'keep_missing_distance': keep_missing_distance,
                'record_grain': 'one eBird taxon presence per card (full or ad hoc), every reference category kept',
                'checklist_grain': 'one source checklist per card; shared event effort must not be summed across members',
                'card_selection': card_selection,
                'card_protocols': {protocol: int(n) for protocol, n in cards['protocol'].value_counts().items()},
                'event_outcomes': {key: int(value) for key, value in event_outcomes.items()},
                'observation_rows_outside_cards': unplaced_rows,
                'shared_conflict_policy': 'lowest checklist ID for date, time and location; maximum duration and distance; complete if any member is complete; most frequent protocol',
                'excluded_observation_rows': excluded_count, 'observation_rows': cache['observation_rows'],
                'events': cache['events'], 'cards': len(cards), 'records': record_count,
                'processing': {'engine': 'duckdb', 'duckdb_version': duckdb.__version__, 'memory_limit': memory_limit,
                               'threads': threads, 'partitions': partitions, 'cache_file': str(cache_file.resolve()),
                               'cache_version': cache['cache_version'],
                               'seconds': time.perf_counter() - started}}
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    write_datapackage(output_dir)
    print(f'Prepared {len(cards):,} cards and {record_count:,} presences in {time.perf_counter() - started:.1f}s.', flush=True)
    if return_tables:
        return species, pentad_table, card_table, pd.read_csv(output_dir / 'records.csv.gz', keep_default_na=False)
    return manifest
