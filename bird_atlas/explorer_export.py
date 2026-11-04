"""Write the Parquet tables read by bird-atlas-explorer from a prepared eBird package.

The explorer loads the card table and card-level taxa whole, and reads a card's
checklists and checklist-level taxa by row range when the card is opened. Tables
are therefore sorted by card and the card table stores each card's row offsets.
The output folder is self-contained, so it can be zipped and shared on its own.
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

FORMAT = 'bird-atlas-explorer'
VERSION = 1
INDEX_FILE = 'explorer.json'
RECORD_SOURCES = 'audit/record_sources.csv.gz'


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _csv(path: Path) -> str:
    return f'read_csv({_quote(path)}, header=true, all_varchar=true, delim=\',\', quote=\'"\', escape=\'"\', strict_mode=true)'


def _copy(con, query: str, path: Path, row_group_size: int) -> None:
    con.execute(f'COPY ({query}) TO {_quote(path)} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {row_group_size})')


def _with_offsets(con, table: str, counts: str, prefix: str) -> None:
    """Add <prefix>_offset/<prefix>_count columns to the card table from a (card_index, n) query."""
    con.execute(f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT t.*, coalesce(n.n, 0)::INTEGER AS {prefix}_count,
               (coalesce(sum(coalesce(n.n, 0)) OVER (ORDER BY t.card_index ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0))::INTEGER AS {prefix}_offset
        FROM {table} t LEFT JOIN ({counts}) n USING (card_index)
    """)


