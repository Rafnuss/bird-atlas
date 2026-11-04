"""Fill observer display names for the top finders, slowly, into the curated crosswalk.

Ranks observers by the pairs they found from `found_from` on that had no
record in the mid period (finders.csv joined to latest.csv), i.e. the likely
leaderboard. Only the top N are looked up.

- ABAP: names come from full-protocol cards in the prepared package. Kenya Bird
  Map has no observer-name endpoint, so ad hoc-only observers stay unavailable.
- eBird: the eBird API checklist view returns `userDisplayName`. It needs a
  personal key (https://ebird.org/api/keygen) in EBIRD_API_KEY or --key-file.
  Requests are sequential, with --delay seconds between them; the CSV is
  rewritten after every lookup, so the run can be stopped and resumed.

uv run python scripts/shared/resolve_observers.py \
    --run data/prepared/latest_by_qdgc/kenya_2026-11 \
    --abap data/prepared/abap/downloaded/kenya_2007-07-23_2026-11-03_reporting_parents
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from bird_atlas.card_schema import parse_observers

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--run', required=True, type=Path, help='latest_by_qdgc run directory')
parser.add_argument('--abap', required=True, type=Path, help='prepared ABAP package (for card names)')
parser.add_argument('--crosswalk', default=Path('data/curated/observer_crosswalk.csv'), type=Path)
parser.add_argument('--top', default=100, type=int)
parser.add_argument('--extra', type=Path,
                    help='CSV of source,observer_id,checklist_id to look up as well (e.g. the site leaderboard)')
parser.add_argument('--delay', default=20.0, type=float, help='seconds between eBird API requests')
parser.add_argument('--key-file', default=Path('~/.config/ebird/api_key').expanduser(), type=Path)
args = parser.parse_args()

COLUMNS = ['observer_id', 'source', 'checklist_id', 'display_name', 'status', 'checked_at', 'person']
now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

# 1. Candidate leaderboard -----------------------------------------------------
latest = pd.read_csv(args.run / 'latest.csv', dtype=str, keep_default_na=False)
finders = pd.read_csv(args.run / 'finders.csv', dtype=str, keep_default_na=False)
open_pairs = latest.loc[latest.mid_date == '', ['qdgc', 'species_id']]
scored = finders.merge(open_pairs, on=['qdgc', 'species_id'])
ranking = (scored.groupby(['source', 'observer_id'])
           .agg(points=('species_id', 'size'), checklist_id=('id', 'first'))
           .reset_index().sort_values('points', ascending=False).head(args.top))
if args.extra:
    extra = pd.read_csv(args.extra, dtype=str, keep_default_na=False)
    ranking = pd.concat([ranking, extra], ignore_index=True).drop_duplicates(['source', 'observer_id'])
print(f'Top {len(ranking)} finders: {ranking.source.value_counts().to_dict()}, '
      f'points {ranking.points.max()}..{ranking.points.min()}')

# 2. Existing crosswalk ----------------------------------------------------------
if args.crosswalk.exists():
    crosswalk = pd.read_csv(args.crosswalk, dtype=str, keep_default_na=False)
    if 'source' not in crosswalk:
        crosswalk['source'] = crosswalk.observer_id.map(lambda i: 'ebird' if i.startswith('obsr') else 'abap')
    crosswalk = crosswalk.reindex(columns=COLUMNS, fill_value='')
else:
    crosswalk = pd.DataFrame(columns=COLUMNS)
done = set(zip(crosswalk.source, crosswalk.observer_id))


def save(row):
    global crosswalk
    crosswalk = pd.concat([crosswalk, pd.DataFrame([row])], ignore_index=True)
    crosswalk.sort_values(['source', 'observer_id']).to_csv(args.crosswalk, index=False)


# 3. ABAP names from full-protocol cards -------------------------------------------
cards = pd.read_csv(args.abap / 'cards.csv', dtype=str, keep_default_na=False)
cards['observer_id'] = cards.observer_id.map(lambda value: '|'.join(parse_observers(value)))
named = cards[cards.observer_name != '']
abap_names = named.groupby('observer_id').observer_name.agg(lambda s: s.mode().iloc[0])
abap_cards = named.groupby('observer_id').card_id.first()
for r in ranking[ranking.source == 'abap'].itertuples():
    if ('abap', r.observer_id) in done:
        continue
    name = abap_names.get(r.observer_id, '')
    save({'observer_id': r.observer_id, 'source': 'abap', 'checklist_id': abap_cards.get(r.observer_id, r.checklist_id),
          'display_name': name, 'status': 'resolved' if name else 'unavailable', 'checked_at': now()})

# 4. eBird names, one slow API request at a time -----------------------------------
todo = [r for r in ranking[ranking.source == 'ebird'].itertuples() if ('ebird', r.observer_id) not in done]
key = os.environ.get('EBIRD_API_KEY') or (args.key_file.read_text().strip() if args.key_file.exists() else '')
if todo and not key:
    raise SystemExit(f'{len(todo)} eBird observers need a name; set EBIRD_API_KEY or create {args.key_file}.')
for n, r in enumerate(todo, 1):
    request = urllib.request.Request(
        f'https://api.ebird.org/v2/product/checklist/view/{r.checklist_id}',
        headers={'X-eBirdApiToken': key, 'User-Agent': 'bird-atlas observer crosswalk (research)'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            name = json.load(response).get('userDisplayName', '')
        status = 'resolved' if name else 'unavailable'
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429) or error.code >= 500:
            raise SystemExit(f'eBird API returned {error.code}; stopping. Re-run later to resume.')
        name, status = '', f'http_{error.code}'
    save({'observer_id': r.observer_id, 'source': 'ebird', 'checklist_id': r.checklist_id,
          'display_name': name, 'status': status, 'checked_at': now()})
    print(f'{n}/{len(todo)} {r.observer_id}: {name or status}', flush=True)
    if n < len(todo):
        time.sleep(args.delay)
print('Done.')
