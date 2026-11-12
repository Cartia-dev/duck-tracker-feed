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
    def block(self, lat, lon, mm, frac=1.0):
        a = np.full((fs.ROWS, fs.COLS), np.nan, dtype=np.float32)
        r0 = int(round((fs.TOP - (lat + .5)) / fs.RES)); c0 = int(round(((lon - .5) - fs.LEFT) / fs.RES)); n = int(120 * frac ** .5)
        a[r0:r0 + n, c0:c0 + n] = mm
        return a

    def test_cell_mean_in_inches(self):
        m = fs.cell_means(self.block(44, -100, 254.0))
        i, j = int(44 - fs.GRID["lat0"]), int(-100 - fs.GRID["lon0"])
        self.assertAlmostEqual(float(m[i, j]), 10.0, places=3)
        self.assertEqual(int(np.isfinite(m).sum()), 1)

    def test_sparse_cell_is_no_data(self):
        m = fs.cell_means(self.block(44, -100, 254.0, frac=.1))
        self.assertTrue(np.isnan(m[int(44 - fs.GRID["lat0"]), int(-100 - fs.GRID["lon0"])]))

    def test_north_edge_is_clipped(self):
        a = np.full((fs.ROWS, fs.COLS), np.nan, dtype=np.float32)
        c0 = int(round(((-100 - .5) - fs.LEFT) / fs.RES)); a[0:45, c0:c0 + 120] = 25.4   # 52.875 N down to 52.5 N, 0.375 of the 53 N cell
        m = fs.cell_means(a)
        self.assertAlmostEqual(float(m[int(53 - fs.GRID["lat0"]), int(-100 - fs.GRID["lon0"])]), 1.0, places=3)
        self.assertTrue(np.isnan(m[int(54 - fs.GRID["lat0"]), int(-100 - fs.GRID["lon0"])]))


if __name__ == "__main__":
    unittest.main()
