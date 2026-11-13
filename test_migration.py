"""Offline tests for fetch_migration.py. Standard library only: python3 -m unittest test_migration"""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import fetch_migration as fm

A = {"ducks_total_millions": 33.439, "migratory_share": 0.70, "flyway_share": 0.45, "fall_flight_factor": 1.0, "season_start": "10-15", "season_end": "01-31",
     "duck_weight": [["10-15", 1.0]],
     "boundaries": [{"lat": 45, "sites": ["A"], "north_at_start": 1.0, "north_at_end": 0.0}, {"lat": 42, "sites": ["B"], "north_at_start": 1.0, "north_at_end": 0.0}]}
START = date(2026, 10, 15)
N = 109   # Oct 15 to Jan 31


def flat(v, n=N):
    return [float(v)] * n


class SeasonTests(unittest.TestCase):
    def test_season_runs_oct_15_to_jan_31(self):
        self.assertEqual(fm.season_dates(A, 2026), (date(2026, 10, 15), date(2027, 1, 31)))
        self.assertEqual((fm.season_dates(A, 2026)[1] - START).days + 1, N)

    def test_season_containing(self):
        self.assertEqual(fm.season_containing(A, date(2026, 11, 11))[0], date(2026, 10, 15))
        self.assertEqual(fm.season_containing(A, date(2027, 1, 10))[0], date(2026, 10, 15))   # after New Year it is still last October's season
        self.assertIsNone(fm.season_containing(A, date(2026, 8, 1)))
        self.assertIsNone(fm.season_containing(A, date(2027, 3, 1)))

    def test_duck_weight_is_linear_between_knots_and_flat_outside(self):
        knots = [["10-15", 0.2], ["11-14", 0.8], ["12-01", 1.0]]
        self.assertAlmostEqual(fm.weight_on(date(2026, 10, 10), START, knots), 0.2)
        self.assertAlmostEqual(fm.weight_on(date(2026, 10, 30), START, knots), 0.5)    # halfway from 0.2 to 0.8
        self.assertAlmostEqual(fm.weight_on(date(2027, 1, 20), START, knots), 1.0)


class GapTests(unittest.TestCase):
    def test_gaps_take_the_neighbours(self):
        out, filled, unfilled = fm.fill_series([10.0, None, 30.0, None, None])
        self.assertEqual(out, [10.0, 20.0, 30.0, 30.0, 30.0])   # inside: mean of both sides; at the end: the one side that has data
        self.assertEqual((filled, unfilled), (3, 0))

    def test_a_long_gap_is_zero_not_invented(self):
        out, filled, unfilled = fm.fill_series([5.0] + [None] * 10, window=3)
        self.assertEqual(out[1:4], [5.0, 5.0, 5.0])
        self.assertEqual(out[-1], 0.0)
        self.assertEqual(unfilled, 7)

    def test_boundary_series_averages_good_sites_only(self):
        nights = {"2026-10-15": {"A": {"status": "ok", "south_km": 100.0}, "B": {"status": "rain"}, "C": {"status": "ok", "south_km": 300.0}},
                  "2026-10-16": {"A": {"status": "no_data"}}}
        self.assertEqual(fm.boundary_series(nights, ["A", "B", "C"], [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)]), [200.0, None, None])


class ShareTests(unittest.TestCase):
    def test_zones_add_to_one_and_follow_the_lines(self):
        b = A["boundaries"]
        self.assertEqual(fm.shares_from_progress([0.0, 0.0], b), [1.0, 0.0, 0.0])      # nothing has crossed: all north of 45
        self.assertEqual(fm.shares_from_progress([1.0, 1.0], b), [0.0, 0.0, 1.0])      # everything crossed both lines
        z = fm.shares_from_progress([0.5, 0.2], b)
        self.assertAlmostEqual(sum(z), 1.0)
        self.assertAlmostEqual(z[0], 0.5)

    def test_a_southern_line_cannot_look_ahead_of_a_northern_one(self):
        z = fm.shares_from_progress([0.2, 0.9], A["boundaries"])    # claims 90% have crossed 42 N but only 20% crossed 45 N: impossible
        self.assertTrue(all(v >= -1e-9 for v in z))
        self.assertAlmostEqual(sum(z), 1.0)
        self.assertAlmostEqual(z[1], 0.0)                           # raised to match, so nothing is "between" the lines

    def test_wintering_north_of_a_line_is_kept(self):
        b = [{"lat": 36, "north_at_start": 1.0, "north_at_end": 0.3}]
        self.assertAlmostEqual(fm.shares_from_progress([1.0], b)[0], 0.3)   # even when every crossing bird has crossed, 30% never leave


