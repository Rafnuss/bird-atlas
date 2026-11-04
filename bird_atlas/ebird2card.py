import pandas as pd
import numpy as np

import os
import datetime
import gzip
from pathlib import Path

import requests
import tarfile

from .grid import latlng2pentad, pentad2latlng
from .checklists import EVENT_METADATA, collapse_shared_checklists, effort_by_day, qualifies_effort, cross_pentad_overlaps
from pyproj import Geod

DEFAULT_EBD_DIR = Path("data") / "ebd"


def _detect_source_kind(file, columns):
    # Modern general EBD also uses PROTOCOL NAME; it is not a project marker.
    if Path(file).name.lower().startswith('project-') or 'BAND INSIDE' in columns:
        return 'project'
    if Path(file).name.lower().startswith('ebd_') or ('GLOBAL UNIQUE IDENTIFIER' in columns and 'BAND INSIDE' not in columns):
        return 'ebd'
    return 'project' if 'PROTOCOL NAME' in columns and 'PROTOCOL TYPE' not in columns else 'ebd'


def _is_species_ebd_file(path):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
        columns = {column.strip().strip('"') for column in f.readline().split("\t")}
    return {"SAMPLING EVENT IDENTIFIER", "SCIENTIFIC NAME"}.issubset(columns)


def _resolve_EBD_file(file):
    path = Path(file)
    if path.is_file():
        return path
    if path.is_dir():
        candidates = sorted(
            candidate
            for candidate in path.iterdir()
            if candidate.is_file()
            and candidate.name.endswith((".txt", ".txt.gz"))
            and "sampling" not in candidate.name.lower()
            and _is_species_ebd_file(candidate)
        )
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise FileNotFoundError(
                f"No species-level EBD .txt or .txt.gz file found in {path}"
            )
        raise ValueError(
            f"More than one species-level EBD file found in {path}: {candidates}"
        )
    raise FileNotFoundError(f"EBD file or directory does not exist: {path}")


def download_EBD(year=None, month=None, output_dir=DEFAULT_EBD_DIR):
    if (year is None) != (month is None):
        raise ValueError("year and month must be provided together")

    if year is None and month is None:
        today = datetime.date.today()
        release_month = today.replace(day=1) - datetime.timedelta(days=1)
        candidates = []
        for _ in range(4):
            candidates.append((release_month.strftime("%Y"), release_month.strftime("%b")))
            release_month = release_month.replace(day=1) - datetime.timedelta(days=1)
    else:
        candidates = [(year, month)]

    os.makedirs(output_dir, exist_ok=True)

    for i, (year, month) in enumerate(candidates):
        url = f"https://download.ebird.org/ebd/prepackaged/ebd_AFR_rel{month}-{year}.tar"
        filename = os.path.basename(url)
        filepath = os.path.join(output_dir, filename)
        print(f"Checking African EBD release {month}-{year}...", flush=True)

        with requests.get(url, stream=True, timeout=(30, 120)) as response:
            if response.status_code == 404 and i < len(candidates) - 1:
                print("Not available; trying an older release.", flush=True)
                continue
            response.raise_for_status()
            total_size = int(response.headers.get("content-length", 0)) or None
            downloaded_size = 0
            next_progress = 0
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size:
                            progress = int(downloaded_size / total_size * 100)
                            if progress >= next_progress:
                                print(f"\rDownloading {filename}: {progress:3d}%", end="", flush=True)
                                next_progress += 5
                        elif downloaded_size // (100 * 1024 * 1024) > next_progress:
                            next_progress = downloaded_size // (100 * 1024 * 1024)
                            print(f"\rDownloading {filename}: {next_progress * 100} MB", end="", flush=True)
            print()
        break
    else:
        raise FileNotFoundError("No African EBD release found in the last four release months")

    extract_dir = os.path.join(output_dir, f"ebd_AFR_rel{month}-{year}")
    os.makedirs(extract_dir, exist_ok=True)
    with tarfile.open(filepath, "r") as tar:
        tar.extractall(extract_dir, filter="data")

    return filepath


