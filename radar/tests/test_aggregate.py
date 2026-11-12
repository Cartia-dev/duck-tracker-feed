import json
import tempfile
import unittest
from datetime import date

from nexrad_pipeline import aggregate
from nexrad_pipeline.s3 import parse_key, thin
from nexrad_pipeline.sites import load_config, load_sites


def vol(colden, mtr, u, v):
    return dict(colden=colden, mtr=mtr, u_bar=u, v_bar=v, mean_h=900.0, precip_frac=0.02)


class AggregateTests(unittest.TestCase):
    def test_night_record(self):
        rec = aggregate.aggregate_night([vol(100, 1000, -5, -12), vol(300, 3000, -5, -12)], 3, hours_span=8)
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["n_failed"], 1)
        self.assertAlmostEqual(rec["mtr_mean"], 2000.0)
        self.assertAlmostEqual(rec["dir_deg"], 202.6, delta=0.1)
        self.assertAlmostEqual(rec["speed_ms"], 13.0, delta=0.01)
        self.assertAlmostEqual(rec["dir_r"], 1.0, places=3)

    def test_opposing_nights_have_low_concentration(self):
        rec = aggregate.aggregate_night([vol(100, 500, 10, 0), vol(100, 500, -10, 0)], 2)
        self.assertLess(rec["dir_r"], 0.05)

    def test_empty_night(self):
        self.assertEqual(aggregate.aggregate_night([], 0)["status"], "no_data")

    def test_export_roundtrip(self):
        sites = load_sites()
        with tempfile.TemporaryDirectory() as d:
            aggregate.write_record(d, date(2026, 11, 9), "KLZK", aggregate.aggregate_night([vol(50, 800, -4, -10)], 1))
            out = aggregate.export(d, sites, date(2026, 11, 9), 10, ["AR"])
            self.assertEqual(list(out["nights"]), ["2026-11-09"])
            self.assertIn("KLZK", out["nights"]["2026-11-09"])
            json.dumps(out)


class S3NameTests(unittest.TestCase):
    def test_parse(self):
        f = parse_key("2026/11/09/KLZK/KLZK20261109_031512_V06")
        self.assertEqual((f.site, f.time.hour, f.time.minute), ("KLZK", 3, 15))
        self.assertIsNone(parse_key("2026/11/09/KLZK/KLZK20261109_031512_V06_MDM"))

    def test_thin(self):
        keys = [f"2026/11/09/KLZK/KLZK20261109_{h:02d}{m:02d}00_V06" for h in range(0, 3) for m in range(0, 60, 6)]
        files = [parse_key(k) for k in keys]
        self.assertEqual(len(thin(files, 60)), 3)


class SiteConfigTests(unittest.TestCase):
    def test_every_flyway_state_has_a_site(self):
        cfg = load_config()
        have = {s["state"] for s in cfg["sites"]}
        self.assertEqual(have, set(cfg["states"]))
        ids = [s["id"] for s in cfg["sites"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_sites_are_inside_the_states_bounding_box(self):
        for s in load_sites():
            self.assertTrue(28.5 < s.lat < 49.5 and -98 < s.lon < -80, s.id)


if __name__ == "__main__":
    unittest.main()
