"""Check source-taxonomy IDs and candidate-card presence exports."""

import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

import pandas as pd

from bird_atlas import export_ebird


class EbirdExportTests(unittest.TestCase):
    def export_rows(self, root, rows, reference, **options):
        rows.to_csv(root / 'input.txt', sep='\t', index=False)
        pd.DataFrame(reference).to_csv(root / 'taxonomy.csv', index=False)
        return export_ebird(root / 'input.txt', root / 'out', taxonomy_file=root / 'taxonomy.csv', ebird_version='2025', **options)

    def test_every_event_joins_one_full_or_adhoc_card_and_all_taxa_are_kept(self):
        base = {'GROUP IDENTIFIER': None, 'OBSERVER ID': 'obs', 'LATITUDE': -25.0416667, 'LONGITUDE': 27.0416667,
                'EFFORT DISTANCE KM': 0.0, 'ALL SPECIES REPORTED': 1, 'PROTOCOL TYPE': 'Stationary', 'CATEGORY': 'species'}
        visits = [('S1', '2024-12-30', 20, 'Bird one', 'avibase-11111111'),
                  ('S2', '2025-01-03', 60, 'Bird one', 'avibase-11111111'),
                  ('S3', '2025-01-04', 70, 'Bird sp.', 'avibase-33333333'),
                  ('S4', '2025-01-12', 15, 'Bird one ssp', 'avibase-44444444'),
                  ('S5', '2025-01-12', 15, 'Not in reference', 'avibase-99999999')]
        rows = pd.DataFrame([{**base, 'SAMPLING EVENT IDENTIFIER': i, 'OBSERVATION DATE': d, 'TIME OBSERVATIONS STARTED': '06:00:00',
                              'DURATION MINUTES': m, 'SCIENTIFIC NAME': n, 'TAXON CONCEPT ID': c} for i, d, m, n, c in visits])
        far = {**base, 'SAMPLING EVENT IDENTIFIER': 'S6', 'OBSERVATION DATE': '2025-01-20', 'TIME OBSERVATIONS STARTED': '06:00:00',
               'DURATION MINUTES': 60, 'SCIENTIFIC NAME': 'Bird one', 'TAXON CONCEPT ID': 'avibase-11111111',
               'PROTOCOL TYPE': 'Traveling', 'EFFORT DISTANCE KM': 50.0}
        rows = pd.concat([rows, pd.DataFrame([far])], ignore_index=True)
        # One source checklist reported with two start times must still appear once per card.
        rows = pd.concat([rows, rows.iloc[[1]].assign(**{'TIME OBSERVATIONS STARTED': '09:00:00', 'SCIENTIFIC NAME': 'Bird sp.', 'TAXON CONCEPT ID': 'avibase-33333333'})], ignore_index=True)
        reference = {'TAXON_ORDER': [1, 2, 3], 'CATEGORY': ['species', 'spuh', 'issf'],
                     'SPECIES_CODE': ['bird1', 'bird', 'bird1a'], 'TAXON_CONCEPT_ID': ['avibase-11111111', 'avibase-33333333', 'avibase-44444444'],
                     'PRIMARY_COM_NAME': ['One', 'bird sp.', 'One (a)'], 'SCI_NAME': ['Bird one', 'Bird sp.', 'Bird one ssp'],
                     'REPORT_AS': ['', '', 'bird1']}
        # Optimized finds the full window starting 3 Jan and cuts the earlier ad-hoc card short;
        # walk starts at the first visit (30 Dec), so neither of its windows reaches two hours.
        for selection, expected in [('optimized', {'20241230': 'adhoc', '20250103': 'full', '20250112': 'adhoc'}),
                                    ('walk', {'20241230': 'adhoc', '20250104': 'adhoc', '20250112': 'adhoc'})]:
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                species, _, cards, records = self.export_rows(root, rows, reference, card_selection=selection)
                self.assertEqual(dict(zip(cards['card_id'].str[-8:], cards['protocol'])), expected)
                checklists = pd.read_csv(root / 'out/checklist.csv', keep_default_na=False)
                self.assertEqual(sorted(checklists['checklist_id']), ['S1', 'S2', 'S3', 'S4', 'S5'])
                self.assertFalse(checklists['checklist_id'].duplicated().any(), 'cards must not overlap')
                quality = pd.read_csv(root / 'out/audit/checklist_quality.csv.gz', keep_default_na=False).set_index('representative_checklist_id')
                self.assertEqual(quality.loc['S6', 'UNASSIGNED REASON'], 'spatial_screen_possible_boundary_crossing')
                self.assertTrue(quality.drop(index='S6')['CARD'].ne('').all())
                self.assertEqual(set(records['ebird_avibase_id']), {'avibase-11111111', 'avibase-33333333', 'avibase-44444444'})
                self.assertEqual(set(records['taxonomy_status']), {'concept_id_match'})
                self.assertIn('avibase-11111111', set(species['ebird_avibase_id']))
                manifest = json.loads((root / 'out/manifest.json').read_text())
                self.assertEqual(manifest['card_selection'], selection)
                self.assertEqual(manifest['event_outcomes'], {'in_card': 5, 'spatial_screen_possible_boundary_crossing': 1})
                excluded = pd.read_csv(root / 'out/excluded_observations.csv.gz', keep_default_na=False)
                self.assertEqual(excluded['checklist_id'].tolist(), ['S5'])
                self.assertEqual(excluded['reason'].tolist(), ['taxon_not_in_declared_reference'])
                validator = Path(__file__).resolve().parents[1] / 'scripts/shared/validate.py'
                result = subprocess.run([sys.executable, str(validator), str(root / 'out')], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ebird_reference_and_presence_grain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = pd.DataFrame({
                'SAMPLING EVENT IDENTIFIER': ['S1', 'S3', 'S2', 'S2', 'S4'], 'GROUP IDENTIFIER': ['G1', 'G1', None, None, None],
                'OBSERVER ID': ['obs', 'obs2', 'obs', 'obs', 'obs'], 'SCIENTIFIC NAME': ['Bird one', 'Bird one', 'Bird one', 'Bird two', 'Bird one'],
                'TAXON CONCEPT ID': ['old1', 'old1', 'old1', 'old2', 'old1'], 'CATEGORY': ['species'] * 5,
                'LATITUDE': [-25.0416667] * 5, 'LONGITUDE': [27.0416667] * 5,
                'OBSERVATION DATE': ['2025-01-01'] * 5,
                'TIME OBSERVATIONS STARTED': ['06:00:00', '06:00:00', '07:00:00', '07:00:00', '10:00:00'],
                'PROTOCOL TYPE': ['Stationary'] * 4 + ['Incidental'], 'DURATION MINUTES': [90] * 4 + [300],
                'EFFORT DISTANCE KM': [0] * 5, 'ALL SPECIES REPORTED': [1] * 5,
            })
            rows.to_csv(root / 'input.txt', sep='\t', index=False)
            pd.DataFrame({'TAXON_ORDER': [1, 2], 'CATEGORY': ['species', 'species'],
                          'SPECIES_CODE': ['bird1', 'bird2'], 'TAXON_CONCEPT_ID': ['avibase-11111111', 'avibase-22222222'],
                          'PRIMARY_COM_NAME': ['One', 'Two'], 'SCI_NAME': ['Bird one', 'Bird two'],
                          'REPORT_AS': ['bird1', 'bird1']}).to_csv(root / 'taxonomy.csv', index=False)
            species, pentads, cards, records = export_ebird(root / 'input.txt', root / 'out', taxonomy_file=root / 'taxonomy.csv', ebird_version='2025')
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards.iloc[0]['duration_minutes'], 150)
            self.assertEqual(cards.iloc[0]['observer_id'], ['obs', 'obs2'])
            self.assertEqual(cards.iloc[0]['protocol'], 'full')
            self.assertEqual(pd.read_csv(root / 'out/cards.csv').iloc[0]['observer_id'], 'obs|obs2')
            checklists = pd.read_csv(root / 'out/checklist.csv', keep_default_na=False)
            self.assertEqual(checklists.columns[:3].tolist(), ['checklist_id', 'checklist_group_id', 'card_id'])
            self.assertNotIn('source', checklists.columns)
            self.assertEqual(set(checklists['checklist_id']), {'S1', 'S2', 'S3', 'S4'})
            self.assertEqual(set(checklists.loc[checklists['checklist_group_id'] == 'G1', 'checklist_id']), {'S1', 'S3'})
            self.assertEqual(checklists.set_index('checklist_id').loc['S2', 'checklist_group_id'], '')
            self.assertEqual(checklists.set_index('checklist_id').loc['S3', 'observer_id'], 'obs2')
            self.assertEqual(checklists.set_index('checklist_id').loc['S4', 'duration_minutes'], 300)
            self.assertFalse(checklists.set_index('checklist_id').loc['S4', 'card_effort_eligible'])
            self.assertTrue(checklists.set_index('checklist_id').loc['S1', 'card_effort_eligible'])
            self.assertNotIn('card_window_duration_minutes', checklists.columns)
            self.assertNotIn('card_window_distance_km', checklists.columns)
            self.assertEqual(set(checklists['card_id']), set(cards['card_id']))
            self.assertEqual(set(cards.columns), {'source', 'card_id', 'pentad_id', 'observer_id', 'start_date', 'end_date', 'start_time', 'duration_minutes', 'protocol'})
            self.assertEqual(len(records), 2)
            self.assertEqual(set(records['ebird_avibase_id']), {'avibase-11111111', 'avibase-22222222'})
            self.assertEqual(set(records['ebird_avibase_id']), set(species['ebird_avibase_id']))
            self.assertEqual(set(cards['pentad_id']), set(pentads['pentad_id']))
            self.assertEqual(json.loads((root / 'out/manifest.json').read_text())['ebird_taxonomy_version'], '2025')
            sources = pd.read_csv(root / 'out/audit/record_sources.csv.gz', keep_default_na=False)
            self.assertEqual(len(sources), 5)
            self.assertEqual(set(sources['avibase_id']), {'old1', 'old2'})
            self.assertEqual(set(sources.loc[sources.checklist_group_id == 'G1', 'checklist_id']), {'S1', 'S3'})
            self.assertEqual(set(sources.loc[sources.checklist_id == 'S2', 'checklist_group_id']), {''})
            members = pd.read_csv(root / 'out/audit/checklist_members.csv.gz', keep_default_na=False)
            self.assertEqual(members.set_index('checklist_id').loc['S2', 'checklist_group_id'], '')
            quality = pd.read_csv(root / 'out/audit/checklist_quality.csv.gz', keep_default_na=False)
            self.assertEqual(quality.set_index('representative_checklist_id').loc['S2', 'checklist_group_id'], '')
            self.assertEqual(dict(zip(species['ebird_species_code'], species['report_as'])), {'bird1': '', 'bird2': 'avibase-11111111'})
            validator = Path(__file__).resolve().parents[1] / 'scripts/shared/validate.py'
            result = subprocess.run([sys.executable, str(validator), str(root / 'out')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
