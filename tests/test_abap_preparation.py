"""ABAP preparation retains source detections and eBird reference names."""
import csv
import gzip
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

import pandas as pd
from bird_atlas import export_ebird
from bird_atlas.card_schema import CARD_COLUMNS

ROOT = Path(__file__).resolve().parents[1]

class AbapPreparationTest(unittest.TestCase):
    def test_start_time_normalization_preserves_unrecognized_values(self):
        normalize = runpy.run_path(str(ROOT / 'scripts/abap/prepare.py'))['normalize_time']
        self.assertEqual(normalize('8:30'), '08:30:00')
        self.assertEqual(normalize('0630'), '06:30:00')
        self.assertEqual(normalize('16:'), '')

    def test_export_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            fields = runpy.run_path(str(ROOT / 'scripts/abap/download_abap.py'))['FIELDS']
            row = dict.fromkeys(fields, '')
            row.update(CardNo='1', StartDate='2024-01-01', EndDate='2024-01-01', StartTime='06:00', Pentad='2555_2755', ObserverNo='1', TotalHours='2', Spp='242', Sequence='1')
            with (snapshot / 'records.csv').open('w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for protocol in ['full_protocol', 'adhoc']:
                    writer.writerow({**row, 'Protocol': protocol})
            (snapshot / 'manifest.json').write_text(json.dumps({'country': 'kenya', 'start_date': '2024-01-01', 'end_date': '2024-01-01', 'completed_at': '2026-11-04', 'records': 2, 'records_by_protocol': {'full_protocol': 1, 'adhoc': 1}}))
            for name, row in {'crosswalk': {'Spp': '242', 'avibase_id': 'avibase-12345678', 'ebird_v2026_status': 'exact', 'ebird_v2026_avibase_id': '', 'avibase_status': 'exact'}, 'taxonomy': {'bio_concept_code': 'avibase-12345678', 'sci_name': 'Example bird', 'primary_com_name': 'eBird name', 'category': 'species', 'species_code': 'example', 'taxon_order': '1', 'report_as': 'parent'}}.items():
                with (root / f'{name}.csv').open('w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=row)
                    writer.writeheader()
                    writer.writerow(row)
                    if name == 'taxonomy':
                        writer.writerow({**row, 'bio_concept_code': 'avibase-87654321', 'species_code': 'parent', 'sci_name': 'Parent bird', 'report_as': 'ancestor'})
                        writer.writerow({**row, 'bio_concept_code': 'avibase-99999999', 'species_code': 'ancestor', 'sci_name': 'Ancestor bird', 'report_as': 'ancestor'})
            (root / 'overrides.csv').write_text('region,avibase_id,ebird_v2026_avibase_id\n')
            output = root / 'output'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/abap/prepare.py'), '--snapshot', str(snapshot), '--output-dir', str(output), '--crosswalk', str(root / 'crosswalk.csv'), '--overrides', str(root / 'overrides.csv'), '--taxonomy', str(root / 'taxonomy.csv')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            abap_cards = pd.read_csv(output / 'cards.csv', dtype=str)
            self.assertEqual(abap_cards.columns[:len(CARD_COLUMNS)].tolist(), CARD_COLUMNS)
            self.assertEqual(abap_cards.loc[0, ['observer_id', 'start_date', 'end_date', 'duration_minutes']].tolist(), ['1', '2024-01-01', '2024-01-01', '120'])
            with gzip.open(output / 'records.csv.gz', 'rt') as f:
                records = list(csv.DictReader(f))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['ebird_avibase_id'], 'avibase-12345678')
            species = pd.read_csv(output / 'species.csv', dtype=str, keep_default_na=False).set_index('ebird_avibase_id')
            self.assertEqual(species.loc['avibase-12345678', 'report_as'], 'avibase-87654321')
            self.assertEqual(species.loc['avibase-87654321', 'report_as'], 'avibase-99999999')
            self.assertEqual(species.loc['avibase-99999999', 'report_as'], '')
            self.assertEqual(species.loc['avibase-87654321', 'parent_spp'], '[]')
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/shared/validate.py'), str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            species_file = output / 'species.csv'
            original_species = species_file.read_text()
            species.drop(index='avibase-99999999').reset_index().to_csv(species_file, index=False)
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/shared/validate.py'), str(output)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Reporting taxa missing', result.stderr)
            species_file.write_text(original_species)
            input_file = root / 'ebird.txt'
            pd.DataFrame([{'SAMPLING EVENT IDENTIFIER': 'E1', 'GROUP IDENTIFIER': None, 'OBSERVER ID': 'obs', 'SCIENTIFIC NAME': 'Example bird', 'TAXON CONCEPT ID': 'avibase-12345678', 'CATEGORY': 'species', 'LATITUDE': -25.0416667, 'LONGITUDE': 27.0416667, 'OBSERVATION DATE': '2024-01-01', 'TIME OBSERVATIONS STARTED': '06:00:00', 'PROTOCOL TYPE': 'Stationary', 'DURATION MINUTES': 120, 'EFFORT DISTANCE KM': 0, 'ALL SPECIES REPORTED': 1}]).to_csv(input_file, sep='\t', index=False)
            ebird = root / 'ebird'
            export_ebird(input_file, ebird, taxonomy_file=root / 'taxonomy.csv', ebird_version='2026')
            ebird_species = pd.read_csv(ebird / 'species.csv', dtype=str, keep_default_na=False).set_index('ebird_avibase_id')
            self.assertEqual(set(ebird_species.index), set(species.index))
            self.assertEqual(ebird_species.loc['avibase-87654321', 'report_as'], 'avibase-99999999')
            ebird_cards = pd.read_csv(ebird / 'cards.csv', dtype=str)
            self.assertEqual(ebird_cards.columns[:len(CARD_COLUMNS)].tolist(), CARD_COLUMNS)
            self.assertEqual(ebird_cards.loc[0, ['observer_id', 'start_date', 'end_date', 'start_time']].tolist(), ['obs', '2024-01-01', '2024-01-01', '06:00:00'])
            self.assertEqual(float(ebird_cards.loc[0, 'duration_minutes']), 120)
            combined = root / 'combined'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/shared/combine.py'), str(output), str(ebird), '--output', str(combined)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((combined / 'checklist.csv').exists())
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/shared/validate.py'), str(combined)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(set(pd.read_csv(combined / 'records.csv.gz')['source']), {'abap', 'ebird'})
            manifest_path = ebird / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['ebird_taxonomy_version'] = '2025'
            manifest_path.write_text(json.dumps(manifest))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/shared/combine.py'), str(output), str(ebird), '--output', str(root / 'rejected')], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'rejected').exists())

if __name__ == '__main__':
    unittest.main()
