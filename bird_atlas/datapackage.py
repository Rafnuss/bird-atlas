"""Write a portable data package descriptor for a prepared dataset."""
import csv
import gzip
import json
from pathlib import Path


def write_datapackage(output_dir):
    root = Path(output_dir)
    resources = []
    names = ['cards', 'records', 'species']
    if (root / 'checklist.csv').exists():
        names.append('checklist')
    for name in names:
        filename = name + ('.csv.gz' if name == 'records' else '.csv')
        opener = gzip.open if name == 'records' else open
        with opener(root / filename, 'rt', encoding='utf-8', newline='') as stream:
            fields = next(csv.reader(stream))
        schema = {'fields': [{'name': field, 'type': 'string'} for field in fields],
                  'primaryKey': {'cards': ['source', 'card_id'], 'records': ['source', 'record_id'], 'species': ['ebird_avibase_id'], 'checklist': ['checklist_id', 'card_id']}[name],
                  'missingValues': ['']}
        if name == 'records':
            schema['foreignKeys'] = [{'fields': ['source', 'card_id'], 'reference': {'resource': 'cards', 'fields': ['source', 'card_id']}},
                                     {'fields': 'ebird_avibase_id', 'reference': {'resource': 'species', 'fields': 'ebird_avibase_id'}}]
        if name == 'checklist':
            schema['foreignKeys'] = [{'fields': 'card_id', 'reference': {'resource': 'cards', 'fields': 'card_id'}}]
        resource = {'name': name, 'path': filename, 'format': 'csv', 'schema': schema}
        if name == 'records':
            resource['compression'] = 'gz'
        resources.append(resource)
    (root / 'datapackage.json').write_text(json.dumps({'name': 'bird-atlas', 'resources': resources}, indent=2) + '\n')
