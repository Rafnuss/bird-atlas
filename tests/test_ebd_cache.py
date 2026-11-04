"""Disk-backed conversion must preserve event boundaries and scientific rules."""

import hashlib
from pathlib import Path
import tempfile
import unittest
import warnings

import duckdb
import numpy as np
import pandas as pd

from bird_atlas import export_ebird, read_EBD
from bird_atlas.ebd_cache import prepare_ebd_cache
from bird_atlas.checklists import effort_by_day
from test_checklist_effort import source_rows


class DiskEbirdTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.input = self.root / 'ebd_test.txt.gz'
        self.cache = self.root / 'cache.duckdb'
        self.taxonomy = self.root / 'taxonomy.csv'
        pd.DataFrame({'TAXON_ORDER': [1, 2], 'CATEGORY': ['species', 'species'],
                      'SPECIES_CODE': ['ostrich', 'bird2'], 'TAXON_CONCEPT_ID': ['avibase-2247CB05', 'avibase-22222222'],
                      'PRIMARY_COM_NAME': ['Ostrich', 'Two'], 'SCI_NAME': ['Struthio camelus', 'Bird two'],
                      'REPORT_AS': ['', 'ostrich']}).to_csv(self.taxonomy, index=False)

    def export(self, name, partitions=4):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            return export_ebird(self.input, self.root / name, taxonomy_file=self.taxonomy,
                                ebird_version='2025', source_kind='ebd', cache_file=self.cache,
                                partitions=partitions, return_tables=False)

    def test_trim_preserves_taxon_detail_and_reuses_complete_cache(self):
        rows = source_rows()
        rows['SUBSPECIES SCIENTIFIC NAME'] = 'Struthio camelus australis'
        rows['GLOBAL UNIQUE IDENTIFIER'] = ['obs-001', 'obs-002']
        rows['OBSERVATION COUNT'] = ['X', '5']
        rows['CHECKLIST COMMENTS'] = 'not used by conversion'
        rows.to_csv(self.input, sep='\t', index=False)
        first = prepare_ebd_cache(self.input, self.cache)
        modified = self.cache.stat().st_mtime_ns
        second = prepare_ebd_cache(self.input, self.cache)
        self.assertEqual(first, second)
        self.assertEqual(self.cache.stat().st_mtime_ns, modified)
        self.assertEqual(first['input_sha256'], hashlib.sha256(self.input.read_bytes()).hexdigest())
        with duckdb.connect(str(self.cache), read_only=True) as con:
            cached = con.table('observations').df()
        self.assertEqual(cached['OBSERVATION COUNT'].tolist(), ['X', '5'])
        self.assertEqual(cached['SUBSPECIES SCIENTIFIC NAME'].unique().tolist(), ['Struthio camelus australis'])
        self.assertEqual(cached['GLOBAL UNIQUE IDENTIFIER'].tolist(), ['obs-001', 'obs-002'])
        self.assertNotIn('CHECKLIST COMMENTS', cached)
        # A modified source cannot silently reuse an old fingerprint.
        rows.iloc[:1].to_csv(self.input, sep='\t', index=False)
        with self.assertRaisesRegex(ValueError, 'Source or cache schema changed'):
            prepare_ebd_cache(self.input, self.cache)

    def test_partition_count_cannot_split_shared_events_or_five_day_windows(self):
        rows = source_rows(('06:00:00', '06:00:00', '07:00:00', '08:00:00', '09:00:00', '10:00:00'), (60, 60, 60, 120, 120, 10))
        rows['GROUP IDENTIFIER'] = ['G1', 'G1', None, None, None, None]
        rows['OBSERVER ID'] = ['obs1', 'obs2', 'obs1', 'obs3', 'obs3', 'obs1']
        rows['OBSERVATION DATE'] = ['2026-01-01', '2026-01-01', '2026-01-05', '2026-01-01', '2026-01-07', '2026-01-03']
        rows.loc[5, ['SCIENTIFIC NAME', 'TAXON CONCEPT ID']] = ['Unknown bird', 'unknown']
        rows = pd.concat([rows, rows.iloc[[0]].assign(**{'SCIENTIFIC NAME': 'Bird two', 'TAXON CONCEPT ID': 'old2'})])
        rows.sample(frac=1, random_state=7).to_csv(self.input, sep='\t', index=False)
        first = self.export('one', partitions=1)
        second = self.export('many', partitions=7)
        self.assertEqual(first['cards'], 3)
        self.assertEqual(first['excluded_observation_rows'], 1)
        self.assertEqual(first['records'], second['records'])
        for filename, keys in [('cards.csv', ['card_id']), ('records.csv.gz', ['record_id']),
                               ('checklist.csv', ['checklist_id', 'card_id']),
                               ('audit/checklist_members.csv.gz', ['checklist_id']),
                               ('audit/checklist_quality.csv.gz', ['representative_checklist_id']),
                               ('audit/record_sources.csv.gz', ['record_id', 'checklist_id'])]:
            with self.subTest(filename=filename):
                left = pd.read_csv(self.root / 'one' / filename, dtype=str, keep_default_na=False).sort_values(keys).reset_index(drop=True)
                right = pd.read_csv(self.root / 'many' / filename, dtype=str, keep_default_na=False).sort_values(keys).reset_index(drop=True)
                pd.testing.assert_frame_equal(left, right)
        cards = pd.read_csv(self.root / 'many/cards.csv')
        shared = cards.loc[cards.card_id.str.contains('obs1')].iloc[0]
        # An unmatched name must not erase the event's qualifying ten minutes.
        self.assertEqual(shared.duration_minutes, 130)
        self.assertEqual(shared.observer_id, 'obs1|obs2')

    def test_no_candidates_still_exports_audits_and_typed_empty_core_tables(self):
        rows = source_rows(durations=(10, 10))
        rows['SCIENTIFIC NAME'] = 'Unknown bird'
        rows['TAXON CONCEPT ID'] = 'unknown'
        rows.to_csv(self.input, sep='\t', index=False)
        manifest = self.export('empty')
        # Too little effort for a full card, so the visit becomes one ad-hoc card with no matched taxa.
        self.assertEqual(manifest['cards'], 1)
        self.assertEqual(manifest['card_protocols'], {'adhoc': 1})
        self.assertEqual(manifest['records'], 0)
        self.assertEqual(manifest['excluded_observation_rows'], 2)
        self.assertTrue(pd.read_csv(self.root / 'empty/records.csv.gz').empty)
        self.assertEqual(len(pd.read_csv(self.root / 'empty/audit/checklist_quality.csv.gz')), 2)

    def test_modern_general_ebd_protocol_name_is_not_a_project_assumption(self):
        rows = source_rows(durations=(120, 120)).rename(columns={'PROTOCOL TYPE': 'PROTOCOL NAME'})
        rows['GLOBAL UNIQUE IDENTIFIER'] = ['obs1', 'obs2']
        rows['EFFORT DISTANCE KM'] = 100
        rows['PROTOCOL NAME'] = 'Traveling'
        rows.to_csv(self.input, sep='\t', index=False)
        self.assertEqual(read_EBD(self.input).attrs['source_kind'], 'ebd')
        self.assertEqual(prepare_ebd_cache(self.input, self.cache)['source_kind'], 'ebd')
        manifest = self.export('screen')
        self.assertEqual(manifest['cards'], 0)
        quality = pd.read_csv(self.root / 'screen/audit/checklist_quality.csv.gz')
        self.assertTrue(quality['SPATIAL STATUS'].eq('possible_boundary_crossing').all())
        self.assertFalse(quality['KEEP PENTAD'].any())

    def test_vectorized_effort_matches_independent_minute_union(self):
        rng = np.random.default_rng(2025)
        starts = rng.integers(0, 8 * 24 * 60, 150)
        durations = rng.integers(1, 700, 150)
        rows = pd.DataFrame({'OBSERVER ID': rng.choice(['a', 'b', 'c'], 150),
                             'PENTAD': rng.choice(['one', 'two'], 150),
                             'OBSERVATION DATETIME': pd.Timestamp('2025-01-01') + pd.to_timedelta(starts, unit='m'),
                             'DURATION MINUTES': durations, 'KEEP PENTAD': True,
                             'KEEP PROTOCOL': True, 'ALL SPECIES REPORTED': 1, 'SHARED METADATA CONFLICT': False})
        occupied = {}
        for observer, pentad, start, duration in zip(rows['OBSERVER ID'], rows['PENTAD'], starts, durations):
            occupied.setdefault((observer, pentad), set()).update(range(start, start + duration))
        expected = {}
        for (observer, pentad), minutes in occupied.items():
            for minute in minutes:
                key = observer, pentad, pd.Timestamp('2025-01-01') + pd.Timedelta(days=minute // 1440)
                expected[key] = expected.get(key, 0) + 1
        actual = effort_by_day(rows, ['OBSERVER ID', 'PENTAD']).set_index(['OBSERVER ID', 'PENTAD', 'OBSERVATION DATE'])['DURATION MINUTES'].to_dict()
        self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
