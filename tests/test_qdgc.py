"""Extended QDGC naming, exact pentad tiling and shared-edge regressions."""

import json
import unittest

import numpy as np
from shapely.geometry import Point, box
from shapely.ops import unary_union

from bird_atlas import (
    generate_qdgc_grid, latlng2pentad, latlng2qdgc, pentad2polygon,
    pentad2qdgc, qdgc2bounds, qdgc2latlng, qdgc2pentads, qdgc2polygon,
    qdgc_children, qdgc_parent,
)


class QdgcTests(unittest.TestCase):
    def test_published_examples(self):
        # BDI's Calvinia example, with Larsen's extended hemisphere prefix.
        self.assertEqual(latlng2qdgc(-31.475, 19.773), ['E019S31BD'])
        self.assertEqual(qdgc2bounds('E019S31BD'), (19.75, -31.5, 20, -31.25))
        # GBIF's published level 0-6 example; point is away from cell edges.
        for level in range(7):
            self.assertEqual(latlng2qdgc(52.3, 10.3, level), ['E010N52' + 'CBCCBB'[:level]])

    def test_quadrant_letters_are_geographic_in_all_hemispheres(self):
        for prefix, west, south in [('E010N10', 10, 10), ('E010S10', 10, -11),
                                     ('W010N10', -11, 10), ('W010S10', -11, -11)]:
            for letter, dx, dy in [('A', 0.25, 0.75), ('B', 0.75, 0.75),
                                   ('C', 0.25, 0.25), ('D', 0.75, 0.25)]:
                with self.subTest(code=prefix + letter):
                    self.assertEqual(latlng2qdgc(south + dy, west + dx, 1), [prefix + letter])

    def test_edges_use_the_existing_pentad_south_east_rule(self):
        for lat, lng, expected in [(0, 0, 'E000S00A'), (1, 1, 'E001N00A'),
                                   (-1, -1, 'W000S01A'), (0.5, 0.5, 'E000N00D'),
                                   (-0.5, -0.5, 'W000S00D')]:
            with self.subTest(lat=lat, lng=lng):
                self.assertEqual(latlng2qdgc(lat, lng, 1), [expected])
                west, south, east, north = qdgc2bounds(expected)
                self.assertEqual(west, lng)
                self.assertEqual(north, lat)
                self.assertEqual((east - west, north - south), (0.5, 0.5))
                self.assertNotEqual(latlng2qdgc(lat + 1e-7, lng, 1), [expected])
                self.assertNotEqual(latlng2qdgc(lat, lng - 1e-7, 1), [expected])
                self.assertEqual(pentad2qdgc(latlng2pentad(lat, lng), 1), [expected])

    def test_random_points_roundtrip_and_parent_agreement(self):
        rng = np.random.default_rng(42)
        lat, lng = rng.uniform(-80, 80, 500), rng.uniform(-179, 179, 500)
        for level in range(5):
            codes = latlng2qdgc(lat, lng, level)
            centre_lat, centre_lng = qdgc2latlng(codes)
            self.assertEqual(latlng2qdgc(centre_lat, centre_lng, level), codes)
            for code, y, x in zip(codes, lat, lng):
                self.assertTrue(qdgc2polygon(code).covers(Point(x, y)))
            if level:
                self.assertEqual([qdgc_parent(code) for code in codes], latlng2qdgc(lat, lng, level - 1))
            if level <= 2:
                # Existing ABAP pentad IDs have two longitude-degree digits.
                selected = np.abs(lng) < 90
                self.assertEqual(pentad2qdgc(latlng2pentad(lat[selected], lng[selected]), level),
                                 np.asarray(codes)[selected].tolist())

    def test_pentads_tile_coarse_cells_exactly(self):
        for root in ['E036N00', 'E036S00', 'W000N00', 'W000S00']:
            for level in range(3):
                for code in qdgc_children(root, level):
                    pentads = qdgc2pentads(code)
                    self.assertEqual(len(pentads), 144 // 4**level)
                    self.assertEqual(len(set(pentads)), len(pentads))
                    self.assertEqual(set(pentad2qdgc(pentads, level)), {code})
                    cell = qdgc2polygon(code)
                    union = unary_union(pentad2polygon(pentads))
                    self.assertLess(cell.symmetric_difference(union).area, 1e-12)
        with self.assertRaises(ValueError):
            pentad2qdgc('0005c3600', level=3)
        with self.assertRaises(ValueError):
            qdgc2pentads('E036N00AAA')

    def test_hierarchy(self):
        self.assertEqual(qdgc_children('E033S02'), ['E033S02A', 'E033S02B', 'E033S02C', 'E033S02D'])
        self.assertEqual(qdgc_parent('E033S02BBCD', 1), 'E033S02B')
        self.assertEqual(qdgc_parent('E033S02BBCD'), 'E033S02BBC')
        root = qdgc2polygon('E033S02')
        children = qdgc2polygon(qdgc_children('E033S02', 2))
        self.assertTrue(root.equals(unary_union(children)))
        self.assertEqual(sum(p.area for p in children), root.area)
        with self.assertRaises(ValueError):
            qdgc_parent('E033S02')
        with self.assertRaises(ValueError):
            qdgc_children('E033S02A', 0)

    def test_kenya_website_square_examples(self):
        # Verified against local grid.json/map_data.json, joined by feature id.
        for label, code, bounds in [('51b', 'E037N00B', (37.5, 0.5, 38, 1)),
                                     ('63a', 'E037S00A', (37, -0.5, 37.5, 0))]:
            with self.subTest(square=label):
                self.assertEqual(qdgc2bounds(code), bounds)
                self.assertEqual(len(qdgc2pentads(code)), 36)

    def test_grid_overlap_and_return_types(self):
        for region in [box(36, 0, 37, 1), box(-1, -1, 0, 0)]:
            grid = generate_qdgc_grid(region, level=1, return_geojson=False)
            self.assertEqual(len(grid), 4)
            self.assertTrue(grid.qdgc.is_unique)
            self.assertEqual(grid.crs.to_epsg(), 4326)
            self.assertTrue(region.equals(unary_union(grid.geometry)))
        triangle = Point(36.5, 0.5).buffer(0.2)
        grid = generate_qdgc_grid(triangle, level=2, return_geojson=False)
        self.assertTrue(all(p.intersection(triangle).area > 0 for p in grid.geometry))
        self.assertEqual(len(json.loads(generate_qdgc_grid(region, level=1))['features']), 4)
        self.assertEqual(qdgc2polygon('E036N00A').geom_type, 'Polygon')
        self.assertEqual(len(qdgc2polygon(['E036N00A'])), 1)
        self.assertEqual(qdgc2latlng('E036N00A'), ([0.75], [36.25]))


if __name__ == '__main__':
    unittest.main()