def export_explorer(package: Path, out: Path | None = None, *, memory_limit: str = '4GB', threads: int | None = None) -> dict:
    """Write <package>/explorer (or `out`) and return its explorer.json content."""
    started = time.perf_counter()
    package = Path(package)
    out = Path(out) if out else package / 'explorer'
    spec = json.loads((package / 'datapackage.json').read_text())
    paths = {resource['name']: package / resource['path'] for resource in spec.get('resources', []) if isinstance(resource.get('path'), str)}
    for name in ['cards', 'checklist', 'records', 'species']:
        if name not in paths or not paths[name].exists():
            raise FileNotFoundError(f'The package needs a local {name} resource; checked {paths.get(name, name)}.')
    pentads_path = paths.get('pentads', package / 'pentads.csv')
    sources_path = package / RECORD_SOURCES

    work = out.with_name(out.name + '.tmp')
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    spill = work / '.duckdb'
    con = duckdb.connect()
    try:
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute(f'SET temp_directory = {_quote(spill)}')
        con.execute('SET preserve_insertion_order = false')
        if threads:
            con.execute(f'SET threads = {int(threads)}')

        con.execute(f"""
            CREATE TABLE cards AS
            SELECT (row_number() OVER (ORDER BY pentad_id, start_date, card_id) - 1)::INTEGER AS card_index,
                   card_id, source, pentad_id, observer_id, start_date, nullif(end_date, '') AS end_date,
                   nullif(start_time, '') AS start_time, TRY_CAST(nullif(duration_minutes, '') AS DOUBLE) AS duration_minutes,
                   nullif(protocol, '') AS protocol
            FROM {_csv(paths['cards'])}
        """)
        sources = [row[0] for row in con.execute('SELECT DISTINCT source FROM cards').fetchall()]
        if sources != ['ebird']:
            raise ValueError(f'The explorer export needs an eBird-only package with checklists; found sources {sources}.')
        if con.execute('SELECT count(*) - count(DISTINCT card_id) FROM cards').fetchone()[0]:
            raise ValueError('Duplicate card_id values in cards.')

        con.execute(f"""
            CREATE TABLE records AS
            SELECT record_id, card_id, ebird_avibase_id AS taxon_id FROM {_csv(paths['records'])} WHERE coalesce(ebird_avibase_id, '') <> ''
        """)
        con.execute(f"""
            CREATE TABLE taxa AS
            WITH species AS (
                SELECT DISTINCT ON (ebird_avibase_id) ebird_avibase_id AS taxon_id, nullif(common_name, '') AS common_name,
                       nullif(scientific_name, '') AS scientific_name, TRY_CAST(ebird_taxon_order AS DOUBLE) AS taxon_order
                FROM {_csv(paths['species'])} WHERE coalesce(ebird_avibase_id, '') <> ''
            ), unknown AS (
                SELECT DISTINCT taxon_id, NULL::VARCHAR, NULL::VARCHAR, NULL::DOUBLE FROM records ANTI JOIN species USING (taxon_id)
            )
            SELECT (row_number() OVER (ORDER BY taxon_order NULLS LAST, taxon_id) - 1)::INTEGER AS taxon_index, *
            FROM (SELECT * FROM species UNION ALL SELECT * FROM unknown)
        """)
        con.execute("""
            CREATE TABLE card_taxa AS
            SELECT DISTINCT c.card_index, t.taxon_index FROM records r JOIN cards c USING (card_id) JOIN taxa t USING (taxon_id)
        """)

        con.execute(f"""
            CREATE TABLE checklists AS
            SELECT c.card_index, k.checklist_id, nullif(k.checklist_group_id, '') AS checklist_group_id,
                   nullif(k.observer_id, '') AS observer_id, k.start_date, nullif(k.start_time, '') AS start_time,
                   TRY_CAST(nullif(k.duration_minutes, '') AS DOUBLE) AS duration_minutes,
                   TRY_CAST(nullif(k.distance_km, '') AS DOUBLE) AS distance_km, nullif(k.protocol, '') AS protocol,
                   TRY_CAST(nullif(k.latitude, '') AS DOUBLE) AS latitude, TRY_CAST(nullif(k.longitude, '') AS DOUBLE) AS longitude,
                   lower(coalesce(k.card_effort_eligible, '')) IN ('true', '1') AS card_effort_eligible,
                   k.card_id
            FROM {_csv(paths['checklist'])} k LEFT JOIN cards c USING (card_id)
        """)
        orphan = con.execute('SELECT any_value(card_id), count(*) FROM checklists WHERE card_index IS NULL').fetchone()
        if orphan[1]:
            raise ValueError(f'{orphan[1]} checklist rows reference missing cards, e.g. {orphan[0]}.')

        has_sources = sources_path.exists()
        if has_sources:
            con.execute(f"""
                CREATE TABLE checklist_taxa AS
                SELECT DISTINCT r.card_index, s.checklist_id, r.taxon_index
                FROM (SELECT record_id, checklist_id FROM {_csv(sources_path)}) s
                JOIN (SELECT r.record_id, c.card_index, t.taxon_index FROM records r JOIN cards c USING (card_id) JOIN taxa t USING (taxon_id)) r USING (record_id)
                SEMI JOIN checklists k ON k.card_index = r.card_index AND k.checklist_id = s.checklist_id
            """)

        _with_offsets(con, 'cards', 'SELECT card_index, count(*) AS n FROM card_taxa GROUP BY 1', 'taxa')
        _with_offsets(con, 'cards', 'SELECT card_index, count(*) AS n FROM checklists GROUP BY 1', 'checklist')
        con.execute("""
            CREATE OR REPLACE TABLE cards AS
            SELECT c.*, coalesce(e.n, 0)::INTEGER AS event_count FROM cards c
            LEFT JOIN (SELECT card_index, count(DISTINCT coalesce(checklist_group_id, checklist_id)) AS n FROM checklists GROUP BY 1) e USING (card_index)
        """)
        if has_sources:
            _with_offsets(con, 'cards', 'SELECT card_index, count(*) AS n FROM checklist_taxa GROUP BY 1', 'checklist_taxa')

        if pentads_path.exists():
            con.execute(f"""
                CREATE TABLE pentads AS
                SELECT p.pentad_id, TRY_CAST(west AS DOUBLE) AS west, TRY_CAST(south AS DOUBLE) AS south,
                       TRY_CAST(east AS DOUBLE) AS east, TRY_CAST(north AS DOUBLE) AS north,
                       TRY_CAST(longitude AS DOUBLE) AS longitude, TRY_CAST(latitude AS DOUBLE) AS latitude
                FROM {_csv(pentads_path)} p SEMI JOIN cards USING (pentad_id)
            """)
        else:
            con.execute('CREATE TABLE pentads (pentad_id VARCHAR, west DOUBLE, south DOUBLE, east DOUBLE, north DOUBLE, longitude DOUBLE, latitude DOUBLE)')
        # Pentads without bounds are placed at the mean of their checklist locations.
        con.execute("""
            INSERT INTO pentads
            SELECT c.pentad_id, NULL, NULL, NULL, NULL, avg(k.longitude), avg(k.latitude)
            FROM checklists k JOIN cards c USING (card_index)
            WHERE k.longitude IS NOT NULL AND k.latitude IS NOT NULL AND c.pentad_id NOT IN (SELECT pentad_id FROM pentads)
            GROUP BY 1
        """)

        _copy(con, """
            SELECT card_index, card_id, pentad_id, observer_id, start_date, end_date, start_time, duration_minutes, protocol,
                   event_count, checklist_count, checklist_offset, taxa_count, taxa_offset""" + (', checklist_taxa_count, checklist_taxa_offset' if has_sources else '') + """
            FROM cards ORDER BY card_index""", work / 'cards.parquet', 131072)
        _copy(con, 'SELECT card_index, taxon_index FROM card_taxa ORDER BY card_index, taxon_index', work / 'card_taxa.parquet', 1048576)
        _copy(con, 'SELECT taxon_index, taxon_id, common_name, scientific_name, taxon_order FROM taxa ORDER BY taxon_index', work / 'taxa.parquet', 131072)
        _copy(con, """
            SELECT card_index, checklist_id, checklist_group_id, observer_id, start_date, start_time, duration_minutes, distance_km,
                   protocol, latitude, longitude, card_effort_eligible
            FROM checklists ORDER BY card_index, coalesce(checklist_group_id, checklist_id), checklist_id""", work / 'checklists.parquet', 16384)
        # Sorted by upper-case checklist ID so a lookup reads one row group, found from the row-group statistics.
        _copy(con, 'SELECT DISTINCT upper(checklist_id) AS checklist_id, card_index FROM checklists ORDER BY checklist_id, card_index',
              work / 'checklist_lookup.parquet', 16384)
        if has_sources:
            _copy(con, 'SELECT card_index, checklist_id, taxon_index FROM checklist_taxa ORDER BY card_index, checklist_id, taxon_index',
                  work / 'checklist_taxa.parquet', 65536)
        _copy(con, 'SELECT * FROM pentads ORDER BY pentad_id', work / 'pentads.parquet', 131072)

        count = lambda table: con.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
        protocols = dict(con.execute("SELECT coalesce(protocol, ''), count(*) FROM cards GROUP BY 1 ORDER BY 1").fetchall())
        months = con.execute('SELECT min(start_date), max(start_date) FROM cards').fetchone()
        manifest_path = package / 'manifest.json'
        index = {
            'format': FORMAT,
            'version': VERSION,
            'name': package.name,
            'created': datetime.now(UTC).isoformat(timespec='seconds'),
            'files': {
                'cards': 'cards.parquet', 'card_taxa': 'card_taxa.parquet', 'taxa': 'taxa.parquet',
                'checklists': 'checklists.parquet', 'checklist_lookup': 'checklist_lookup.parquet', 'pentads': 'pentads.parquet',
                **({'checklist_taxa': 'checklist_taxa.parquet'} if has_sources else {}),
            },
            'counts': {
                'cards': count('cards'), 'card_taxa': count('card_taxa'), 'taxa': count('taxa'), 'checklists': count('checklists'),
                'pentads': count('pentads'), **({'checklist_taxa': count('checklist_taxa')} if has_sources else {}),
            },
            'card_protocols': protocols,
            'date_range': list(months),
            'provenance': json.loads(manifest_path.read_text()) if manifest_path.exists() else {},
            'duckdb_version': duckdb.__version__,
        }
    except BaseException:
        con.close()
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
    index['seconds'] = round(time.perf_counter() - started, 1)
    (work / INDEX_FILE).write_text(json.dumps(index, indent=2) + '\n')
    shutil.rmtree(out, ignore_errors=True)
    work.rename(out)
    return index


def zip_explorer(folder: Path, archive: Path) -> Path:
    """Zip an explorer folder (stored, since Parquet is already compressed) for sharing."""
    import zipfile
    folder, archive = Path(folder), Path(archive)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as zf:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                zf.write(path, Path(archive.stem) / path.relative_to(folder))
    return archive