def iter_EBD(file, *, nrows=None, chunksize=100000):
    """Yield raw normalized EBD chunks; shared groups are not collapsed per chunk."""
    file = _resolve_EBD_file(file)
    columns = pd.read_csv(file, sep="\t", nrows=0).columns
    protocol_column = "PROTOCOL TYPE" if "PROTOCOL TYPE" in columns else "PROTOCOL NAME"
    usecols = ["SAMPLING EVENT IDENTIFIER", "GROUP IDENTIFIER", "SCIENTIFIC NAME", "TAXON CONCEPT ID", "CATEGORY",
               "LATITUDE", "LONGITUDE", "OBSERVATION DATE", "TIME OBSERVATIONS STARTED", protocol_column,
               "DURATION MINUTES", "EFFORT DISTANCE KM", "ALL SPECIES REPORTED", "OBSERVER ID"]
    if "OBSERVATION COUNT" in columns:
        usecols.append("OBSERVATION COUNT")
    for ebd in pd.read_csv(file, sep="\t", usecols=usecols, nrows=nrows, chunksize=chunksize):
        ebd = ebd.rename(columns={protocol_column: "PROTOCOL TYPE"})
        ebd["OBSERVATION DATE"] = pd.to_datetime(ebd["OBSERVATION DATE"]).dt.normalize()
        ebd["DURATION MINUTES"] = ebd["DURATION MINUTES"].astype(float)
        ebd["OBSERVATION DATETIME"] = pd.to_datetime(
            ebd["OBSERVATION DATE"].dt.strftime("%Y-%m-%d") + " " + ebd["TIME OBSERVATIONS STARTED"],
            format="%Y-%m-%d %H:%M:%S",
        )
        ebd["EVENT ID"] = ebd["GROUP IDENTIFIER"].astype("string").fillna(ebd["SAMPLING EVENT IDENTIFIER"].astype("string"))
        ebd.attrs["source_kind"] = _detect_source_kind(file, columns)
        yield ebd


def read_EBD(file, nrows=None, *, return_members=False):
    """Read EBD, collapse independent events by Avibase concept, retain member IDs.

    ``return_members=True`` also returns the original checklist metadata table.
    Missing start times remain unknown and cannot establish timed effort.
    """
    raw = pd.concat(iter_EBD(file, nrows=nrows), ignore_index=True)
    ebd, members = collapse_shared_checklists(raw)
    ebd.attrs["source_kind"] = raw.attrs["source_kind"]
    return (ebd, members) if return_members else ebd


