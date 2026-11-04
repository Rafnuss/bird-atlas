"""Shared API regression checks for both naming systems and legacy callers."""

import unittest

import numpy as np
from shapely.geometry import box

from bird_atlas import (
    cell2bounds, cell2latlng, cell2polygon, generate_grid, generate_pentad_grid,
    generate_qdgc_grid, latlng2cell, latlng2pentad, latlng2polygon, latlng2qdgc,
    pentad2latlng, pentad2polygon, qdgc2bounds, qdgc2latlng, qdgc2polygon,
)
from bird_atlas.pentad import pentad2polygon as legacy_polygon


class GridTests(unittest.TestCase):
    def test_mixed_ids_preserve_their_geometry(self):
        cells = ['3355_1825', '0955c0850', '1445b1730', 'E019S31BD', 'W010N10A']
        for cell, bounds in zip(cells, cell2bounds(cells)):
            with self.subTest(cell=cell):
                self.assertEqual(cell2bounds(cell), bounds)
                self.assertEqual(cell2polygon(cell).bounds, bounds)
                lat, lng = cell2latlng(cell)
                encoded = latlng2cell(lat, lng, len(cell) - 7 if cell[0] in 'EW' else None)
                self.assertEqual(encoded, [cell])

    def test_existing_names_are_the_same_functions(self):
        self.assertIs(pentad2latlng, cell2latlng)
        self.assertIs(qdgc2latlng, cell2latlng)
        self.assertIs(pentad2polygon, cell2polygon)
        self.assertIs(qdgc2polygon, cell2polygon)
        self.assertIs(qdgc2bounds, cell2bounds)
        self.assertIs(legacy_polygon, cell2polygon)

    def test_coordinate_and_polygon_api_at_both_resolutions(self):
        for level in [None, 0, 1, 2, 3]:
            lat, lng = [0, -0.5, 4.75], [0, -0.5, 37.75]
            codes = latlng2cell(lat, lng, level)
            expected = latlng2pentad(lat, lng) if level is None else latlng2qdgc(lat, lng, level)
            self.assertEqual(codes, expected)
            polygons = latlng2polygon(lat, lng, level)
            for code, polygon in zip(codes, polygons):
                self.assertTrue(polygon.equals(cell2polygon(code)))
            self.assertTrue(latlng2polygon(lat[0], lng[0], level).equals(polygons[0]))

    def test_one_grid_generator_preserves_legacy_outputs(self):
        region = box(36, 0, 36.5, 0.5)
        pentads = generate_grid(region, return_geojson=False)
        self.assertEqual(len(pentads), 36)
        self.assertTrue(pentads.equals(generate_pentad_grid(region, False)))
        for level in range(1, 4):
            grid = generate_grid(region, level, False)
            self.assertTrue(grid.equals(generate_qdgc_grid(region, level, False)))
            self.assertEqual(len(grid), 4**(level - 1))
        np.testing.assert_allclose(sum(p.area for p in pentads.geometry), region.area)


if __name__ == '__main__':
    unittest.main()
