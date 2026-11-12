import math
import unittest

import numpy as np

from nexrad_pipeline.physics import (Params, beam_height, dbz_to_eta, fit_vvp, nan_uniform)
from nexrad_pipeline.volume import process_radar
from tests.fake_radar import make_radar


class PhysicsTests(unittest.TestCase):
    def test_eta_at_zero_dbz(self):
        self.assertAlmostEqual(float(dbz_to_eta(0.0, 10.7)), 21.7, delta=0.2)

    def test_eta_scales_10db_is_10x(self):
        self.assertAlmostEqual(float(dbz_to_eta(10.0) / dbz_to_eta(0.0)), 10.0, places=6)

    def test_beam_height_reasonable(self):
        # 0.5 deg beam at 35 km is a bit under 400 m up; 4.5 deg at 35 km is about 2.8 km.
        self.assertAlmostEqual(float(beam_height(35000.0, 0.5)), 380.0, delta=40.0)
        self.assertAlmostEqual(float(beam_height(35000.0, 4.5)), 2800.0, delta=150.0)

    def test_nan_uniform_ignores_nan(self):
        a = np.full((9, 9), 2.0); a[4, 4] = np.nan
        out = nan_uniform(a, 3)
        self.assertTrue(np.allclose(out[np.isfinite(out)], 2.0))

    def test_vvp_recovers_wind(self):
        rng = np.random.default_rng(1)
        az = rng.uniform(0, 360, 2000); el = np.full(2000, 2.0)
        u, v, w = 7.0, -9.0, 0.2
        a, e = np.radians(az), np.radians(el)
        vr = u * np.sin(a) * np.cos(e) + v * np.cos(a) * np.cos(e) + w * np.sin(e) + rng.normal(0, 1.0, az.size)
        fit = fit_vvp(vr, az, el, Params())
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit[0], u, delta=0.2)
        self.assertAlmostEqual(fit[1], v, delta=0.2)
        self.assertAlmostEqual(fit[3], 1.0, delta=0.1)

    def test_vvp_needs_azimuth_coverage(self):
        rng = np.random.default_rng(2)
        az = rng.uniform(0, 120, 500); el = np.full(500, 2.0)
        self.assertIsNone(fit_vvp(rng.normal(size=500), az, el, Params()))


class VolumeTests(unittest.TestCase):
    def test_recovers_birds_density_speed_direction(self):
        out = process_radar(make_radar(u=-5.0, v=-12.0, dbz=10.0, noise=1.5))
        expected_dens = float(dbz_to_eta(10.0)) / 11.0           # about 19.7 birds/km^3
        self.assertAlmostEqual(out["mtr"] / (out["colden"] * 3.6 * math.hypot(5, 12)), 1.0, delta=0.03)
        speed = math.hypot(out["u_bar"], out["v_bar"])
        self.assertAlmostEqual(speed, 13.0, delta=0.4)
        direction = (math.degrees(math.atan2(out["u_bar"], out["v_bar"])) + 360) % 360
        self.assertAlmostEqual(direction, 202.6, delta=2.0)       # moving toward SSW
        # column density is density times the thickness of the covered layers
        self.assertAlmostEqual(out["colden"] / expected_dens, out["n_layers"] * 0.2, delta=0.1)
        self.assertGreater(out["n_layers"], 6)

    def test_split_cuts_are_paired(self):
        # Real low cuts: a surveillance sweep (rho, no velocity) and a Doppler sweep (velocity, no rho) at each elevation.
        joined = process_radar(make_radar(u=-5.0, v=-12.0, seed=5))
        split = process_radar(make_radar(u=-5.0, v=-12.0, seed=5, split=True))
        self.assertEqual(split["sweeps_paired"], 5)
        self.assertEqual(split["sweeps_skipped"], 0)
        self.assertAlmostEqual(split["colden"], joined["colden"], delta=0.1 * joined["colden"])
        self.assertAlmostEqual(math.hypot(split["u_bar"], split["v_bar"]), 13.0, delta=0.5)
        self.assertAlmostEqual(split["n_velocity_gates"] / joined["n_velocity_gates"], 1.0, delta=0.05)

    def test_split_cut_rain_is_screened_with_the_partner_rho(self):
        clean = process_radar(make_radar(seed=6, split=True))
        rainy = process_radar(make_radar(seed=6, split=True, rain_az=(0, 120)))
        self.assertLess(rainy["n_velocity_gates"], 0.75 * clean["n_velocity_gates"])
        self.assertAlmostEqual(math.hypot(rainy["u_bar"], rainy["v_bar"]), 13.0, delta=1.0)

    def test_doppler_sweep_without_partner_is_skipped(self):
        radar = make_radar(seed=7, split=True, elevs=(0.5, 1.5))
        radar.fixed_angle["data"][1] = 3.0   # the Doppler sweep no longer matches the surveillance sweep's elevation
        out = process_radar(radar)
        self.assertGreaterEqual(out["sweeps_skipped"], 1)

    def test_rain_sector_is_filtered_out(self):
        clean = process_radar(make_radar(seed=3))
        rainy = process_radar(make_radar(seed=3, rain_az=(60, 200)))
        self.assertGreater(rainy["precip_frac"], 0.2)
        self.assertAlmostEqual(math.hypot(rainy["u_bar"], rainy["v_bar"]), math.hypot(clean["u_bar"], clean["v_bar"]), delta=0.5)
        d_clean = (math.degrees(math.atan2(clean["u_bar"], clean["v_bar"])) + 360) % 360
        d_rainy = (math.degrees(math.atan2(rainy["u_bar"], rainy["v_bar"])) + 360) % 360
        self.assertAlmostEqual(d_clean, d_rainy, delta=3.0)

    def test_quiet_velocity_field_is_not_birds(self):
        out = process_radar(make_radar(noise=0.3))   # residual below the 1 m/s threshold
        self.assertEqual(out["colden"], 0.0)

    def test_no_dualpol_raises(self):
        with self.assertRaises(ValueError):
            process_radar(make_radar(no_rho=True))


if __name__ == "__main__":
    unittest.main()