def ebd2chk(ebd, *, source_kind=None, keep_missing_distance=True, border_tolerance_m=0):
    """Screen independent events; project exports assume pentad containment.

    Source kind comes from read_EBD metadata unless explicitly supplied. EBD
    keeps missing distances by default; False excludes all missing distances.
    Diagnostic spatial status remains independent of acceptance policy.
    Positive border_tolerance_m allows exploratory acceptance beyond an edge;
    it does not change the original spatial diagnostic or establish containment.
    """
    source_kind = source_kind or ebd.attrs.get("source_kind", "ebd")
    columns = ["SAMPLING EVENT IDENTIFIER", "OBSERVER ID", *EVENT_METADATA,
               "EVENT ID", "GROUP IDENTIFIER", "SOURCE CHECKLIST IDS", "SOURCE OBSERVER IDS", "SHARED METADATA CONFLICT"]
    chk = ebd[columns].drop_duplicates("SAMPLING EVENT IDENTIFIER").sort_values("OBSERVATION DATETIME", kind="stable").reset_index(drop=True)
    chk["KEEP PROTOCOL"] = chk["PROTOCOL TYPE"].isin(["Stationary", "Traveling"])
    chk["PENTAD"] = pd.Series(latlng2pentad(chk["LATITUDE"], chk["LONGITUDE"]), dtype=str)
    lat, lng = pentad2latlng(chk["PENTAD"])
    north, south = np.array(lat) + 1/24, np.array(lat) - 1/24
    west, east = np.array(lng) - 1/24, np.array(lng) + 1/24
    geod = Geod(ellps="WGS84")
    edge_distances = [
        geod.inv(chk["LONGITUDE"].to_numpy(), chk["LATITUDE"].to_numpy(), chk["LONGITUDE"].to_numpy(), edge)[2]
        for edge in [north, south]
    ] + [
        geod.inv(chk["LONGITUDE"].to_numpy(), chk["LATITUDE"].to_numpy(), edge, chk["LATITUDE"].to_numpy())[2]
        for edge in [west, east]
    ]
    chk["EDGE DISTANCE M"] = np.min(edge_distances, axis=0)
    radius = chk["EFFORT DISTANCE KM"] * 1000
    radius = radius.where(chk["PROTOCOL TYPE"].ne("Stationary"), np.maximum(radius.fillna(0), 30.0))
    chk["SPATIAL STATUS"] = np.where(radius.isna(), "unknown_distance",
                                     np.where(radius.le(chk["EDGE DISTANCE M"]), "screen_pass", "possible_boundary_crossing"))
    missing_distance = chk["EFFORT DISTANCE KM"].isna()
    chk["SCREEN RADIUS M"] = radius
    chk["BORDER TOLERANCE M"] = border_tolerance_m
    accepted = radius.le(chk["EDGE DISTANCE M"] + border_tolerance_m) | (missing_distance & keep_missing_distance)
    if source_kind == "project":
        accepted = pd.Series(True, index=chk.index)
    chk["SPATIAL BASIS"] = np.where(source_kind == "project", "assumed_within_pentad",
                                    np.where(missing_distance & keep_missing_distance, "accepted_missing_distance",
                                             np.where(accepted & chk["SPATIAL STATUS"].eq("possible_boundary_crossing"),
                                                      "accepted_border_tolerance", "distance_screen")))
    chk["KEEP PENTAD"] = accepted & (~missing_distance | keep_missing_distance)
    chk["QUALIFIES EFFORT"] = qualifies_effort(chk)
    chk["CROSS PENTAD TIME CONFLICT"] = cross_pentad_overlaps(chk)
    return chk


def chk2valid_card(chk, *, min_duration_minutes=120, exclude_starts=None):
    """Select candidate windows; thresholds below 120 are research scenarios.

    exclude_starts (PENTAD, OBSERVER ID, OBSERVATION DATE) forbids those start days.
    """
    # Non-overlapping elapsed effort, split across calendar dates.
    checkday = effort_by_day(chk, ["PENTAD", "OBSERVER ID"])
    if checkday.empty:
        return checkday[["PENTAD", "OBSERVER ID", "OBSERVATION DATE"]].assign(CARD=pd.Series(dtype=str))

    starts = chk.loc[qualifies_effort(chk), ["PENTAD", "OBSERVER ID", "OBSERVATION DATE"]].drop_duplicates()
    starts["CAN START CARD"] = True
    checkday = checkday.merge(starts, how="left", on=["PENTAD", "OBSERVER ID", "OBSERVATION DATE"])
    checkday["CAN START CARD"] = checkday["CAN START CARD"].eq(True)
    if exclude_starts is not None and len(exclude_starts):
        keys = ["PENTAD", "OBSERVER ID", "OBSERVATION DATE"]
        forbidden = exclude_starts[keys].astype({"OBSERVATION DATE": checkday["OBSERVATION DATE"].dtype}).assign(FORBIDDEN=True)
        checkday = checkday.merge(forbidden.drop_duplicates(keys), how="left", on=keys)
        checkday.loc[checkday["FORBIDDEN"].eq(True), "CAN START CARD"] = False
        checkday = checkday.drop(columns="FORBIDDEN")

    totals = checkday.groupby(['PENTAD', 'OBSERVER ID'])['DURATION MINUTES'].transform('sum')
    checkday = checkday.loc[totals >= min_duration_minutes].sort_values('OBSERVATION DATE', kind='stable').reset_index(drop=True)
    selected = []
    for positions in checkday.groupby(['PENTAD', 'OBSERVER ID'], sort=False).indices.values():
        group = checkday.iloc[positions]
        starts = _card_window_starts(group['OBSERVATION DATE'].to_numpy(), group['DURATION MINUTES'].to_numpy(),
                                     group['CAN START CARD'].to_numpy(), min_duration_minutes)
        selected.extend(positions[starts])
    cards = checkday.iloc[selected][['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE']].copy()
    cards['CARD'] = cards['PENTAD'] + '_' + cards['OBSERVER ID'] + '_' + cards['OBSERVATION DATE'].dt.strftime('%Y%m%d')
    return cards.sort_values('CARD', kind='stable').reset_index(drop=True)


