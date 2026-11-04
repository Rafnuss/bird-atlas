"""Shared EPSG:4326 utilities for ABAP pentads and extended QDGCs.

level=None selects 5-minute pentads; QDGC level L has 1 / 2**L degree sides.
Coordinates are latitude first. Exact edges belong south/east, including
zero: latitude (south, north], longitude [west, east). See docs/spatial-grids.md.
"""

import geopandas as gpd
import numpy as np
from shapely.geometry import box


def _grid_nw_corner(lat, lng, scale):
    """Return integer north/west edges, snapping floating-point grid noise."""
    coords = np.asarray([np.atleast_1d(lat), np.atleast_1d(lng)], dtype=float) * scale
    coords = np.where(np.isclose(coords, np.rint(coords), rtol=0, atol=1e-10), np.rint(coords), coords)
    return np.ceil(coords[0]).astype(int), np.floor(coords[1]).astype(int)


def _grid_centres(bounds, scale):
    """Return flattened centre coordinates covering a bounding box."""
    west, south, east, north = bounds
    xs = (np.arange(np.floor(west * scale), np.ceil(east * scale)) + 0.5) / scale
    ys = (np.arange(np.floor(south * scale), np.ceil(north * scale)) + 0.5) / scale
    lng, lat = np.meshgrid(xs, ys)
    return lat.ravel(), lng.ravel()


# Naming ------------------------------------------------------------------

def latlng2pentad(lat, lng):
    """Return ABAP northwest-corner IDs, e.g. 3355_1825 or 0955c0850.

    Separators: _ south/east, a south/west, b north/west, c north/east.
    Degree fields retain the existing two-digit ABAP naming domain.
    """
    north, west = _grid_nw_corner(lat, lng, 12)
    quadrant = np.where(north > 0, np.where(west < 0, 'b', 'c'), np.where(west < 0, 'a', '_'))
    return [f'{y // 60:02d}{y % 60:02d}{q}{x // 60:02d}{x % 60:02d}'
            for y, x, q in zip(np.abs(north) * 5, np.abs(west) * 5, quadrant)]


def latlng2qdgc(lat, lng, level=2):
    """Return extended QDGC IDs: E/WdddN/Sdd followed by A=NW/B=NE/C=SW/D=SE.

    Level 0 is 1 degree, 1 is 30 minutes (Kenya QSD), 2 is 15 minutes.
    Levels are non-negative integers; -90 < lat <= 90, -180 <= lng < 180.
    Exact-edge choices follow this package's pentads, not legacy encoders.
    """
    scale = 2**level
    north, west = _grid_nw_corner(lat, lng, scale)
    south_degree, west_degree = (north - 1) // scale, west // scale
    row, col = (south_degree + 1) * scale - north, west - west_degree * scale
    codes = [f"{'E' if x >= 0 else 'W'}{(x if x >= 0 else -x - 1):03d}"
             f"{'N' if y >= 0 else 'S'}{(y if y >= 0 else -y - 1):02d}"
             for y, x in zip(south_degree, west_degree)]
    for bit in range(level - 1, -1, -1):
        letters = np.array(list('ABCD'))[((row >> bit) & 1) * 2 + ((col >> bit) & 1)]
        codes = [code + letter for code, letter in zip(codes, letters)]
    return codes


def latlng2cell(lat, lng, level=None):
    """Return pentad IDs by default, or extended QDGC IDs at a given level."""
    return latlng2pentad(lat, lng) if level is None else latlng2qdgc(lat, lng, level)


# Shared geometry ---------------------------------------------------------

def cell2bounds(cells):
    """Decode pentad or extended QDGC IDs to (west, south, east, north).

    A string returns one tuple; a sequence returns a list, including mixed
    naming systems. Inputs are valid source IDs; names are never reinterpreted.
    """
    bounds = []
    for code in [cells] if isinstance(cells, str) else cells:
        if code[0] in 'EW':
            west, south = int(code[1:4]), int(code[5:7])
            west = west if code[0] == 'E' else -west - 1
            south = south if code[4] == 'N' else -south - 1
            north, size = south + 1, 1
            for letter in code[7:]:
                size /= 2
                west += size * (letter in 'BD')
                north -= size * (letter in 'CD')
        else:
            north = (int(code[:2]) + int(code[2:4]) / 60) * (-1 if code[4] in '_a' else 1)
            west = (int(code[5:7]) + int(code[7:9]) / 60) * (-1 if code[4] in 'ab' else 1)
            size = 1 / 12
        bounds.append((west, north - size, west + size, north))
    return bounds[0] if isinstance(cells, str) else bounds


