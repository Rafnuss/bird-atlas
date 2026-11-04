# Pentads, QDGCs and Kenya atlas squares

## References and terminology

The likely paper is Larsen, R., Holmern, T., Prager, S. D., Maliti, H. and Røskaft, E. (2009), [Using the extended quarter degree grid cell system to unify mapping and sharing of biodiversity data](https://doi.org/10.1111/j.1365-2028.2008.00997.x), *African Journal of Ecology* 47:382–392. It proposes hemisphere-aware, scalable references for sharing generalized biodiversity observations. The publisher's abstract was accessible during this review; its full text was not. The implementation details below were checked against the author's [QDGC project](https://github.com/ragnvald/qdgc), its [Python documentation](https://pypi.org/project/qdgc-py/), and [GBIF's extended QDGC documentation and examples](https://techdocs.gbif.org/en/data-use/api-sql-download-functions).

For pentads, see Brooks et al. (2022), [The African Bird Atlas Project: a description of the project and BirdMap data-collection protocol](https://doi.org/10.2989/00306525.2022.2125097), and the project team's [pentad explanation](https://thebdi.org/glossary/pentad/). A pentad spans 5 minutes of latitude and longitude. Its identifier encodes the northwest corner; `3355_1825`, for example, has north edge 33°55′S and west edge 18°25′E.

The terminology is easy to confuse:

| Grid | Angular width × height | Extended QDGC level | Pentads per cell |
| --- | --- | --- | --- |
| Degree cell | 1° × 1° | 0 | 144 = 12 × 12 |
| Kenya Bird Trends QSD | 0.5° × 0.5° = 30′ × 30′ | 1 | 36 = 6 × 6 |
| Quarter-degree grid cell, QDGC/QDS | 0.25° × 0.25° = 15′ × 15′ | 2 | 9 = 3 × 3 |
| Pentad | 5′ × 5′ = 1/12° × 1/12° | No integer level | 1 |

In the Kenyan website, “quarter square degree” describes an angular area of 0.25 square degrees, whose sides are each 0.5°. “Quarter-degree” in QDGC describes the side length, 0.25°. Use explicit side lengths or QDGC levels in code and metadata, rather than a bare “quarter square”.

These are longitude/latitude cells, not equal-area cells. Their east–west width shrinks towards the poles. Approximately, the side lengths in kilometres are `111.2 × side_degrees` north–south and `111.2 × cos(latitude) × side_degrees` east–west. Use an appropriate area calculation when area matters; a square-degree value is not a square-kilometre value.

## Naming conventions

Keep the existing ABAP pentad IDs unchanged. This package supports the quadrant separators `_` (south/east), `a` (south/west), `b` (north/west), and `c` (north/east), using signed northwest corners as documented in `grid.py`. These IDs and the extended QDGC codes are different naming systems: they agree geometrically where cells nest, but cannot be substituted for each other as strings. The pentad separator is a hemisphere indicator; a QDGC suffix letter identifies a subdivision within its parent.

For larger cells, use extended QDGC IDs rather than inventing longer pentad IDs. A code such as `E033S02BBCD` has a seven-character root (`E033S02`) and a path (`BBCD`). The root identifies longitude 33–34°E and latitude 2–3°S. Longitude is written first, with three digits; latitude follows with two digits and an explicit hemisphere. This also distinguishes cells on either side of zero. The prefix refers to the degree boundaries nearest the prime meridian and equator, which are not always the northwest corner.

Within each degree cell, every appended letter selects a geographic quadrant: `A` northwest, `B` northeast, `C` southwest, `D` southeast. The side length is `1 / 2**level` degrees; the number of letters is the level. An ancestor code is obtained by removing trailing letters. The author's implementation documents these orientations in all hemispheres. [QDGC Python documentation](https://pypi.org/project/qdgc-py/).

An older southern African QDS code such as `3119BD` has implicit south/east hemispheres and latitude first. Its extended equivalent is `E019S31BD`. The new utilities accept extended IDs only, so hemisphere information cannot be silently inferred from a legacy label. BDI gives the original [Calvinia example](https://thebdi.org/2019/06/27/quarter-degree-grid-cells-made-simple/).

Kenya Bird Trends labels such as `51b` are a separate atlas index, not a QDGC coordinate code or an ABAP hemisphere separator. Join them to geometric grid features through the website's feature IDs; retain both `Sq` and `qdgc` in exports. Checks against the current local website assets found that all 228 polygons exactly match level-1 QDGC bounds. Two examples are:

| Website `Sq` | Extended QDGC | West, south, east, north |
| --- | --- | --- |
| `51b` | `E037N00B` | 37.5, 0.5, 38, 1 |
| `63a` | `E037S00A` | 37, -0.5, 37.5, 0 |

## Where the hierarchies align

The QDGC hierarchy recursively divides each cell into four. The pentad grid divides each full degree into twelve cells along each axis. Consequently, whole pentads fit level 0, level 1 and level 2 cells exactly. They do not fit finer QDGC cells: level 3 has 7.5-minute sides, which cut across some 5-minute pentads. This follows from the side-length ratios, not from a separate naming rule.

`pentad2qdgc` therefore accepts levels 0–2 only. Its use of pentad centres is exact for those containing cells. Applying the same centre assignment to finer levels would imply more spatial precision than the pentad supplies. `qdgc2pentads` has the same restriction. Point coordinates can still be encoded at finer QDGC levels with `latlng2qdgc`; that operation assigns a point, not a pentad footprint.

The existing ABAP pentad parser expects two longitude-degree digits. Pentad conversions therefore retain that regional naming domain; the new extended QDGC codes themselves allow three longitude-degree digits. This change does not redefine pentad identifiers outside the existing domain.

Aggregating observations to a coarser cell does not improve their location precision or resolve different sampling protocols. In particular, a checklist's reported coordinate can select a cell, but does not locate every bird on a travelling checklist.

## Shared edges and implementation choices

The existing package assigns exact shared edges south/east: latitude `(south, north]` and longitude `[west, east)`. The new QDGC coordinate encoder uses the same convention at every level, including integer-degree lines, the equator and Greenwich. This ensures that converting a boundary point directly to a coarse QDGC agrees with converting its assigned pentad to that QDGC.

This is an explicit package choice, not a claim that every published encoder resolves edges identically. For example, the author's Python documentation gives `(0, 0)` as `E000N00C` at level 1; this package assigns the same boundary point to `E000S00A`. The cell geometries and interior-point names agree; exact-edge encodings can differ. [Author's documented boundary behavior](https://pypi.org/project/qdgc-py/).

Polygons geometrically include all four edges, so a generic `intersects` spatial join can match two or four cells for a boundary point. Use the coordinate encoder for unique point assignment. Grid generation excludes boundary-only contact and retains full cells rather than clipped fragments. Inputs use EPSG:4326; functions do not reproject or wrap antimeridian-crossing geometries. The point encoder's domain is `-90 < latitude <= 90`, `-180 <= longitude < 180`.

## Utility usage

The implementation lives in `bird_atlas/grid.py`, with shared bounds, centre, polygon, and grid-generation code. `bird_atlas/pentad.py` only re-exports existing names for compatibility. Inputs use latitude then longitude. Coordinate and centre conversions return lists even for a single ID; bounds and polygon functions return a single result for a string and a list for multiple IDs.

Use `level=None` for pentads and integer levels for QDGCs in `latlng2cell`, `latlng2polygon`, and `generate_grid`. `cell2bounds`, `cell2latlng`, and `cell2polygon` recognize either ID format, including mixed lists. The earlier `pentad2latlng`/`qdgc2latlng` and `pentad2polygon`/`qdgc2polygon` names are aliases to the shared implementations.

```python
from bird_atlas import (
    latlng2cell, cell2bounds, cell2latlng, cell2polygon, generate_grid,
    pentad2qdgc, qdgc2pentads, qdgc_parent, qdgc_children,
)

# One API for both resolutions.
pentad = latlng2cell(0.75, 37.75)[0]
qdgc = latlng2cell(0.75, 37.75, level=1)[0]  # E037N00B / Sq 51b
bounds = cell2bounds(qdgc)                  # (37.5, 0.5, 38.0, 1.0)
latitude, longitude = cell2latlng([pentad, qdgc])
polygon = cell2polygon(qdgc)

# Whole-cell aggregation and the binary QDGC hierarchy.
pentads = qdgc2pentads(qdgc)                # 36 pentads
parents = pentad2qdgc(pentads, level=1)      # same QDGC for every pentad
degree_cell = qdgc_parent(qdgc)             # E037N00
quarter_degree_cells = qdgc_children(qdgc)  # four 15-minute cells
grid = generate_grid(polygon, level=2, return_geojson=False)
```

The implementation adds no dependency. Tests cover published naming examples, all four hemispheres, edge ownership, parent consistency, exact pentad tilings, and geometric coverage. Website asset checks were performed separately so the test suite does not require a second repository.