def _card_window_starts(dates, durations, can_start, min_duration_minutes):
    """Select greedy five-day windows using linear storage."""
    ends = np.searchsorted(dates, dates + np.timedelta64(5, 'D'))
    effort = np.concatenate(([0], durations.cumsum()))
    selected, start = [], 0
    while start < len(dates):
        end = ends[start]
        if can_start[start] and effort[end] - effort[start] >= min_duration_minutes:
            selected.append(start)
            start = end
        else:
            start += 1
    return np.asarray(selected, dtype=int)


CARD_SELECTIONS = ('optimized', 'walk')
WINDOW_DAYS = pd.Timedelta(days=5)


def _walk_windows(keys, dates, limits=None):
    """Greedy windows over sorted (key, date) rows: start at the first uncovered date,
    run five days or until the limit (the next full card's start). Returns, per row,
    the index of its window's first row, and each window's end."""
    starts = np.empty(len(dates), dtype=np.int64)
    ends = {}
    i, n = 0, len(dates)
    while i < n:
        end = dates[i] + WINDOW_DAYS.to_timedelta64()
        if limits is not None and limits[i] < end:
            end = limits[i]
        j = i + 1
        while j < n and keys[j] == keys[i] and dates[j] < end:
            j += 1
        starts[i:j] = i
        ends[i] = end
        i = j
    return starts, ends


def _accepted_days(chk):
    days = chk.loc[chk['KEEP PENTAD'], ['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE']].drop_duplicates()
    days['OBSERVATION DATE'] = days['OBSERVATION DATE'].astype('datetime64[ns]')
    return days.sort_values(['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE'], kind='stable').reset_index(drop=True)


def _card_ids(cards):
    return cards['PENTAD'] + '_' + cards['OBSERVER ID'] + '_' + cards['OBSERVATION DATE'].dt.strftime('%Y%m%d')


def _empty_windows():
    return pd.DataFrame({'PENTAD': pd.Series(dtype=str), 'OBSERVER ID': pd.Series(dtype=str),
                         'OBSERVATION DATE': pd.Series(dtype='datetime64[ns]'), 'CARD': pd.Series(dtype=str),
                         'WINDOW END': pd.Series(dtype='datetime64[ns]'), 'PROTOCOL': pd.Series(dtype=str)})


def _window_effort(chk, windows):
    """Qualifying effort each window would really get: only its own events, clipped at its end."""
    if windows.empty:
        return pd.Series(dtype=float)
    windows = windows.assign(CARD=_card_ids(windows))
    assigned = valid_card2chk_card(chk, windows)
    daily = effort_by_day(assigned, ['CARD']) if not assigned.empty else pd.DataFrame(columns=['CARD', 'OBSERVATION DATE', 'DURATION MINUTES'])
    daily = daily.merge(windows[['CARD', 'OBSERVATION DATE', 'WINDOW END']], on='CARD', suffixes=('', '_start'))
    daily = daily.loc[daily['OBSERVATION DATE'].ge(daily['OBSERVATION DATE_start']) & daily['OBSERVATION DATE'].lt(daily['WINDOW END'])]
    return daily.groupby('CARD')['DURATION MINUTES'].sum().reindex(windows['CARD']).fillna(0.0)


