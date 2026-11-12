"""One Level II volume -> one profile summary. Works on any object shaped like a Py-ART Radar."""
from __future__ import annotations

import numpy as np

from .physics import Params, build_profile, concat_gatesets, integrate_profile, select_gates

FIELDS = ("reflectivity", "velocity", "cross_correlation_ratio")


def _field(radar, name: str, sl) -> np.ndarray | None:
    f = radar.fields.get(name)
    if f is None:
        return None
    data = f["data"][sl]
    return np.ma.filled(np.ma.asarray(data).astype("float64"), np.nan)


def _nearest_azimuth(az_src: np.ndarray, az_dst: np.ndarray) -> np.ndarray:
    """For each destination ray, the index of the source ray with the closest azimuth."""
    d = np.abs((az_dst[:, None] - az_src[None, :] + 180.0) % 360.0 - 180.0)
    return d.argmin(axis=1)


def process_radar(radar, p: Params = Params()) -> dict:
    """Summarise a polar volume.

    Low elevations are split cuts: a surveillance sweep carries reflectivity and rho_hv but no velocity, then a Doppler sweep at the same
    elevation carries velocity but no rho_hv. Each Doppler sweep borrows rho_hv from its surveillance partner (nearest azimuth) so its
    velocity gates can be screened for rain; its own reflectivity only screens strong echoes. Eta and the precipitation counts come from
    the surveillance sweep, so nothing is counted twice. Sweeps that cannot be used are counted in sweeps_skipped.
    """
    rng = np.asarray(radar.range["data"], dtype=float)
    info = []
    for s in range(radar.nsweeps):
        if float(radar.fixed_angle["data"][s]) > p.max_elev:
            continue
        sl = radar.get_slice(s)
        ref, rho, vel = _field(radar, "reflectivity", sl), _field(radar, "cross_correlation_ratio", sl), _field(radar, "velocity", sl)
        info.append(dict(s=s, fa=float(radar.fixed_angle["data"][s]), ref=ref,
                         rho=rho if rho is not None and np.isfinite(rho).any() else None,
                         vel=vel if vel is not None and np.isfinite(vel).any() else None,
                         az=np.asarray(radar.azimuth["data"][sl], dtype=float), el=np.asarray(radar.elevation["data"][sl], dtype=float)))
    sets, skipped, paired = [], 0, 0
    for i in info:
        if i["ref"] is None:
            skipped += 1
        elif i["rho"] is not None:
            sets.append(select_gates(i["ref"], i["vel"], i["rho"], rng, i["az"], i["el"], p))
        elif i["vel"] is not None:
            cands = [q for q in info if q["rho"] is not None and q["vel"] is None and abs(q["fa"] - i["fa"]) <= 0.25]
            if not cands:
                skipped += 1
                continue
            q = min(cands, key=lambda c: (abs(c["fa"] - i["fa"]), abs(c["s"] - i["s"])))
            rho = q["rho"][_nearest_azimuth(q["az"], i["az"])]
            gs = select_gates(i["ref"], i["vel"], rho, rng, i["az"], i["el"], p)
            gs.eta, gs.h_eta, gs.n_valid, gs.n_precip = np.empty(0), np.empty(0), 0, 0
            sets.append(gs)
            paired += 1
        else:
            skipped += 1
    if not sets:
        raise ValueError("no usable dual-pol sweeps in this volume")
    gs = concat_gatesets(sets)
    prof = build_profile(gs, p)
    out = integrate_profile(prof, p)
    out["precip_frac"] = gs.n_precip / gs.n_valid if gs.n_valid else float("nan")
    out["sweeps_used"] = len(sets)
    out["sweeps_paired"] = paired
    out["sweeps_skipped"] = skipped
    out["n_velocity_gates"] = int(gs.vr.size)
    return out


def process_file(path: str, p: Params = Params()) -> dict:
    import pyart  # imported lazily so the math can be tested without radar libraries

    radar = pyart.io.read_nexrad_archive(path, include_fields=list(FIELDS))
    return process_radar(radar, p)


def inspect_file(path: str) -> str:
    """Human-readable list of which moments each sweep carries. Run this on your first file."""
    import pyart

    radar = pyart.io.read_nexrad_archive(path)
    lines = [f"site lat/lon: {float(radar.latitude['data'][0]):.4f}, {float(radar.longitude['data'][0]):.4f}"]
    for s in range(radar.nsweeps):
        sl = radar.get_slice(s)
        have = []
        for name, f in radar.fields.items():
            d = np.ma.filled(np.ma.asarray(f["data"][sl]).astype("float64"), np.nan)
            frac = float(np.isfinite(d).mean())
            if frac > 0.01:
                have.append(f"{name}:{frac:.0%}")
        lines.append(f"sweep {s:2d}  elev {float(radar.fixed_angle['data'][s]):5.2f}  rays {sl.stop - sl.start}  " + ", ".join(have))
    return "\n".join(lines)
