"""Validate the shared core keys and eBird taxonomy links of a prepared dataset."""
import argparse
import json
from pathlib import Path
from frictionless import Package
import pandas as pd
from bird_atlas.card_schema import CARD_COLUMNS, parse_observers

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('dataset', type=Path)
args = parser.parse_args()
root = args.dataset
cards = pd.read_csv(root / 'cards.csv', dtype=str, keep_default_na=False)
records = pd.read_csv(root / 'records.csv.gz', dtype=str, keep_default_na=False)
species = pd.read_csv(root / 'species.csv', dtype=str, keep_default_na=False)
manifest = json.loads((root / 'manifest.json').read_text())
assert list(cards.columns[:len(CARD_COLUMNS)]) == CARD_COLUMNS
assert set(cards.source) <= {'abap', 'ebird'}
assert not cards.observer_id.str.startswith('[').any(), 'observer_id must be pipe-separated'
if 'protocol' in cards and cards.source.eq('ebird').any():
    assert set(cards.loc[cards.source.eq('ebird'), 'protocol']) <= {'full', 'adhoc'}
assert set(['source', 'record_id', 'card_id', 'ebird_avibase_id']).issubset(records.columns)
assert set(['ebird_avibase_id', 'ebird_species_code', 'scientific_name', 'report_as']).issubset(species.columns)
assert not cards.duplicated(['source', 'card_id']).any()
assert not records.duplicated(['source', 'record_id']).any()
assert not species.duplicated(['ebird_avibase_id']).any()
assert set(zip(records.source, records.card_id)) <= set(zip(cards.source, cards.card_id))
assert set(records.ebird_avibase_id) - {''} <= set(species.ebird_avibase_id)
assert set(species.report_as) - {''} <= set(species.ebird_avibase_id), 'Reporting taxa missing from species.csv'
assert manifest.get('ebird_taxonomy_version') or manifest.get('target_ebird_version')
assert (root / 'checklist.csv').exists() or 'ebird' not in set(cards.source)
if (root / 'checklist.csv').exists():
    checklists = pd.read_csv(root / 'checklist.csv', dtype=str, keep_default_na=False)
    assert not checklists.duplicated(['checklist_id', 'card_id']).any()
    assert (checklists.checklist_group_id.eq('') | checklists.checklist_group_id.ne(checklists.checklist_id)).all()
    assert set(checklists.card_id) <= set(cards.loc[cards.source.eq('ebird'), 'card_id'])
    observers = checklists.groupby('card_id').observer_id.apply(lambda values: set(values) - {''})
    for card in cards.loc[cards.source.eq('ebird')].itertuples():
        assert card.card_id in observers.index
        assert set(parse_observers(card.observer_id)) == observers.loc[card.card_id]
report = Package(str(root / 'datapackage.json')).validate()
if not report.valid:
    raise ValueError(str(report))
print(f'Valid shared keys: {len(cards)} cards, {len(records)} records, {len(species)} taxa.')