def select_card_windows(chk, *, method='optimized', min_duration_minutes=120):
    """Cover every accepted event (KEEP PENTAD) with non-overlapping cards per pentad and observer.

    Each card spans at most five calendar days, like a BirdLasser/ABAP card. A
    card is 'full' with at least min_duration_minutes of qualifying effort, else
    'adhoc'. method='optimized' first chooses the full windows that maximise
    full cards (chk2valid_card), then walks the remaining events into ad-hoc
    cards, each ending early rather than overlapping a full card. method='walk'
    starts every card at the observer's next visit, as in BirdLasser, and then
    classifies it. Returns PENTAD, OBSERVER ID, OBSERVATION DATE (start), CARD,
    WINDOW END (exclusive) and PROTOCOL.

    A full card needs the effort of its own events. Daily effort can include a
    long list from an earlier card running past midnight; a window that reaches
    the threshold only through such spillover is not full. In optimized mode its
    start is ruled out and the selection is repeated.
    """
    if method not in CARD_SELECTIONS:
        raise ValueError(f'card selection must be one of {CARD_SELECTIONS}')
    # Match the caller's datetime precision so later as-of joins line up.
    unit = chk['OBSERVATION DATE'].dtype if pd.api.types.is_datetime64_dtype(chk['OBSERVATION DATE']) else 'datetime64[ns]'
    days = _accepted_days(chk)
    if days.empty:
        return _empty_windows().astype({'OBSERVATION DATE': unit, 'WINDOW END': unit})
    keys = (days['PENTAD'] + '\0' + days['OBSERVER ID']).to_numpy()
    dates = days['OBSERVATION DATE'].to_numpy()
    if method == 'walk':
        starts, ends = _walk_windows(keys, dates)
        windows = days.iloc[sorted(ends)].copy()
        windows['WINDOW END'] = [ends[i] for i in sorted(ends)]
        windows = windows.astype({'OBSERVATION DATE': unit, 'WINDOW END': unit})
        minutes = _window_effort(chk, windows).to_numpy(dtype=float)
        windows['PROTOCOL'] = np.where(minutes >= min_duration_minutes, 'full', 'adhoc')
    else:
        excluded = None
        while True:
            full = chk2valid_card(chk, min_duration_minutes=min_duration_minutes, exclude_starts=excluded)
            full = full.assign(**{'WINDOW END': full['OBSERVATION DATE'] + WINDOW_DAYS, 'PROTOCOL': 'full'})
            short = full.loc[_window_effort(chk, full).to_numpy() < min_duration_minutes]
            if short.empty:
                break
            excluded = pd.concat([excluded, short], ignore_index=True) if excluded is not None else short
        full['OBSERVATION DATE'] = full['OBSERVATION DATE'].astype('datetime64[ns]')
        full['WINDOW END'] = full['WINDOW END'].astype('datetime64[ns]')
        # Days inside a full window belong to it; walk the rest, stopping at the next full start.
        rest = days.sort_values('OBSERVATION DATE', kind='stable')
        rest['NEXT FULL'] = pd.Series(pd.NaT, index=rest.index, dtype='datetime64[ns]')
        if not full.empty:
            starts_by = full[['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE', 'WINDOW END']].rename(columns={'OBSERVATION DATE': 'FULL START'}).sort_values('FULL START')
            prior = pd.merge_asof(rest.drop(columns='NEXT FULL'), starts_by, left_on='OBSERVATION DATE', right_on='FULL START',
                                  by=['PENTAD', 'OBSERVER ID'], direction='backward')
            following = pd.merge_asof(rest.drop(columns='NEXT FULL'), starts_by.drop(columns='WINDOW END').rename(columns={'FULL START': 'NEXT FULL'}),
                                      left_on='OBSERVATION DATE', right_on='NEXT FULL', by=['PENTAD', 'OBSERVER ID'],
                                      direction='forward', allow_exact_matches=False)
            rest = following.loc[~prior['OBSERVATION DATE'].lt(prior['WINDOW END']).to_numpy()]
        located = rest
        located = located.sort_values(['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE'], kind='stable').reset_index(drop=True)
        limits = located['NEXT FULL'].fillna(pd.Timestamp.max).to_numpy(dtype='datetime64[ns]')
        rest_keys = (located['PENTAD'] + '\0' + located['OBSERVER ID']).to_numpy()
        _, ends = _walk_windows(rest_keys, located['OBSERVATION DATE'].to_numpy(), limits)
        adhoc = located.iloc[sorted(ends)][['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE']].copy()
        adhoc['WINDOW END'] = [ends[i] for i in sorted(ends)]
        adhoc['PROTOCOL'] = 'adhoc'
        windows = pd.concat([full.drop(columns='CARD'), adhoc], ignore_index=True)
    windows = windows[['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE', 'WINDOW END', 'PROTOCOL']].copy()
    windows['OBSERVATION DATE'] = windows['OBSERVATION DATE'].astype(unit)
    windows['WINDOW END'] = pd.to_datetime(windows['WINDOW END']).astype(unit)
    windows['CARD'] = _card_ids(windows)
    return windows[['PENTAD', 'OBSERVER ID', 'OBSERVATION DATE', 'CARD', 'WINDOW END', 'PROTOCOL']].sort_values('CARD', kind='stable').reset_index(drop=True)


