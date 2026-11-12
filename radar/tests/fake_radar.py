"""Synthetic Py-ART-shaped radar volumes with a known answer, for tests."""
import numpy as np


class FakeRadar:
    def __init__(self, ranges, azimuth, elevation, fixed_angle, fields, rays_per_sweep):
        self.range = {"data": ranges}
        self.azimuth = {"data": azimuth}
        self.elevation = {"data": elevation}
        self.fixed_angle = {"data": fixed_angle}
        self.fields = {k: {"data": v} for k, v in fields.items()}
        self.nsweeps = len(fixed_angle)
        self._n = rays_per_sweep

    def get_slice(self, i):
        return slice(i * self._n, (i + 1) * self._n)


def make_radar(u=-5.0, v=-12.0, w=0.3, dbz=10.0, rho=0.80, noise=1.5, elevs=(0.5, 1.5, 2.5, 3.5, 4.5),
               rain_az=None, rain_dbz=28.0, rain_wind=(12.0, 12.0), no_rho=False, split=False, seed=0):
    rng = np.random.default_rng(seed)
    ranges = np.arange(2000.0, 60000.0, 250.0)
    az1 = np.arange(0.0, 360.0, 1.0) + 0.5
    az_all, el_all, ref_all, vel_all, rho_all, fixed = [], [], [], [], [], []
    for e in elevs:
        az = az1
        a = np.radians(az)[:, None]
        el = np.radians(e)
        wind = (u, v, w)
        vr = (wind[0] * np.sin(a) * np.cos(el) + wind[1] * np.cos(a) * np.cos(el) + wind[2] * np.sin(el)) * np.ones((1, ranges.size))
        vr = vr + rng.normal(0, noise, vr.shape)
        ref = np.full(vr.shape, dbz)
        rh = np.full(vr.shape, rho) + rng.normal(0, 0.02, vr.shape)
        if rain_az is not None:
            m = ((az >= rain_az[0]) & (az <= rain_az[1]))[:, None] & np.ones((1, ranges.size), bool)
            rvr = (rain_wind[0] * np.sin(a) + rain_wind[1] * np.cos(a)) * np.cos(el) * np.ones((1, ranges.size))
            vr = np.where(m, rvr, vr)
            ref = np.where(m, rain_dbz, ref)
            rh = np.where(m, 0.99, rh)
        sweeps = [(az, ref, vr, rh)]
        if split:   # a real low-elevation cut is two sweeps: a surveillance cut with rho but no velocity, then a Doppler cut with velocity but no rho
            nan = np.full(vr.shape, np.nan)
            sweeps = [(az, ref, nan, rh), (az + 0.25, ref, vr, nan)]
        for az_s, ref_s, vr_s, rh_s in sweeps:
            az_all.append(az_s); el_all.append(np.full(az_s.size, e)); fixed.append(e)
            ref_all.append(ref_s); vel_all.append(vr_s); rho_all.append(rh_s)
    fields = {
        "reflectivity": np.ma.masked_invalid(np.vstack(ref_all)),
        "velocity": np.ma.masked_invalid(np.vstack(vel_all)),
        "cross_correlation_ratio": np.ma.masked_invalid(np.full((len(elevs) * az1.size, ranges.size), np.nan) if no_rho else np.vstack(rho_all)),
    }
    return FakeRadar(ranges, np.concatenate(az_all), np.concatenate(el_all), np.array(fixed, float), fields, az1.size)
