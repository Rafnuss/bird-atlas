"""Shared columns at the start of each prepared card table."""

CARD_COLUMNS = ['card_id', 'source', 'pentad_id', 'observer_id', 'start_date', 'end_date', 'start_time', 'duration_minutes']


def parse_observers(value):
    """Observer IDs from a card cell: pipe-separated, or a legacy JSON array."""
    import json
    value = '' if value is None else str(value).strip()
    if value.startswith('['):
        return [str(item) for item in json.loads(value)]
    return [item.strip() for item in value.split('|') if item.strip()]


def format_observers(values):
    return '|'.join(values)