def checkday_pentad_observer(df, *, min_duration_minutes=120):
    """Label chronological, non-overlapping cards within five calendar days."""
    df = df.sort_values('OBSERVATION DATE').copy()
    dates = df['OBSERVATION DATE'].to_numpy()
    can_start = df['CAN START CARD'].to_numpy() if 'CAN START CARD' in df else np.ones(len(df), dtype=bool)
    labels = np.full(len(df), '', dtype=object)
    for start in _card_window_starts(dates, df['DURATION MINUTES'].to_numpy(), can_start, min_duration_minutes):
        end = np.searchsorted(dates, dates[start] + np.timedelta64(5, 'D'))
        labels[start:end] = df.iloc[start]['pentad_observer_date']
    df['CARD'] = labels
    return df


def valid_card2chk_card(chk, card_valid):
    """Assign each event to the latest candidate start without a Cartesian join."""
    left = chk.loc[chk['KEEP PENTAD']].rename(columns={'OBSERVATION DATE': 'OBSERVATION DATE_chk'})
    right = card_valid.rename(columns={'OBSERVATION DATE': 'OBSERVATION DATE_card'})
    chk_card = pd.merge_asof(left.sort_values('OBSERVATION DATE_chk'), right.sort_values('OBSERVATION DATE_card'),
                             left_on='OBSERVATION DATE_chk', right_on='OBSERVATION DATE_card',
                             by=['OBSERVER ID', 'PENTAD'], direction='backward', tolerance=pd.Timedelta(days=5))
    window_end = chk_card['WINDOW END'] if 'WINDOW END' in chk_card else chk_card['OBSERVATION DATE_card'] + WINDOW_DAYS
    chk_card = chk_card.loc[chk_card['CARD'].notna() & chk_card['OBSERVATION DATE_chk'].lt(window_end)].copy()
    stop = chk_card['WINDOW END'] if 'WINDOW END' in chk_card else chk_card['OBSERVATION DATE_card'] + WINDOW_DAYS
    reported_end = chk_card['OBSERVATION DATETIME'] + pd.to_timedelta(chk_card['DURATION MINUTES'], unit='m')
    clipped = reported_end.gt(stop)
    chk_card.loc[clipped, 'DURATION MINUTES'] = (stop[clipped] - chk_card.loc[clipped, 'OBSERVATION DATETIME']).dt.total_seconds() / 60
    chk_card.loc[clipped & chk_card['EFFORT DISTANCE KM'].ne(0), 'EFFORT DISTANCE KM'] = np.nan
    return chk_card


