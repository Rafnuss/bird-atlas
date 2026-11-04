"""Validate the notebooks without requiring the ignored local datasets."""
from pathlib import Path
import nbformat

root = Path(__file__).resolve().parents[1]
for path in sorted((root / 'notebook').glob('*.ipynb')):
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == 'code':
            compile(cell.source, str(path), 'exec')
    print(f'Notebook format and syntax passed: {path.name}')
