"""Offline tests for fetch_birdcast.py. Standard library only: python3 -m unittest test_birdcast"""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import fetch_birdcast as fb


def csv_profile(t, layers, radar_h=133):
    """BirdCast-style rows for one profile. layers: [(height, dens, ff, u, v)] with None for NaN."""
    f = lambda x: "" if x is None else str(x)
    return "".join(f"KNQA,{t},{h},sea,{f(u)},{f(v)},0,{f(ff)},0,1,FALSE,0,{f(d)},0,0,0,0,0,0,11,1,35,35.3,-89.8,{radar_h},10.7,x\n" for h, d, ff, u, v in layers)


HEAD = "radar,datetime,height,height_reference,u,v,w,ff,dd,sd_vvp,gap,eta,dens,dbz,dbz_all,n,n_dbz,n_all,n_dbz_all,rcs,sd_vvp_threshold,vcp,radar_latitude,radar_longitude,radar_height,radar_wavelength,source_file\n"
BIRDS = [(200 + 100 * i, 20.0, 10.0, 6.0, 8.0) for i in range(6)]   # 6 layers, 20 birds/km^3, 10 m/s toward 37 degrees


class BirdCastTests(unittest.TestCase):
    def test_summarise_a_profile(self):
        p = fb.parse_profiles(HEAD + csv_profile("2026-11-11T03:00:00Z", BIRDS))[0]
        s = fb.summarise(p)
        self.assertAlmostEqual(s["colden"], 12.0)                  # 6 layers x 20 birds/km^3 x 0.1 km
        self.assertAlmostEqual(s["mtr"], 20 * 10 * 3.6 * 0.6)       # birds/km/h
        self.assertAlmostEqual(s["u_bar"], 6.0)
        self.assertAlmostEqual(s["mean_h"], (sum(h for h, *_ in BIRDS) / 6) - 133)   # above the radar, not sea level

    def test_southward_flux_counts_net_movement_only(self):
        south = [(200 + 100 * i, 20.0, 10.0, 0.0, -10.0) for i in range(6)]    # heading due south at 10 m/s: v = -10
        north = [(200 + 100 * i, 20.0, 10.0, 0.0, 10.0) for i in range(6)]
        s = fb.summarise(fb.parse_profiles(HEAD + csv_profile("2026-11-11T03:00:00Z", south))[0])
        n = fb.summarise(fb.parse_profiles(HEAD + csv_profile("2026-11-11T03:00:00Z", north))[0])
        self.assertAlmostEqual(s["south"], 20 * 10 * 3.6 * 0.6)    # 432 birds/km/h southward
        self.assertAlmostEqual(n["south"], -432.0)                  # northward movement is negative
        mixed = fb.summarise(fb.parse_profiles(HEAD + csv_profile("2026-11-11T03:00:00Z", south[:3] + north[3:]))[0])
        self.assertAlmostEqual(mixed["south"], 0.0)                 # equal numbers each way cancel

    def test_night_record_carries_the_passage_per_km(self):
        ok = [dict(colden=10.0, mtr=300.0, south=200.0, u_bar=0.0, v_bar=-5.0, mean_h=400.0)] * 8
        rec = fb.aggregate_night(ok, 8, 0, 10.0)
        self.assertEqual((rec["south_mean"], rec["south_km"]), (200.0, 2000.0))    # 200 birds/km/h for 10 hours

    def test_layers_without_a_velocity_fit_do_not_count(self):
        gap = [(h, d, None, None, None) for h, d, *_ in BIRDS]
        p = fb.parse_profiles(HEAD + csv_profile("2026-11-11T03:00:00Z", gap))[0]
        self.assertIsNone(fb.summarise(p))                         # density but no velocity: too little to say anything
        two = BIRDS[:2] + [(h, d, None, None, None) for h, d, *_ in BIRDS[2:]]
        self.assertIsNone(fb.summarise(fb.parse_profiles(HEAD + csv_profile("2026-11-11T03:00:00Z", two))[0]))

    def test_older_files_with_milliseconds_parse(self):
        ps = fb.parse_profiles(HEAD + csv_profile("2025-01-01T00:02:00.000Z", BIRDS))
        self.assertEqual(ps[0]["t"], datetime(2025, 1, 1, 0, 2, tzinfo=timezone.utc))

    def test_sites_load_from_every_flyway_file_with_a_tag(self):
        cfg = fb.load_sites()
        by = {}
        for s in cfg["sites"]:
            by.setdefault(s["flyway"], []).append(s["id"])
        self.assertEqual(sorted(by), ["central", "mississippi"])
        self.assertEqual(len(cfg["sites"]), len({s["id"] for s in cfg["sites"]}))   # no radar twice
        self.assertIn("KNQA", by["mississippi"])
        self.assertIn("KTLX", by["central"])
        self.assertTrue({"TX", "ND", "AR"} <= set(cfg["states"]))
        self.assertTrue(all("lat" in s and "lon" in s and "name" in s for s in cfg["sites"]))

    def test_parse_groups_rows_by_time(self):
        text = HEAD + csv_profile("2026-11-11T03:00:00Z", BIRDS) + csv_profile("2026-11-11T03:10:00Z", BIRDS[:3])
        ps = fb.parse_profiles(text)
        self.assertEqual([len(p["layers"]) for p in ps], [6, 3])
        self.assertEqual(ps[1]["t"], datetime(2026, 11, 11, 3, 10, tzinfo=timezone.utc))

    def test_night_record_and_rain_rules(self):
        ok = [dict(colden=10.0, mtr=300.0, south=0.0, u_bar=3.0, v_bar=0.0, mean_h=400.0)] * 8
        rec = fb.aggregate_night(ok, 10, 0, 9.0)
        self.assertEqual((rec["status"], rec["n_failed"], rec["dir_deg"], rec["mtr_mean"]), ("ok", 2, 90.0, 300.0))
        self.assertEqual(fb.aggregate_night(ok[:4], 10, 6, 9.0)["status"], "rain")          # more than half left out for rain
        self.assertEqual(fb.aggregate_night(ok[:3], 10, 0, 9.0)["status"], "no_data")        # mostly gaps
        self.assertEqual(fb.aggregate_night([], 0, 0, 0.0)["status"], "no_data")
        self.assertEqual(fb.aggregate_night(ok[:5], 10, 5, 9.0)["status"], "ok")             # exactly half is kept
        self.assertNotIn("mtr_mean", fb.aggregate_night(ok[:4], 10, 6, 9.0))

    def test_wet_hours_use_a_buffer(self):
        t = datetime(2026, 11, 1, 3, 46, tzinfo=timezone.utc)
        wet = lambda s: [datetime.fromisoformat(s).replace(tzinfo=timezone.utc)]
        self.assertTrue(fb.is_wet(t, wet("2026-11-01T05:00")))
        self.assertTrue(fb.is_wet(t, wet("2026-11-01T03:00")))
        self.assertFalse(fb.is_wet(t, wet("2026-11-01T06:00")))
        self.assertFalse(fb.is_wet(t, None))

    def test_latest_complete_night_follows_the_publish_time(self):
        at = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)
        self.assertEqual(fb.latest_complete_night(at(13, 1, 10)), date(2026, 11, 11))    # the Nov 12 file is out at 01:05 UTC on the 13th
        self.assertEqual(fb.latest_complete_night(at(13, 1, 0)), date(2026, 11, 10))     # not yet
        self.assertEqual(fb.latest_complete_night(at(12, 15, 30)), date(2026, 11, 10))   # mid-day: last night is not there yet

    def test_site_nights_end_to_end(self):
        site = {"id": "KNQA", "lat": 35.3447, "lon": -89.8734}
        quiet = csv_profile("2026-11-11T02:00:00Z", BIRDS) + csv_profile("2026-11-11T03:00:00Z", BIRDS) + csv_profile("2026-11-11T04:00:00Z", BIRDS)
        day = csv_profile("2026-11-11T18:00:00Z", BIRDS)   # mid-day: sun up, not part of any night
        texts = {"20261110": None, "20261111": HEAD + day + quiet}
        recs = fb.site_nights(site, [date(2026, 11, 10)], [], texts)["2026-11-10"]
        self.assertEqual((recs["status"], recs["n_attempted"], recs["n_ok"]), ("ok", 3, 3))
        wet = [datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)]   # the wet hour covers all three profiles
        self.assertEqual(fb.site_nights(site, [date(2026, 11, 10)], wet, texts)["2026-11-10"]["status"], "rain")
        self.assertEqual(fb.site_nights(site, [date(2026, 11, 10)], None, texts)["2026-11-10"]["rain_check"], "BirdCast gaps only")


