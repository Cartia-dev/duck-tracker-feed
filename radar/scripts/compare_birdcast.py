#!/usr/bin/env python3
"""Compare this pipeline with BirdCast's processed profiles, volume for volume.

BirdCast publishes vol2bird profiles from the same NEXRAD Level II files (public bucket, one CSV per radar per UTC day,
column `source_file` = the Level II file name). So each volume here can be checked against the reference for exactly the same scan.

  python scripts/compare_birdcast.py --site KNQA --night 2026-11-10 --stride-min 60
  python scripts/compare_birdcast.py --site KNQA --night 2026-11-10 --keep-dir data/volumes     # keep downloads for reruns

Prints one row per volume (column density, traffic rate, mean direction and speed, both ways) and a summary.
BirdCast layers are 100 m thick up to 5 km; ours are 200 m up to 4 km, so BirdCast is also summed over 0 to 4 km for a fair column.
"""
import argparse
import csv
import io
import math
import os
import sys
import urllib.request
from collections import OrderedDict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from nexrad_pipeline import s3 as s3mod  # noqa: E402
from nexrad_pipeline.physics import Params  # noqa: E402
from nexrad_pipeline.sites import load_sites  # noqa: E402
from nexrad_pipeline.solar import night_id, solar_elevation  # noqa: E402

BIRDCAST = "https://birdcastdata.s3.amazonaws.com/nexrad/daily/{site}/{y}/{site}_vpts_{ymd}.csv"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def birdcast_profiles(site, days):
    """{source_file: [rows]} for the given UTC days."""
    out = OrderedDict()
    for d in days:
        url = BIRDCAST.format(site=site, y=d.year, ymd=d.strftime("%Y%m%d"))
        try:
            text = urllib.request.urlopen(url, timeout=60).read().decode("utf-8")
        except Exception as e:
            print(f"  could not read {url}: {e}")
            continue
        for r in csv.DictReader(io.StringIO(text)):
            out.setdefault(r["source_file"], []).append(r)
    return out


def summarise_birdcast(rows, top_m=4000.0):
    """Column density (birds/km^2), traffic rate (birds/km/h), mean u, v (m/s) over layers below top_m."""
    col = mtr = su = sv = 0.0
    for r in rows:
        h, dens, ff, u, v = num(r["height"]), num(r["dens"]), num(r["ff"]), num(r["u"]), num(r["v"])
        if h >= top_m or not np.isfinite(dens):
            continue
        dz = 0.1   # km: layers are 100 m apart
        col += dens * dz
        if np.isfinite(ff):
            mtr += dens * ff * 3.6 * dz
        if np.isfinite(u) and np.isfinite(v):
            su += dens * u * dz
            sv += dens * v * dz
    ub, vb = (su / col, sv / col) if col > 0 else (float("nan"), float("nan"))
    return dict(colden=col, mtr=mtr, u_bar=ub, v_bar=vb)


def direction(u, v):
    return (math.degrees(math.atan2(u, v)) + 360.0) % 360.0 if np.isfinite(u) and np.isfinite(v) else float("nan")


def angdiff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--night", required=True, help="evening date, YYYY-MM-DD")
    ap.add_argument("--stride-min", type=float, default=60.0)
    ap.add_argument("--keep-dir", default=None, help="keep downloaded volumes here (reused on rerun)")
    ap.add_argument("--max-volumes", type=int, default=0)
    a = ap.parse_args()
    from nexrad_pipeline.volume import process_file

    site = load_sites(only=[a.site])[0]
    night = date.fromisoformat(a.night)
    ref = birdcast_profiles(site.id, [night, night + timedelta(days=1)])
    vols = []
    for key in ref:
        vf = s3mod.parse_key(f"{site.id}/{key}")
        if vf and night_id(vf.time, site.lon) == night and solar_elevation(vf.time, site.lat, site.lon) < -6.0:
            vols.append(vf)
    chosen = s3mod.thin(vols, a.stride_min)
    if a.max_volumes:
        chosen = chosen[: a.max_volumes]
    print(f"{site.id} night {night}: {len(vols)} BirdCast profiles at night, comparing {len(chosen)}")
    client = s3mod.client()
    rows = []
    for f in chosen:
        key = f"{f.time:%Y/%m/%d}/{site.id}/{f.key.split('/')[-1]}"
        dest = os.path.join(a.keep_dir or "/tmp", os.path.basename(key))
        if a.keep_dir:
            os.makedirs(a.keep_dir, exist_ok=True)
        try:
            if not os.path.exists(dest):
                s3mod.download(client, key, dest)
            mine = process_file(dest, Params())
        except Exception as e:
            print(f"  {key}: FAILED {type(e).__name__}: {e}")
            continue
        finally:
            if not a.keep_dir and os.path.exists(dest):
                os.remove(dest)
        bc = summarise_birdcast(ref[os.path.basename(key)])
        rows.append((f.time, mine, bc))
    if not rows:
        raise SystemExit("nothing compared")
    print(f"{'UTC':>6} | {'col mine':>8} {'col BC':>8} | {'mtr mine':>8} {'mtr BC':>8} | {'dir mine':>8} {'dir BC':>7} | {'spd m/s mine':>12} {'BC':>5} | precip%")
    for t, m, b in rows:
        dm, db = direction(m["u_bar"], m["v_bar"]), direction(b["u_bar"], b["v_bar"])
        sm, sb = math.hypot(m["u_bar"], m["v_bar"]), math.hypot(b["u_bar"], b["v_bar"])
        print(f"{t:%H:%M} | {m['colden']:8.1f} {b['colden']:8.1f} | {m['mtr']:8.0f} {b['mtr']:8.0f} | {dm:8.0f} {db:7.0f} | {sm:12.1f} {sb:5.1f} | {100 * m.get('precip_frac', float('nan')):5.0f}")
    cm = np.array([r[1]["colden"] for r in rows]); cb = np.array([r[2]["colden"] for r in rows])
    mm = np.array([r[1]["mtr"] for r in rows]); mb = np.array([r[2]["mtr"] for r in rows])
    ok = (cb > 0) | (cm > 0)
    print("--- summary ---")
    if ok.sum() > 2:
        print(f"column density: correlation {np.corrcoef(cm[ok], cb[ok])[0, 1]:.2f}, median ratio mine/BirdCast {np.median(cm[ok] / np.where(cb[ok] > 0, cb[ok], np.nan)):.2f}")
        print(f"traffic rate:   correlation {np.corrcoef(mm[ok], mb[ok])[0, 1]:.2f}, median ratio mine/BirdCast {np.median(mm[ok] / np.where(mb[ok] > 0, mb[ok], np.nan)):.2f}")
    d = [angdiff(direction(r[1]["u_bar"], r[1]["v_bar"]), direction(r[2]["u_bar"], r[2]["v_bar"])) for r in rows]
    d = np.array([x for x in d if np.isfinite(x)])
    if d.size:
        print(f"direction: median difference {np.median(d):.0f} deg, 90th percentile |difference| {np.percentile(np.abs(d), 90):.0f} deg ({d.size} volumes)")
    print(f"night mean column density: mine {cm.mean():.1f}, BirdCast {cb.mean():.1f} birds/km^2;  mean traffic rate: mine {mm.mean():.0f}, BirdCast {mb.mean():.0f} birds/km/h")


if __name__ == "__main__":
    main()
