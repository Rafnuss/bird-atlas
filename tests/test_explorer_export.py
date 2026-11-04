import gzip
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import duckdb

from bird_atlas.explorer_export import export_explorer, zip_explorer


def write_package(root, with_sources=True):
    root.mkdir(parents=True)
    resources = [{'name': name, 'path': path} for name, path in
                 [('cards', 'cards.csv'), ('checklist', 'checklist.csv'), ('records', 'records.csv'), ('species', 'species.csv')]]
    (root / 'datapackage.json').write_text(json.dumps({'resources': resources}))
    (root / 'manifest.json').write_text(json.dumps({'source_kind': 'ebd', 'ebird_taxonomy_version': '2025'}))
    (root / 'cards.csv').write_text(
        'card_id,source,pentad_id,observer_id,start_date,end_date,start_time,duration_minutes,protocol\n'
        'C2,ebird,2500_2705,obs3,2025-02-01,2025-02-01,07:00:00,,adhoc\n'
        'C1,ebird,2500_2700,obs1|obs2,2025-01-01,2025-01-05,06:00:00,120.0,full\n')
    (root / 'checklist.csv').write_text(
        'checklist_id,checklist_group_id,card_id,observer_id,start_date,start_time,duration_minutes,distance_km,protocol,all_species_reported,latitude,longitude,card_effort_eligible\n'
        'S2,G1,C1,obs2,2025-01-01,06:00:00,120,,Stationary,1,-25.1,27.1,True\n'
        'S1,G1,C1,obs1,2025-01-01,06:00:00,120,,Stationary,1,-25.1,27.1,True\n'
        'S3,,C1,obs1,2025-01-02,,,,"Traveling (2 band, 25m)",0,-25.2,27.2,False\n'
        'S4,,C2,obs3,2025-02-01,07:00:00,30,0.5,Traveling,1,-25.3,27.3,True\n')
    (root / 'records.csv').write_text('record_id,source,card_id,ebird_avibase_id\n1,ebird,C1,taxon1\n2,ebird,C1,taxon2\n3,ebird,C2,taxon1\n4,ebird,C2,unlisted\n')
    (root / 'species.csv').write_text('ebird_avibase_id,common_name,scientific_name,ebird_taxon_order\ntaxon1,"Bird, one",Avis una,2\ntaxon2,Bird two,Avis duo,1\n')
    if with_sources:
        (root / 'audit').mkdir()
        (root / 'audit' / 'record_sources.csv.gz').write_bytes(gzip.compress(
            b'record_id,checklist_group_id,checklist_id\n1,G1,S1\n1,G1,S2\n2,,S3\n3,,S4\n4,,S4\n'))
    return root


def table(path):
    return duckdb.sql(f"SELECT * FROM '{path}'").df()


class ExplorerExportTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_export_writes_card_ordered_tables_with_offsets(self):
        tmp_path = self.tmp
        package = write_package(tmp_path / 'pkg')
        index = export_explorer(package)
        out = package / 'explorer'
        assert index['format'] == 'bird-atlas-explorer' and index['name'] == 'pkg'
        assert index['counts'] == {'cards': 2, 'card_taxa': 4, 'taxa': 3, 'checklists': 4, 'pentads': 2, 'checklist_taxa': 5}
        assert index['card_protocols'] == {'adhoc': 1, 'full': 1}
        assert index['provenance']['ebird_taxonomy_version'] == '2025'
        assert json.loads((out / 'explorer.json').read_text())['files']['checklist_taxa'] == 'checklist_taxa.parquet'

        cards = table(out / 'cards.parquet').set_index('card_id')
        taxa = table(out / 'taxa.parquet')
        # Taxa follow the eBird taxon order; IDs missing from species.csv come last.
        assert taxa.taxon_id.tolist() == ['taxon2', 'taxon1', 'unlisted']
        card_taxa = table(out / 'card_taxa.parquet')
        checklists = table(out / 'checklists.parquet')
        for card_id, n_taxa, n_lists, n_events in [('C1', 2, 3, 2), ('C2', 2, 1, 1)]:
            card = cards.loc[card_id]
            assert (card.taxa_count, card.checklist_count, card.event_count) == (n_taxa, n_lists, n_events)
            rows = card_taxa.iloc[card.taxa_offset:card.taxa_offset + card.taxa_count]
            assert set(rows.card_index) == {card.card_index}
            rows = checklists.iloc[card.checklist_offset:card.checklist_offset + card.checklist_count]
            assert set(rows.card_index) == {card.card_index}
        c1 = checklists[checklists.card_index == cards.loc['C1'].card_index]
        assert c1.checklist_id.tolist() == ['S1', 'S2', 'S3']
        assert c1.protocol.tolist()[-1] == 'Traveling (2 band, 25m)'
        assert c1.card_effort_eligible.tolist() == [True, True, False]

        checklist_taxa = table(out / 'checklist_taxa.parquet')
        c2 = cards.loc['C2']
        rows = checklist_taxa.iloc[c2.checklist_taxa_offset:c2.checklist_taxa_offset + c2.checklist_taxa_count]
        assert sorted(rows.taxon_index) == [1, 2] and set(rows.checklist_id) == {'S4'}

        lookup = table(out / 'checklist_lookup.parquet')
        assert lookup.checklist_id.tolist() == sorted(lookup.checklist_id)
        # Without pentads.csv, pentads are placed at the mean of their checklist locations.
        pentads = table(out / 'pentads.parquet').set_index('pentad_id')
        assert round(pentads.loc['2500_2700'].longitude, 9) == round((27.1 * 2 + 27.2) / 3, 9)

        archive = zip_explorer(out, tmp_path / 'pkg-explorer.zip')
        assert 'pkg-explorer/explorer.json' in zipfile.ZipFile(archive).namelist()


    def test_export_without_record_sources(self):
        tmp_path = self.tmp
        package = write_package(tmp_path / 'pkg', with_sources=False)
        index = export_explorer(package, tmp_path / 'out')
        assert 'checklist_taxa' not in index['files']
        assert 'checklist_taxa_offset' not in table(tmp_path / 'out' / 'cards.parquet').columns


    def test_export_rejects_orphan_checklists(self):
        tmp_path = self.tmp
        package = write_package(tmp_path / 'pkg')
        with open(package / 'checklist.csv', 'a') as handle:
            handle.write('S9,,MISSING,obs1,2025-01-01,,,,Incidental,0,,,False\n')
        with self.assertRaisesRegex(ValueError, 'missing cards'):
            export_explorer(package)
        assert not (package / 'explorer').exists()


if __name__ == '__main__':
    unittest.main()
