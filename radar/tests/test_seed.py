import json
import tempfile
import unittest
from argparse import Namespace
from datetime import date, datetime, timezone
from pathlib import Path

from nexrad_pipeline import aggregate, cli
from nexrad_pipeline.sites import Site


class SeedTests(unittest.TestCase):
    def test_export_then_seed_round_trips_ok_nights_only(self):
        night = date(2026, 11, 10)
        sites = [Site("KNQA", "Memphis", "TN", 35.34, -89.87), Site("KLIX", "Slidell", "LA", 30.33, -89.83)]
        ok = aggregate.aggregate_night([dict(colden=20, mtr=400, u_bar=3, v_bar=0.5, mean_h=600.0, precip_frac=0.1)], 2, hours_span=10)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            aggregate.write_record(a, night, "KNQA", ok)
            aggregate.write_record(a, night, "KLIX", aggregate.aggregate_night([], 0))   # no_data
            exported = aggregate.export(a, sites, night, 1, ["TN"])
            src = Path(a) / "nightly.json"
            src.write_text(json.dumps(exported))
            cli.cmd_seed(Namespace(src=str(src), out=b))
            self.assertEqual(aggregate.read_record(b, night, "KNQA"), ok)
            self.assertIsNone(aggregate.read_record(b, night, "KLIX"))   # retried on the next run

    def test_refuses_synthetic_and_foreign_files(self):
        with tempfile.TemporaryDirectory() as a:
            src = Path(a) / "x.json"
            src.write_text(json.dumps({"schema": aggregate.SCHEMA, "synthetic": True, "nights": {}}))
            with self.assertRaises(SystemExit):
                cli.cmd_seed(Namespace(src=str(src), out=a))
            src.write_text(json.dumps({"schema": "other"}))
            with self.assertRaises(SystemExit):
                cli.cmd_seed(Namespace(src=str(src), out=a))

    def test_latest_complete_night_at_any_hour(self):
        at = lambda d, h: datetime(2026, 11, d, h, 0, tzinfo=timezone.utc)
        self.assertEqual(cli.latest_complete_night(at(12, 10)), date(2026, 11, 10))   # the night of the 11th is still going
        self.assertEqual(cli.latest_complete_night(at(12, 13)), date(2026, 11, 11))
        self.assertEqual(cli.latest_complete_night(at(12, 15)), date(2026, 11, 11))   # the scheduled run
        self.assertEqual(cli.latest_complete_night(at(12, 22)), date(2026, 11, 11))   # a manual evening run
        self.assertEqual(cli.latest_complete_night(at(13, 0)), date(2026, 11, 11))


if __name__ == "__main__":
    unittest.main()
