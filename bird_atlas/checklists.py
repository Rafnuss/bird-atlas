"""Independent sampling events, membership provenance, and elapsed effort."""

import numpy as np
import pandas as pd

EVENT_METADATA = [
    'LATITUDE', 'LONGITUDE', 'OBSERVATION DATE', 'OBSERVATION DATETIME',
    'PROTOCOL TYPE', 'DURATION MINUTES', 'EFFORT DISTANCE KM', 'ALL SPECIES REPORTED',
]
MEMBER_COLUMNS = ['EVENT ID', 'GROUP IDENTIFIER', 'SAMPLING EVENT IDENTIFIER', 'OBSERVER ID', *EVENT_METADATA]


def collapse_shared_checklists(ebd):
    """Union group taxa by concept ID; select lowest checklist ID deterministically.

    Retain all member IDs. Conflicting effort metadata is resolved as in
    collapse_event_metadata and flagged. Returns collapsed observations and an
    explicit member metadata table.
    """
    members = ebd[MEMBER_COLUMNS].drop_duplicates().sort_values('SAMPLING EVENT IDENTIFIER', kind='stable')
    representative = collapse_event_metadata(members)
    observations = ebd.sort_values('SAMPLING EVENT IDENTIFIER', kind='stable').drop_duplicates(['EVENT ID', 'TAXON CONCEPT ID']).copy()
    observations = observations.rename(columns={
        'SAMPLING EVENT IDENTIFIER': 'ORIGINAL SAMPLING EVENT IDENTIFIER', 'OBSERVER ID': 'ORIGINAL OBSERVER ID',
    }).drop(columns=EVENT_METADATA)
    observations = observations.merge(representative.drop(columns='GROUP IDENTIFIER'), on='EVENT ID', validate='many_to_one')
    return observations.sort_values('OBSERVATION DATETIME', kind='stable').reset_index(drop=True), members


def _resolve_protocol(values, representative):
    """Most frequent member protocol; ties keep the lowest checklist's, else alphabetical."""
    counts = values.value_counts()
    tied = counts.index[counts.eq(counts.max())]
    return representative if representative in tied else sorted(tied)[0]


def collapse_event_metadata(members):
    """Resolve shared membership at checklist scale, independent of taxa.

    Like auk_unique, the lowest checklist ID supplies the event's date, start
    time and location, and all member checklist and observer IDs are kept.
    When members disagree on effort, the event takes the maximum duration and
    distance, is complete if any member is complete, and uses the most frequent
    protocol. Such events keep SHARED METADATA CONFLICT for review but are not
    excluded.
    """
    members = members.sort_values('SAMPLING EVENT IDENTIFIER', kind='stable')
    representative = members.drop_duplicates('EVENT ID').copy().set_index('EVENT ID', drop=False)
    representative['SHARED METADATA CONFLICT'] = False
    representative['SOURCE CHECKLIST IDS'] = [[value] for value in representative['SAMPLING EVENT IDENTIFIER']]
    representative['SOURCE OBSERVER IDS'] = [[value] for value in representative['OBSERVER ID']]
    repeated = members.loc[members['EVENT ID'].duplicated(keep=False)]
    if len(repeated):
        groups = repeated.groupby('EVENT ID', sort=False)
        conflicts = groups[EVENT_METADATA].nunique(dropna=False).gt(1).any(axis=1)
        representative.loc[conflicts.index, 'SHARED METADATA CONFLICT'] = conflicts
        resolved = conflicts.index[conflicts]
        if len(resolved):
            conflicted = repeated.loc[repeated['EVENT ID'].isin(resolved)].groupby('EVENT ID', sort=False)
            for column in ['DURATION MINUTES', 'EFFORT DISTANCE KM', 'ALL SPECIES REPORTED']:
                representative.loc[resolved, column] = conflicted[column].max().reindex(resolved)
            protocols = conflicted['PROTOCOL TYPE'].agg(list).reindex(resolved)
            representative.loc[resolved, 'PROTOCOL TYPE'] = [
                _resolve_protocol(pd.Series(values), representative.at[event, 'PROTOCOL TYPE'])
                for event, values in protocols.items()]
        for target, source in [('SOURCE CHECKLIST IDS', 'SAMPLING EVENT IDENTIFIER'), ('SOURCE OBSERVER IDS', 'OBSERVER ID')]:
            values = groups[source].agg(list)
            representative.loc[values.index, target] = values
    return representative.reset_index(drop=True)


