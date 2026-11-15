#!/usr/bin/env python3
"""Write a snow-daily/1 file (see docs/DATA_CONTRACTS.md): snow depth on a half degree grid, observed then forecast.

  python3 feed/fetch_snow.py --out snow.json
  python3 feed/fetch_snow.py --out pipeline/examples/snow_sample.json        # the test fixture

Observed days: NOAA NOHRSC SNODAS snow depth (about 1 km, daily, posted by about 11 UTC; product 1036) from the NSIDC archive,
averaged to half degree cells. Forecast days: the last observed depth plus the change in the Open-Meteo model's snow depth since that day
(new snow and melt), so a model that disagrees with the observation only moves it, never replaces it. The model is asked at one degree
points only (a quarter of the requests) and its change is blended onto the half degree cells. If the model cannot be reached the
last observed depth is carried forward and the file says so (forecast_method).
SNODAS covers the lower 48 and a little of Canada (to 52.875 N); cells outside it are written as 0.
Needs numpy. feed/.github/workflows/fronts.yml runs it every 6 hours next to the fronts job.
"""
import argparse, gzip, io, json, os, tarfile, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

import numpy as np

GRID = {"lat0": 30.0, "lon0": -115.0, "d": 0.5, "nlat": 51, "nlon": 101}   # row-major, south to north, west to east; half degree cells (was one degree until Nov 15 2026)
MODEL_EVERY = 2                            # the forecast model is asked at every second cell in each direction (one degree points)
COLS, ROWS = 6935, 3351                    # SNODAS masked CONUS grid, 30 arc-second pixels, big-endian int16 millimetres, -9999 = no data
RES, LEFT, TOP = 1 / 120, -124.7333333333, 52.875
MM_PER_IN, CAP_MM, MIN_COVER = 25.4, 1500, 0.2   # cap pixels at about 60 in so glacier cells do not swamp a cell mean; need 20% valid pixels
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
SNODAS = "https://noaadata.apps.nsidc.org/NOAA/G02158/masked/{y}/{m:02d}_{mon}/SNODAS_{ymd}.tar"
MODEL_MIN_LAT = 34                        # no model request south of this: snow there is rare and short
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def get(url, tries=2):
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if k == tries - 1:
                raise
        except Exception:
            if k == tries - 1:
                raise
        time.sleep(3)


def snodas_depth(day):
    """Snow depth in mm for one date as a float array (NaN = no data), or None if that day is not posted."""
    raw = get(SNODAS.format(y=day.year, m=day.month, mon=MONTHS[day.month - 1], ymd=day.strftime("%Y%m%d")))
    if raw is None:
        return None
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:   # read members in memory, nothing is extracted to disk
        name = next(n for n in tf.getnames() if "ssmv11036" in n and n.endswith(".dat.gz"))
        data = gzip.decompress(tf.extractfile(name).read())
    if len(data) != COLS * ROWS * 2:
        raise ValueError("unexpected SNODAS grid size %d" % len(data))
    a = np.frombuffer(data, dtype=">i2").reshape(ROWS, COLS).astype(np.float32)
    a[a < -9000] = np.nan
    return np.minimum(a, CAP_MM)


def cell_means(a):
    """Cell means in inches, shape (nlat, nlon); NaN where less than MIN_COVER of the cell has data."""
    out = np.full((GRID["nlat"], GRID["nlon"]), np.nan, dtype=np.float32)
    half = GRID["d"] / 2
    per = int(round(GRID["d"] / RES))
    for i in range(GRID["nlat"]):
        lat = GRID["lat0"] + i * GRID["d"]
        r0 = int(round((TOP - (lat + half)) / RES))
        for j in range(GRID["nlon"]):
            lon = GRID["lon0"] + j * GRID["d"]
            c0 = int(round(((lon - half) - LEFT) / RES))
            ra, rb, ca, cb = max(r0, 0), min(r0 + per, ROWS), max(c0, 0), min(c0 + per, COLS)
            if rb <= ra or cb <= ca:
                continue
            blk = a[ra:rb, ca:cb]; ok = ~np.isnan(blk)
            if ok.sum() >= MIN_COVER * per * per:
                out[i, j] = blk[ok].mean() / MM_PER_IN
    return out


def model_depths(cells, days):
    """{(i, j): {date: depth in inches}} from Open-Meteo's snow_depth at 01:00 local (about 06Z, when SNODAS is stamped)."""
    out = {}
    for k in range(0, len(cells), 250):
        batch = cells[k:k + 250]
        q = urllib.parse.urlencode({
            "latitude": ",".join(str(GRID["lat0"] + i * GRID["d"]) for i, j in batch),
            "longitude": ",".join(str(GRID["lon0"] + j * GRID["d"]) for i, j in batch),
            "hourly": "snow_depth", "timezone": "America/Chicago", "past_days": 5, "forecast_days": 9})
        res = json.loads(get(OPEN_METEO + "?" + q))
        res = res if isinstance(res, list) else [res]
        for (i, j), p in zip(batch, res):
            h = p["hourly"]
            idx = {t[:10]: n for n, t in enumerate(h["time"]) if t.endswith("T01:00")}
            out[(i, j)] = {d: (h["snow_depth"][idx[d.isoformat()]] or 0) * 39.3701 for d in days if d.isoformat() in idx}
        time.sleep(1)
    return out