def _unique_members(values):
    """Flatten event memberships without assigning individual participation effort."""
    return sorted({member for group in values for member in group})


def chk_card2card_chk(chk_card, card_valid):
    """Aggregate card metadata; only qualifying interval union counts as effort."""
    card_chk = chk_card.groupby("CARD").agg(**{
        "SOURCE CHECKLIST IDS": ("SOURCE CHECKLIST IDS", _unique_members),
        "SOURCE OBSERVER IDS": ("SOURCE OBSERVER IDS", _unique_members),
        "EVENT IDS": ("EVENT ID", list),
        "OBSERVATION DATETIME_min": ("OBSERVATION DATETIME", "min"),
    }).reset_index()
    if "WINDOW END" not in card_valid:
        card_valid = card_valid.assign(**{"WINDOW END": card_valid["OBSERVATION DATE"] + WINDOW_DAYS})
    daily = effort_by_day(chk_card, ["CARD"])
    daily = daily.merge(card_valid[["CARD", "OBSERVATION DATE", "WINDOW END"]], on="CARD", suffixes=("", "_start"))
    daily = daily.loc[(daily["OBSERVATION DATE"] >= daily["OBSERVATION DATE_start"])
                      & (daily["OBSERVATION DATE"] < daily["WINDOW END"])]
    durations = daily.groupby("CARD")["DURATION MINUTES"].sum().rename("DURATION MINUTES_sum")
    qualified = chk_card.loc[qualifies_effort(chk_card)].copy()
    qualified["EFFORT END"] = qualified["OBSERVATION DATETIME"] + pd.to_timedelta(qualified["DURATION MINUTES"], unit="m")
    ends = qualified.groupby("CARD")["EFFORT END"].max().rename("OBSERVATION DATETIME_max")
    # Any missing distance makes the full total unknown, rather than a partial sum.
    distances = qualified.groupby("CARD")["EFFORT DISTANCE KM"].agg(lambda values: values.sum() if values.notna().all() else np.nan).rename("EFFORT DISTANCE KM_sum")
    # Overlapping route distances cannot be corrected from clocks alone.
    naive = qualified.groupby("CARD")["DURATION MINUTES"].sum()
    overlap = naive.sub(durations).clip(lower=0).rename("OVERLAP MINUTES REMOVED")
    distances.loc[overlap.gt(0) & distances.ne(0)] = np.nan
    # Ad-hoc cards can have no qualifying effort: their duration is then unknown, not zero.
    for table in [durations, ends, distances, overlap]:
        card_chk = card_chk.merge(table, on="CARD", how="left")
    card_chk["OVERLAP MINUTES REMOVED"] = card_chk["OVERLAP MINUTES REMOVED"].fillna(0)
    card_chk = card_chk.merge(card_valid, on="CARD", validate="one_to_one")
    last_start = chk_card.groupby("CARD")["OBSERVATION DATETIME"].max()
    card_chk["OBSERVATION DATETIME_max"] = pd.concat([card_chk.set_index("CARD")["OBSERVATION DATETIME_max"], last_start], axis=1).max(axis=1).reindex(card_chk["CARD"]).to_numpy()
    card_chk["OBSERVATION DATETIME_max"] = card_chk["OBSERVATION DATETIME_max"].where(
        card_chk["OBSERVATION DATETIME_max"] < card_chk["WINDOW END"],
        card_chk["WINDOW END"] - pd.Timedelta(microseconds=1))
    return card_chk


