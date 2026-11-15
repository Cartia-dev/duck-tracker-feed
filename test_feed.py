"""Offline tests for the feed jobs: python3 -m unittest (from this folder; fetch_snow needs numpy)."""
import unittest
from datetime import datetime, timezone

import numpy as np

import fetch_fronts as ff
import fetch_snow as fs

BULLETIN = """FSUS02 KWBC 121818
CODSRP

CODED SURFACE FRONTAL POSITIONS FORECAST
NWS WEATHER PREDICTION CENTER COLLEGE PARK MD
118 PM EST THU NOV 12 2026

12HR PROG VALID 130600Z
HIGHS 1020 38107
COLD 3681 3385 3289 3193 35101 38103
TROF 5582 5376 5271
WARM 4168 4167 4164
COLD STG 30120 29120 27121
COLD 1010 1011

24HR PROG VALID 131800Z
COLD 45122 41123 37124
$$
"""


class FrontsTest(unittest.TestCase):
    def test_parse_pairs(self):
        self.assertEqual(ff.parse_pairs(["3681", "35101", "WK", "9999999"]), [[-81.0, 36.0], [-101.0, 35.0]])

    def test_bulletin(self):
        issued, frames = ff.parse(BULLETIN, "forecast")
        self.assertEqual(issued, datetime(2026, 11, 12, 18, 18, tzinfo=timezone.utc))
        self.assertEqual([f["valid_utc"] for f in frames], ["2026-11-13T06:00:00Z", "2026-11-13T18:00:00Z"])
        first = frames[0]["fronts"]
        self.assertEqual([f["type"] for f in first], ["cold", "trough", "warm", "cold"])   # the far-off 10N 10W front is dropped
        self.assertEqual(first[0]["pts"][0], [-81.0, 36.0])

    def test_day_of_month_across_month_end(self):
        ref = datetime(2026, 12, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(ff.nearest_date(30, 18, 0, ref), datetime(2026, 11, 30, 18, tzinfo=timezone.utc))


class SnowTest(unittest.TestCase):
    def idx(self, lat, lon):
        return int(round((lat - fs.GRID["lat0"]) / fs.GRID["d"])), int(round((lon - fs.GRID["lon0"]) / fs.GRID["d"]))

    def block(self, lat, lon, mm, frac=1.0):
        """SNODAS pixels filled over (a fraction of) the cell centred on lat, lon."""
        a = np.full((fs.ROWS, fs.COLS), np.nan, dtype=np.float32)
        half, per = fs.GRID["d"] / 2, int(round(fs.GRID["d"] / fs.RES))
        r0 = int(round((fs.TOP - (lat + half)) / fs.RES)); c0 = int(round(((lon - half) - fs.LEFT) / fs.RES)); n = int(per * frac ** .5)
        a[r0:r0 + n, c0:c0 + n] = mm
        return a

    def test_grid_is_half_degree_over_the_same_area(self):
        G = fs.GRID
        self.assertEqual((G["d"], G["lat0"], G["lon0"]), (0.5, 30.0, -115.0))
        self.assertEqual((G["lat0"] + (G["nlat"] - 1) * G["d"], G["lon0"] + (G["nlon"] - 1) * G["d"]), (55.0, -65.0))

    def test_cell_mean_in_inches(self):
        m = fs.cell_means(self.block(44, -100, 254.0))
        self.assertAlmostEqual(float(m[self.idx(44, -100)]), 10.0, places=3)
        self.assertEqual(int(np.isfinite(m).sum()), 1)          # a filled cell does not leak into its neighbours

    def test_sparse_cell_is_no_data(self):
        m = fs.cell_means(self.block(44, -100, 254.0, frac=.1))
        self.assertTrue(np.isnan(m[self.idx(44, -100)]))

    def test_north_edge_is_clipped(self):
        a = np.full((fs.ROWS, fs.COLS), np.nan, dtype=np.float32)
        c0 = int(round(((-100 - fs.GRID["d"] / 2) - fs.LEFT) / fs.RES)); a[0:45, c0:c0 + 60] = 25.4   # 52.875 N down to 52.5 N in the -100 column
        m = fs.cell_means(a)
        self.assertAlmostEqual(float(m[self.idx(53, -100)]), 1.0, places=3)     # a quarter of the 53 N cell has data: enough
        self.assertTrue(np.isnan(m[self.idx(53.5, -100)]))                      # wholly north of SNODAS

    def test_model_change_is_blended_between_the_one_degree_points(self):
        n = fs.MODEL_EVERY
        coarse = np.full((len(range(0, fs.GRID["nlat"], n)), len(range(0, fs.GRID["nlon"], n))), np.nan, dtype=np.float32)
        coarse[10, 20], coarse[10, 21], coarse[11, 20], coarse[11, 21] = 4.0, 0.0, 2.0, 2.0
        out = fs.spread_change(coarse)
        self.assertEqual(out.shape, (fs.GRID["nlat"], fs.GRID["nlon"]))
        self.assertAlmostEqual(float(out[20, 40]), 4.0)          # on a model point: its own change
        self.assertAlmostEqual(float(out[20, 41]), 2.0)          # halfway to a point with no change
        self.assertAlmostEqual(float(out[21, 41]), 2.0)          # centre of the four
        self.assertEqual(float(out[0, 0]), 0.0)                  # no model point near: no change


if __name__ == "__main__":
    unittest.main()