def spread_change(coarse):
    """A change known at every MODEL_EVERY-th cell (NaN where unknown), blended onto every cell: linear between the known points, 0 where none is near."""
    n = MODEL_EVERY
    c = np.where(np.isnan(coarse), 0.0, coarse)
    out = np.zeros((GRID["nlat"], GRID["nlon"]), dtype=np.float32)
    ci, cj = c.shape
    for i in range(GRID["nlat"]):
        a, fi = min(i // n, ci - 1), (i % n) / n
        a2 = min(a + 1, ci - 1)
        for j in range(GRID["nlon"]):
            b, fj = min(j // n, cj - 1), (j % n) / n
            b2 = min(b + 1, cj - 1)
            out[i, j] = (c[a, b] * (1 - fj) + c[a, b2] * fj) * (1 - fi) + (c[a2, b] * (1 - fj) + c[a2, b2] * fj) * fi
    return out


def build(back_days=6, ahead_days=6, forecast=True):
    today = datetime.now(timezone.utc).date()
    latest = depth = None
    for k in range(0, 6):
        day = today - timedelta(days=k)
        depth = snodas_depth(day)
        if depth is not None:
            latest = day
            break
    if latest is None:
        raise RuntimeError("no SNODAS file in the last 6 days")
    obs = {latest: cell_means(depth)}
    for k in range(1, back_days + 1):
        d = latest - timedelta(days=k)
        a = snodas_depth(d)
        if a is not None:
            obs[d] = cell_means(a)
    days = {d.isoformat(): m for d, m in obs.items()}
    method = "none"
    base = obs[latest]
    fdays = [latest + timedelta(days=k) for k in range(1, ahead_days + 1)]
    model = None
    if forecast and ahead_days:
        cells = [(i, j) for i in range(0, GRID["nlat"], MODEL_EVERY) for j in range(0, GRID["nlon"], MODEL_EVERY)
                 if GRID["lat0"] + i * GRID["d"] >= MODEL_MIN_LAT and not np.isnan(base[i, j])]
        try:
            model = model_depths(cells, [latest] + fdays)
            method = "snodas + open-meteo model change"
        except Exception as e:   # carry the observation forward rather than inventing anything
            print("model unavailable (%s); carrying the last observed depth" % e)
    if ahead_days and method == "none":
        method = "persistence"
    for d in fdays:
        m = base.copy()
        if model:
            coarse = np.full((len(range(0, GRID["nlat"], MODEL_EVERY)), len(range(0, GRID["nlon"], MODEL_EVERY))), np.nan, dtype=np.float32)
            for (i, j), v in model.items():
                if latest in v and d in v:
                    coarse[i // MODEL_EVERY, j // MODEL_EVERY] = v[d] - v[latest]
            m = np.where(np.isnan(base), base, np.maximum(0.0, base + spread_change(coarse)))
        days[d.isoformat()] = m
    kinds = {d.isoformat(): ("observed" if d <= latest else "forecast") for d in list(obs) + fdays}
    flat = {k: [0 if np.isnan(x) or x < .05 else round(float(x), 1) for x in days[k].ravel()] for k in sorted(days)}   # 0, not 0.0: most cells are bare and the file is four times the cells
    return {"schema": "snow-daily/1", "synthetic": False, "units": "inches", "grid": GRID, "days": flat, "kinds": kinds,
            "observed_through": latest.isoformat(), "forecast_method": method,
            "source": "NOAA NOHRSC SNODAS (NSIDC G02158) with Open-Meteo.com model change for forecast days",
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="snow.json")
    ap.add_argument("--max-age-hours", type=float, default=0, help="skip the run if --out already exists and is newer than this")
    ap.add_argument("--no-forecast", action="store_true")
    a = ap.parse_args()
    if a.max_age_hours and os.path.exists(a.out):
        try:
            gen = datetime.strptime(json.load(open(a.out))["generated_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - gen < timedelta(hours=a.max_age_hours):
                print("snow file is recent enough, skipping")
                raise SystemExit(0)
        except (ValueError, KeyError, OSError):
            pass
    doc = build(ahead_days=0 if a.no_forecast else 6, forecast=not a.no_forecast)
    with open(a.out, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    ks = sorted(doc["days"])
    print("wrote", a.out, "|", ks[0], "to", ks[-1], "| observed through", doc["observed_through"], "|", doc["forecast_method"])
