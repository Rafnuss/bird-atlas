"""Download the BirdMap species catalogue."""
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

API = 'https://api.birdmap.africa/sabap2/v2/search/species?format=csv'
OUT = Path(__file__).resolve().parents[2] / 'data/taxonomy'
OUT.mkdir(parents=True, exist_ok=True)
month = datetime.now(ZoneInfo('Africa/Nairobi')).strftime('%Y-%m')
with urlopen(API, timeout=120) as response:
    content = response.read()
filename = OUT / f'abap_taxonomy_{month}.csv'
filename.write_bytes(content)
print(f'Saved {filename.name}')