def qualifies_effort(chk):
    """Complete accepted checklists with known timing and spatial support."""
    return (chk['KEEP PENTAD'] & chk['KEEP PROTOCOL'] & chk['ALL SPECIES REPORTED'].eq(1)
            & chk['DURATION MINUTES'].gt(0) & chk['OBSERVATION DATETIME'].notna())


def effort_by_day(chk, keys):
    """Union eligible time intervals, split at midnight, then sum per day.

    Independent of row order; concurrent lists from one analysis unit cannot
    manufacture extra hours. Unknown starts never become midnight observations.
    """
    timed = chk.loc[qualifies_effort(chk), [*keys, 'OBSERVATION DATETIME', 'DURATION MINUTES']].copy()
    timed = timed.sort_values([*keys, 'OBSERVATION DATETIME'], kind='stable')
    timed['END'] = timed['OBSERVATION DATETIME'] + pd.to_timedelta(timed['DURATION MINUTES'], unit='m')
    latest = timed.groupby(keys, sort=False)['END'].cummax()
    previous = latest.groupby([timed[key] for key in keys], sort=False).shift()
    timed['BLOCK'] = (previous.isna() | timed['OBSERVATION DATETIME'].gt(previous)).cumsum()
    intervals = timed.groupby('BLOCK', sort=False).agg(**{
        **{key: (key, 'first') for key in keys},
        'START': ('OBSERVATION DATETIME', 'min'), 'END': ('END', 'max'),
    })
    # Expand only merged intervals that cross midnight, not every checklist.
    days = ((intervals['END'] - pd.Timedelta(nanoseconds=1)).dt.normalize() - intervals['START'].dt.normalize()).dt.days + 1
    expanded = intervals.loc[intervals.index.repeat(days)].copy()
    offsets = np.arange(len(expanded)) - np.repeat(days.cumsum().to_numpy() - days.to_numpy(), days)
    expanded['OBSERVATION DATE'] = expanded['START'].dt.normalize() + pd.to_timedelta(offsets, unit='D')
    start = expanded['START'].where(expanded['START'] > expanded['OBSERVATION DATE'], expanded['OBSERVATION DATE'])
    midnight = expanded['OBSERVATION DATE'] + pd.Timedelta(days=1)
    stop = expanded['END'].where(expanded['END'] < midnight, midnight)
    expanded['DURATION MINUTES'] = (stop - start).dt.total_seconds() / 60
    return expanded.groupby([*keys, 'OBSERVATION DATE'], as_index=False)['DURATION MINUTES'].sum()


def cross_pentad_overlaps(chk):
    """Flag contradictory same-observer timing across cells, without allocating it.

    Adjacent intervals are not overlaps. The flag is diagnostic: no evidence
    identifies which reported location or interval should be changed.
    """
    conflict = pd.Series(False, index=chk.index)
    timed = chk.loc[qualifies_effort(chk)].sort_values(['OBSERVER ID', 'OBSERVATION DATETIME'], kind='stable')
    ends = timed['OBSERVATION DATETIME'] + pd.to_timedelta(timed['DURATION MINUTES'], unit='m')
    active, flagged, observer = [], set(), None
    for index, current, start, end, pentad in zip(timed.index, timed['OBSERVER ID'], timed['OBSERVATION DATETIME'], ends, timed['PENTAD']):
        active = [row for row in active if row[1] > start] if current == observer else []
        for previous, _, previous_pentad in active:
            if pentad != previous_pentad:
                flagged.update([index, previous])
        active.append((index, end, pentad))
        observer = current
    conflict.loc[list(flagged)] = True
    return conflict
