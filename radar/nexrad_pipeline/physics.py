"""Radar physics for turning one NEXRAD volume into a vertical profile of bird density,
speed and direction.

This is a compact implementation that follows the logic of vol2bird / bioRad (Dokter et al.
2011, 2019): dual-pol precipitation filtering with rho_hv, a volume velocity profile (VVP)
fit per altitude layer, reflectivity converted to eta and divided by a radar cross section.
It is NOT a drop-in replacement: it has no velocity dealiasing, no MistNet segmentation and
a simpler clutter treatment. See README for the validation steps.

Everything here is pure numpy/scipy so it can be tested without radar files.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import uniform_filter

EARTH_R = 6_371_000.0
K_EFF = 4.0 / 3.0


@dataclass(frozen=True)
class Params:
    range_min: float = 5_000.0      # m, slant range. Skips the clutter-heavy near field.
    range_max: float = 35_000.0     # m, beyond this the beam is too wide for layered profiles.
    layer_height: float = 200.0     # m
    n_layers: int = 20              # 0-4 km above the radar
    rho_hv: float = 0.95            # gates at or above this correlation coefficient are precipitation
    smooth_gates: int = 5           # window for the smoothed rho_hv test
    eta_max: float = 36_000.0       # cm^2/km^3, discard stronger echoes (matches vol2bird default)
    rcs_cm2: float = 11.0           # radar cross section per bird (vol2bird starting point)
    wavelength_cm: float = 10.7     # WSR-88D S-band
    sd_vvp_min: float = 1.0         # m/s, S-band default. Below this a layer is treated as non-bird.
    min_gates_eta: int = 30
    min_gates_vvp: int = 60
    min_az_sectors: int = 6         # of 8 sectors of 45 degrees must contain gates
    max_elev: float = 10.0          # degrees, ignore the steep upper sweeps


def beam_height(slant_m, elev_deg, k: float = K_EFF, r_earth: float = EARTH_R):
    """Beam-centre height above the radar for a slant range, 4/3 effective earth radius model."""
    kr = k * r_earth
    e = np.radians(elev_deg)
    return np.sqrt(slant_m ** 2 + kr ** 2 + 2.0 * slant_m * kr * np.sin(e)) - kr


def dbz_to_eta(dbz, wavelength_cm: float = 10.7, k2: float = 0.93):
    """Reflectivity factor (dBZ) to volume reflectivity eta in cm^2/km^3.

    eta = (pi^5 / lambda^4) * |K|^2 * Z, with Z in mm^6/m^3. In these units the constant is
    pi^5 * |K|^2 / lambda_cm^4 * 1000. At S-band 0 dBZ is about 21.7 cm^2/km^3.
    """
    z = np.power(10.0, np.asarray(dbz, dtype=float) / 10.0)
    return (math.pi ** 5 * k2 / wavelength_cm ** 4) * 1000.0 * z


def nan_uniform(arr: np.ndarray, size: int) -> np.ndarray:
    """Moving average that ignores NaNs. Cells with no valid neighbours stay NaN."""
    valid = np.isfinite(arr)
    num = uniform_filter(np.where(valid, arr, 0.0), size=size, mode="nearest")
    den = uniform_filter(valid.astype(float), size=size, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[den < 1e-6] = np.nan
    return out


@dataclass
class GateSet:
    eta: np.ndarray = field(default_factory=lambda: np.empty(0))
    h_eta: np.ndarray = field(default_factory=lambda: np.empty(0))
    vr: np.ndarray = field(default_factory=lambda: np.empty(0))
    az_v: np.ndarray = field(default_factory=lambda: np.empty(0))
    el_v: np.ndarray = field(default_factory=lambda: np.empty(0))
    h_v: np.ndarray = field(default_factory=lambda: np.empty(0))
    n_valid: int = 0     # gates in range with reflectivity and rho_hv
    n_precip: int = 0    # of those, flagged as precipitation


def select_gates(ref, vel, rho, ranges, az, el, p: Params) -> GateSet:
    """Pick biological-candidate gates from one sweep.

    ref, vel, rho: (n_ray, n_gate) float arrays with NaN for missing. vel may be None.
    ranges: (n_gate,) slant range in m. az, el: (n_ray,) degrees.
    """
    n_ray, n_gate = ref.shape
    slant = np.broadcast_to(np.asarray(ranges, float)[None, :n_gate], (n_ray, n_gate))
    elev = np.asarray(el, float)[:, None]
    h = beam_height(slant, elev)
    in_range = (slant >= p.range_min) & (slant <= p.range_max)

    have = in_range & np.isfinite(ref) & np.isfinite(rho)
    rho_s = nan_uniform(rho, p.smooth_gates)
    with np.errstate(invalid="ignore"):
        precip = (rho >= p.rho_hv) | (rho_s >= p.rho_hv)
    eta_all = dbz_to_eta(np.where(np.isfinite(ref), ref, -999.0), p.wavelength_cm)

    bio = have & ~precip & (eta_all <= p.eta_max)
    gs = GateSet(n_valid=int(have.sum()), n_precip=int((have & precip).sum()))
    gs.eta = eta_all[bio]
    gs.h_eta = h[bio]
    if vel is not None:
        bv = bio & np.isfinite(vel)
        gs.vr = vel[bv]
        gs.az_v = np.broadcast_to(np.asarray(az, float)[:, None], (n_ray, n_gate))[bv]
        gs.el_v = np.broadcast_to(elev, (n_ray, n_gate))[bv]
        gs.h_v = h[bv]
    return gs


def concat_gatesets(sets: list[GateSet]) -> GateSet:
    out = GateSet()
    if not sets:
        return out
    for name in ("eta", "h_eta", "vr", "az_v", "el_v", "h_v"):
        setattr(out, name, np.concatenate([getattr(s, name) for s in sets]))
    out.n_valid = sum(s.n_valid for s in sets)
    out.n_precip = sum(s.n_precip for s in sets)
    return out


def fit_vvp(vr, az_deg, el_deg, p: Params):
    """Least-squares fit of a uniform wind (u east, v north, w up) to radial velocities.

    Returns (u, v, w, sd, n_used) or None when there are too few gates or too little
    azimuth coverage. One pass of 3-sigma outlier rejection, then a refit.
    """
    if vr.size < p.min_gates_vvp:
        return None
    sectors = np.unique(np.floor((az_deg % 360.0) / 45.0).astype(int))
    if sectors.size < p.min_az_sectors:
        return None
    az = np.radians(az_deg)
    el = np.radians(el_deg)
    a = np.column_stack([np.sin(az) * np.cos(el), np.cos(az) * np.cos(el), np.sin(el)])
    keep = np.ones(vr.size, bool)
    sol = np.zeros(3)
    for _ in range(2):
        if keep.sum() < p.min_gates_vvp:
            return None
        sol, *_ = np.linalg.lstsq(a[keep], vr[keep], rcond=None)
        resid = vr - a @ sol
        sd = float(resid[keep].std())
        keep = np.abs(resid) <= 3.0 * max(sd, 1e-6)
    if keep.sum() < p.min_gates_vvp:
        return None
    resid = vr - a @ sol
    return float(sol[0]), float(sol[1]), float(sol[2]), float(resid[keep].std()), int(keep.sum())


def build_profile(gs: GateSet, p: Params) -> dict:
    """Per-layer eta, density, wind components and VVP residual."""
    n = p.n_layers
    prof = {k: np.full(n, np.nan) for k in ("eta", "dens", "u", "v", "w", "ff", "dd", "sd_vvp")}
    prof["n_eta"] = np.zeros(n, int)
    prof["n_vvp"] = np.zeros(n, int)
    li_e = np.floor(gs.h_eta / p.layer_height).astype(int)
    li_v = np.floor(gs.h_v / p.layer_height).astype(int)
    for layer in range(n):
        m = li_e == layer
        prof["n_eta"][layer] = int(m.sum())
        if m.sum() >= p.min_gates_eta:
            prof["eta"][layer] = float(gs.eta[m].mean())
        mv = li_v == layer
        prof["n_vvp"][layer] = int(mv.sum())
        fit = fit_vvp(gs.vr[mv], gs.az_v[mv], gs.el_v[mv], p)
        if fit is None:
            continue
        u, v, w, sd, _ = fit
        prof["u"][layer], prof["v"][layer], prof["w"][layer], prof["sd_vvp"][layer] = u, v, w, sd
        prof["ff"][layer] = math.hypot(u, v)
        prof["dd"][layer] = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
    dens = prof["eta"] / p.rcs_cm2
    sd = prof["sd_vvp"]
    dens = np.where(np.isfinite(sd), dens, np.nan)      # no velocity fit: cannot tell birds from insects
    dens = np.where(np.isfinite(sd) & (sd < p.sd_vvp_min), 0.0, dens)
    prof["dens"] = dens
    return prof


def integrate_profile(prof: dict, p: Params) -> dict:
    """Collapse a profile to numbers per volume: column density, traffic rate, mean vector."""
    dz_km = p.layer_height / 1000.0
    ok = np.isfinite(prof["dens"]) & np.isfinite(prof["u"]) & np.isfinite(prof["v"])
    out = dict(colden=0.0, mtr=0.0, u_bar=float("nan"), v_bar=float("nan"), mean_h=float("nan"), n_layers=int(ok.sum()))
    if not ok.any():
        return out
    d, u, v = prof["dens"][ok], prof["u"][ok], prof["v"][ok]
    h = (np.arange(p.n_layers)[ok] + 0.5) * p.layer_height
    colden = float(np.sum(d) * dz_km)                       # birds per km^2
    out["colden"] = colden
    out["mtr"] = float(np.sum(d * np.hypot(u, v)) * 3.6 * dz_km)   # birds per km per hour
    if colden > 0:
        out["u_bar"] = float(np.sum(d * u) * dz_km / colden)
        out["v_bar"] = float(np.sum(d * v) * dz_km / colden)
        out["mean_h"] = float(np.sum(d * h) / np.sum(d))
    return out