class ComputeTests(unittest.TestCase):
    def run_compute(self, cur_rate, days=40, ref_rate=10.0):
        current = {"45": flat(cur_rate, days), "42": flat(cur_rate / 2, days)}
        reference = {"45": {"2024": flat(ref_rate), "2025": flat(ref_rate)}, "42": {"2024": flat(ref_rate / 2), "2025": flat(ref_rate / 2)}}
        return fm.compute(A, current, reference, START, START + timedelta(days=days - 1))

    def test_a_normal_year_is_on_schedule(self):
        out = self.run_compute(10.0)
        b45 = out["boundaries"][0]
        self.assertAlmostEqual(b45["progress"], 40 / N, places=3)         # 40 nights of a normal 109-night total
        self.assertAlmostEqual(b45["normal_progress"], 40 / N, places=3)
        self.assertAlmostEqual(b45["days_ahead"], 0.0, delta=1.0)
        self.assertEqual(out["pool_millions"], round(33.439 * 0.7 * 0.45, 2))
        self.assertAlmostEqual(sum(z["share"] for z in out["zones"]), 1.0, places=3)
        self.assertEqual(len(out["history"]), 40)

    def test_a_heavy_year_is_ahead(self):
        out = self.run_compute(20.0)                                       # twice the normal rate for 40 nights = what a normal year does in 80
        b45 = out["boundaries"][0]
        self.assertAlmostEqual(b45["progress"], 80 / N, places=3)
        self.assertGreater(b45["days_ahead"], 30)
        self.assertLess(out["zones"][0]["share"], out["zones"][0]["normal_share"])   # less is still north than in a normal year

    def test_progress_stops_at_one(self):
        out = self.run_compute(1000.0)
        self.assertEqual(out["boundaries"][0]["progress"], 1.0)
        self.assertAlmostEqual(out["zones"][-1]["share"], 1.0, places=3)

    def test_a_reference_with_no_southward_passage_is_refused(self):
        with self.assertRaises(ValueError):
            fm.compute(A, {"45": flat(10, 5), "42": flat(5, 5)}, {"45": {"2024": flat(0)}, "42": {"2024": flat(5)}}, START, date(2026, 10, 19))

    def test_earlier_nights_count_for_less_when_songbirds_dominate(self):
        a = dict(A, duck_weight=[["10-15", 0.1], ["11-30", 1.0]])
        current = {"45": flat(10, 20), "42": flat(5, 20)}
        reference = {"45": {"2024": flat(10)}, "42": {"2024": flat(5)}}
        weighted = fm.compute(a, current, reference, START, date(2026, 11, 3))["boundaries"][0]["progress"]
        flat_w = fm.compute(A, current, reference, START, date(2026, 11, 3))["boundaries"][0]["progress"]
        self.assertLess(weighted, flat_w)


class JobTests(unittest.TestCase):
    def test_off_season_writes_a_note_and_fetches_nothing(self):
        with mock.patch.object(fm.fb, "build", side_effect=AssertionError("must not fetch")):
            doc = fm.build_current(A, {"years": [], "boundaries": {}}, now=datetime(2026, 7, 1, 12, tzinfo=timezone.utc))
        self.assertEqual(doc["status"], "off_season")

    def test_stored_records_are_trimmed_but_reusable(self):
        t = fm.trim({"2026-10-15": {"KNQA": {"status": "ok", "south_km": 5.0, "rain_check": "BirdCast gaps and weather", "colden_mean": 3.0, "dir_deg": 90}}})
        self.assertEqual(t["2026-10-15"]["KNQA"], {"status": "ok", "south_km": 5.0, "rain_check": "BirdCast gaps and weather"})


if __name__ == "__main__":
    unittest.main()