def cell2latlng(cells):
    """Return two lists of centre latitudes and longitudes for either ID type."""
    bounds = cell2bounds([cells] if isinstance(cells, str) else cells)
    return ([(south + north) / 2 for west, south, east, north in bounds],
            [(west + east) / 2 for west, south, east, north in bounds])


def cell2polygon(cells):
    """Return full cell polygons; a string returns one, a sequence a list."""
    if isinstance(cells, str):
        return box(*cell2bounds(cells))
    return [box(*bounds) for bounds in cell2bounds(cells)]


def latlng2polygon(lat, lng, level=None):
    """Return cells containing points; level=None selects pentads."""
    codes = latlng2cell(lat, lng, level)
    return cell2polygon(codes[0] if np.ndim(lat) == 0 else codes)


def generate_grid(geometry, level=None, return_geojson=True):
    """Return full cells overlapping an EPSG:4326 polygon by positive area.

    level=None selects pentads; an integer selects QDGCs. Return GeoJSON or
    a GeoDataFrame with pentad/qdgc IDs and geometry (plus level for QDGC).
    Input geometry must not cross the antimeridian; no reprojection is done.
    """
    lat, lng = _grid_centres(geometry.bounds, 12 if level is None else 2**level)
    codes = latlng2cell(lat, lng, level)
    grid = gpd.GeoDataFrame({'pentad' if level is None else 'qdgc': codes,
                             'geometry': cell2polygon(codes)}, crs='EPSG:4326')
    if level is not None:
        grid['level'] = level
    grid = grid[grid.intersects(geometry) & ~grid.touches(geometry)]
    return grid.to_json() if return_geojson else grid


# Hierarchy and exact aggregation -----------------------------------------

def qdgc_parent(qdgc, level=None):
    """Return an ancestor code; default to the immediate parent."""
    depth = len(qdgc) - 7
    level = depth - 1 if level is None else level
    if not 0 <= level <= depth:
        raise ValueError('Parent level must be between 0 and the cell level.')
    return qdgc[:7 + level]


def qdgc_children(qdgc, level=None):
    """Return descendant codes in A/B/C/D order; default to four children."""
    depth = len(qdgc) - 7
    level = depth + 1 if level is None else level
    if level < depth:
        raise ValueError('Child level must be at least the cell level.')
    codes = [qdgc]
    for _ in range(level - depth):
        codes = [code + letter for code in codes for letter in 'ABCD']
    return codes


def pentad2qdgc(pentads, level=2):
    """Return exact containing QDGCs; only levels 0–2 contain whole pentads."""
    if level not in (0, 1, 2):
        raise ValueError('Whole pentads nest only in QDGC levels 0, 1 and 2.')
    return latlng2qdgc(*cell2latlng(pentads), level)


def qdgc2pentads(qdgc):
    """Return the 144/36/9 pentads tiling a level 0/1/2 cell in the ABAP domain."""
    if len(qdgc) - 7 not in (0, 1, 2):
        raise ValueError('Whole pentads nest only in QDGC levels 0, 1 and 2.')
    return latlng2pentad(*_grid_centres(cell2bounds(qdgc), 12))


# Existing names share implementations; they do not duplicate geometry.
pentad2latlng = qdgc2latlng = cell2latlng
pentad2polygon = qdgc2polygon = cell2polygon
qdgc2bounds = cell2bounds


def generate_pentad_grid(geometry, return_geojson=True):
    """Generate pentads; preserve the existing positional GeoJSON argument."""
    return generate_grid(geometry, return_geojson=return_geojson)


def generate_qdgc_grid(geometry, level=2, return_geojson=True):
    """Generate QDGCs; prefer generate_grid when working with both systems."""
    return generate_grid(geometry, level, return_geojson)
