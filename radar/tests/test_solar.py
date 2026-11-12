import unittest
from datetime import datetime, timezone, date

from nexrad_pipeline.solar import julian_day, solar_elevation, night_id


class SolarTests(unittest.TestCase):
    def test_julian_day_j2000(self):
        self.assertAlmostEqual(julian_day(datetime(2000, 1, 1, 12, tzinfo=timezone.utc)), 2451545.0, places=6)

    def test_noon_elevation_arkansas_november(self):
        # Declination about -17 deg, latitude 34.8: noon elevation about 38 deg. Solar noon near 17:53 UTC.
        e = solar_elevation(datetime(2026, 11, 9, 17, 53, tzinfo=timezone.utc), 34.8, -92.3)
        self.assertAlmostEqual(e, 38.2, delta=1.0)

    def test_midnight_is_dark(self):
        self.assertLess(solar_elevation(datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc), 34.8, -92.3), -40)

    def test_night_id_groups_whole_night(self):
        lon = -92.3
        evening = datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)   # 17:30 local
        small_hours = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
        dawn = datetime(2026, 11, 10, 12, 30, tzinfo=timezone.utc)
        next_evening = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc)
        self.assertEqual({night_id(t, lon) for t in (evening, small_hours, dawn)}, {date(2026, 11, 9)})
        self.assertEqual(night_id(next_evening, lon), date(2026, 11, 10))


if __name__ == "__main__":
    unittest.main()