class WeatherFailureTests(unittest.TestCase):
    def test_no_weather_record_means_no_file(self):
        with mock.patch.object(fb, "fetch_wet_hours", side_effect=OSError("timed out")), mock.patch.object(fb, "get_day", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                fb.build(n_nights=2, end=date(2026, 11, 10))
        self.assertIn("not rain-screened", str(cm.exception))

    def test_no_weather_on_purpose_is_allowed_and_marked(self):
        with mock.patch.object(fb, "get_day", return_value=None):
            doc = fb.build(n_nights=2, end=date(2026, 11, 10), weather=False)
        rec = next(iter(doc["nights"]["2026-11-10"].values()))
        self.assertEqual(rec["rain_check"], "BirdCast gaps only")

    def test_old_nights_are_kept_and_only_the_new_ones_are_fetched(self):
        ids = [x["id"] for x in fb.load_sites()["sites"]]
        night = lambda check: {i: {"status": "ok", "rain_check": check, "mtr_mean": 123.0, "south_km": 1000.0} for i in ids}
        prev = {"record_version": fb.VERSION, "source": fb.SOURCE, "nights": {"2026-11-05": night("BirdCast gaps and weather"), "2026-11-04": night("BirdCast gaps only")}}
        asked = []
        with mock.patch.object(fb, "fetch_wet_hours", side_effect=lambda sites, first, last=None, **k: asked.append(first) or {}), mock.patch.object(fb, "get_day", return_value=None):
            doc = fb.build(n_nights=3, end=date(2026, 11, 7), reuse=prev)   # nights Nov 7 and 6 are always re-read; Nov 5 is old enough to keep
        self.assertEqual(doc["nights"]["2026-11-05"]["KNQA"]["mtr_mean"], 123.0)
        self.assertEqual(len(asked), 1)
        self.assertGreaterEqual(asked[0].date(), date(2026, 11, 5))   # the weather request starts at the first night that needs it, not at the kept one
        self.assertEqual(doc["nights"]["2026-11-07"]["KNQA"]["status"], "no_data")   # no BirdCast file in this test


    def test_a_night_that_skipped_the_weather_check_is_not_kept(self):
        ids = [x["id"] for x in fb.load_sites()["sites"]]
        prev = {"record_version": fb.VERSION, "source": fb.SOURCE, "nights": {"2026-11-04": {i: {"status": "ok", "rain_check": "BirdCast gaps only", "mtr_mean": 999.0, "south_km": 1.0} for i in ids}}}
        with mock.patch.object(fb, "fetch_wet_hours", return_value={}), mock.patch.object(fb, "get_day", return_value=None):
            doc = fb.build(n_nights=1, end=date(2026, 11, 4) + timedelta(days=0), reuse=prev)
        self.assertNotEqual(doc["nights"]["2026-11-04"]["KNQA"].get("mtr_mean"), 999.0)

    def test_a_partly_missing_night_is_not_kept(self):
        prev = {"record_version": fb.VERSION, "source": fb.SOURCE, "nights": {"2026-11-03": {"KNQA": {"status": "ok", "rain_check": "BirdCast gaps and weather", "mtr_mean": 999.0, "south_km": 1.0}}}}
        with mock.patch.object(fb, "fetch_wet_hours", return_value={}), mock.patch.object(fb, "get_day", return_value=None):
            doc = fb.build(n_nights=3, end=date(2026, 11, 5), reuse=prev)
        self.assertNotEqual(doc["nights"]["2026-11-03"]["KNQA"].get("mtr_mean"), 999.0)


if __name__ == "__main__":
    unittest.main()
