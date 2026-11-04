"""Trim local EBD files once; keep observation-scale work on disk."""

import csv
import gzip
import hashlib
import json
from pathlib import Path
import time

import duckdb

from .ebird2card import _resolve_EBD_file, _detect_source_kind
from .checklists import MEMBER_COLUMNS

CACHE_VERSION = 2


def prepare_ebd_cache(input_file, cache_file, *, memory_limit='1GB', threads=4):
    """Create a reusable DuckDB cache without filtering taxa, dates or effort.

    Cache identity includes the source path, size, modification time and schema
    version. Its manifest retains a full source SHA256. Changed sources require
    a new cache path; incomplete caches are never reused.
    """
    input_file = _resolve_EBD_file(input_file).resolve()
    cache_file = Path(cache_file)
    manifest_file = cache_file.with_suffix(cache_file.suffix + '.json')
    stat = input_file.stat()
    identity = dict(input=str(input_file), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, cache_version=CACHE_VERSION)
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text())
        if any(manifest[key] != value for key, value in identity.items()):
            raise ValueError('Source or cache schema changed; choose a new --cache path.')
        return manifest
    if cache_file.exists():
        raise ValueError('Incomplete cache exists; remove it or choose a new --cache path.')

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if input_file.name.endswith('.gz') else open
    with opener(input_file, 'rt', encoding='utf-8-sig', newline='') as stream:
        columns = next(csv.reader(stream, delimiter='\t'))
    protocol = 'PROTOCOL TYPE' if 'PROTOCOL TYPE' in columns else 'PROTOCOL NAME'
    strings = ['SAMPLING EVENT IDENTIFIER', 'GROUP IDENTIFIER', 'SCIENTIFIC NAME', 'TAXON CONCEPT ID',
               'CATEGORY', 'TIME OBSERVATIONS STARTED', 'OBSERVER ID']
    projection = [f'"{name}"' for name in strings]
    projection += [f'"{protocol}" AS "PROTOCOL TYPE"']
    projection += [f'CAST("{name}" AS DOUBLE) AS "{name}"' for name in
                   ['LATITUDE', 'LONGITUDE', 'DURATION MINUTES', 'EFFORT DISTANCE KM']]
    projection += ['CAST("ALL SPECIES REPORTED" AS INTEGER) AS "ALL SPECIES REPORTED"',
                   'CAST(CAST("OBSERVATION DATE" AS TIMESTAMP) AS DATE) AS "OBSERVATION DATE"',
                   'COALESCE("GROUP IDENTIFIER", "SAMPLING EVENT IDENTIFIER") AS "EVENT ID"']
    # Retain source taxon detail for later all-taxon exports; never roll up here.
    optional = [name for name in ['GLOBAL UNIQUE IDENTIFIER', 'COMMON NAME', 'SUBSPECIES COMMON NAME',
                'SUBSPECIES SCIENTIFIC NAME', 'OBSERVATION COUNT', 'PROTOCOL CODE', 'NUMBER OBSERVERS',
                'EXOTIC CODE', 'APPROVED', 'REVIEWED'] if name in columns]
    projection += [f'"{name}"' for name in optional]
    started = time.perf_counter()
    print(f'Trimming {input_file.name} into {cache_file} ...', flush=True)
    with duckdb.connect(str(cache_file), config={'memory_limit': memory_limit, 'threads': threads}) as con:
        # Row numbers retain the original tie-break when a source repeats a taxon.
        con.execute(f"""CREATE TABLE observations AS SELECT row_number() OVER () AS source_row,
            {', '.join(projection)},
            CAST(CAST(CAST(CAST("OBSERVATION DATE" AS TIMESTAMP) AS DATE) AS VARCHAR)
                || ' ' || "TIME OBSERVATIONS STARTED" AS TIMESTAMP) AS "OBSERVATION DATETIME"
            FROM read_csv(?, delim='\t', header=true, all_varchar=true)""", [str(input_file)])
        con.execute('SET preserve_insertion_order = false')
        quoted = ', '.join(f'"{name}"' for name in MEMBER_COLUMNS)
        con.execute(f'CREATE TABLE members AS SELECT DISTINCT {quoted} FROM observations')
        con.execute('''CREATE TABLE representatives AS
            SELECT "EVENT ID", min("SAMPLING EVENT IDENTIFIER") AS representative_checklist_id
            FROM members GROUP BY "EVENT ID"''')
        rows, names = con.execute('SELECT count(*), count(DISTINCT "SCIENTIFIC NAME") FROM observations').fetchone()
        members = con.execute('SELECT count(*) FROM members').fetchone()[0]
        events = con.execute('SELECT count(*) FROM representatives').fetchone()[0]
        con.execute('CHECKPOINT')
    print(f'Hashing source ({stat.st_size / 1e9:.2f} GB) ...', flush=True)
    with input_file.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = {**identity, 'input_sha256': digest, 'source_kind': _detect_source_kind(input_file, columns),
                'observation_rows': rows, 'member_metadata_rows': members, 'events': events,
                'scientific_names': names, 'optional_columns': optional,
                'duckdb_version': duckdb.__version__, 'seconds': time.perf_counter() - started}
    manifest_file.write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Cached {rows:,} observations; {members:,} checklist metadata rows; {events:,} events.', flush=True)
    return manifest
