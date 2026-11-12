"""Per-volume summaries -> one record per site per night, and the export file for the map."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

SCHEMA = "nexrad-nightly/1"


def aggregate_night(volumes: list[dict], n_attempted: int, hours_span: float | None = None) -> dict:
    """volumes: successful per-volume summaries, each with colden, mtr, u_bar, v_bar, mean_h,
    precip_frac. Returns the nightly record stored in the cache and the export."""
    n_ok = len(volumes)
    rec = {"status": "ok", "n_attempted": int(n_attempted), "n_ok": n_ok, "n_failed": int(n_attempted - n_ok)}
    if hours_span is not None:
        rec["hours_span"] = round(float(hours_span), 2)
    if n_ok == 0:
        rec["status"] = "no_data"
        return rec
    colden = np.array([v["colden"] for v in volumes], float)
    mtr = np.array([v["mtr"] for v in volumes], float)
    u = np.array([v["u_bar"] for v in volumes], float)
    v = np.array([v["v_bar"] for v in volumes], float)
    pf = np.array([v.get("precip_frac", np.nan) for v in volumes], float)
    h = np.array([v.get("mean_h", np.nan) for v in volumes], float)
    rec["colden_mean"] = round(float(np.nanmean(colden)), 2)       # birds / km^2
    rec["mtr_mean"] = round(float(np.nanmean(mtr)), 1)             # birds / km / h
    rec["mtr_peak"] = round(float(np.nanmax(mtr)), 1)
    rec["precip_frac_mean"] = None if not np.isfinite(pf).any() else round(float(np.nanmean(pf)), 3)
    rec["mean_height_m"] = None if not np.isfinite(h).any() else round(float(np.nanmean(h)), 0)
    ok = np.isfinite(u) & np.isfinite(v) & (colden > 0)
    if ok.any():
        w = colden[ok]
        ub, vb = np.average(u[ok], weights=w), np.average(v[ok], weights=w)
        speeds = np.average(np.hypot(u[ok], v[ok]), weights=w)
        rec["speed_ms"] = round(float(math.hypot(ub, vb)), 2)
        rec["dir_deg"] = round(float((math.degrees(math.atan2(ub, vb)) + 360.0) % 360.0), 1)   # direction of travel
        rec["dir_r"] = round(float(math.hypot(ub, vb) / speeds), 3) if speeds > 0 else None    # 1 = all volumes agree
    else:
        rec["speed_ms"] = rec["dir_deg"] = rec["dir_r"] = None
    return rec


def cache_path(root: str | Path, night: date, site_id: str) -> Path:
    return Path(root) / "cache" / night.isoformat() / f"{site_id}.json"


def write_record(root, night: date, site_id: str, rec: dict) -> None:
    p = cache_path(root, night, site_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    tmp.replace(p)


def read_record(root, night: date, site_id: str) -> dict | None:
    p = cache_path(root, night, site_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def export(root, sites, end: date, days: int, states: list[str], synthetic: bool = False) -> dict:
    """Assemble the last `days` nights ending at `end` from the cache."""
    out = {
        "schema": SCHEMA,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "synthetic": synthetic,
        "flyway_states": states,
        "units": {"colden_mean": "birds/km^2", "mtr_mean": "birds/km/h", "mtr_peak": "birds/km/h",
                  "speed_ms": "m/s, ground speed", "dir_deg": "degrees clockwise from north, direction of travel",
                  "mean_height_m": "m above radar"},
        "night_definition": "evening date; sun below -6 degrees; volumes thinned to the pipeline stride",
        "sites": {s.id: {"name": s.name, "state": s.state, "lat": s.lat, "lon": s.lon} for s in sites},
        "nights": {},
    }
    for k in range(days):
        night = end - timedelta(days=k)
        recs = {}
        for s in sites:
            r = read_record(root, night, s.id)
            if r is not None:
                recs[s.id] = r
        if recs:
            out["nights"][night.isoformat()] = recs
    return out
