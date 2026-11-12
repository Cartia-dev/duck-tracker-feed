"""Write examples/synthetic_snow.json: FAKE snow depth in the real snow-daily/1 schema, for testing the map loader.

Schema: grid {lat0, lon0, d (degrees), nlat, nlon}; days {YYYY-MM-DD: flat list of snow depth in inches, row-major,
rows run south to north starting at lat0, columns west to east starting at lon0}. Real files would come from NOAA's
SNODAS snow depth grid (about 1 km) averaged down to this coarse grid.
The values here are invented: a snow line near 43N that surges south and melts back. Never mistake it for observations.
"""
import json, math
from datetime import date, timedelta
from pathlib import Path

G = dict(lat0=30.0, lon0=-115.0, d=1.0, nlat=26, nlon=51)
days = {}
start = date(2026, 11, 1)
for k in range(14):
    day = start + timedelta(days=k)
    line = 44.0 - 3.0 * abs(math.sin(k * 0.45))          # pulses south, melts back
    rows = []
    for i in range(G["nlat"]):
        lat = G["lat0"] + i * G["d"]
        for j in range(G["nlon"]):
            lon = G["lon0"] + j * G["d"]
            edge = line + 0.8 * math.sin((lon + 100) / 12)
            rows.append(round(max(0.0, min(16.0, (lat - edge) * 1.2)), 1))   # snow lies north of the line
    days[day.isoformat()] = rows
out = {"schema": "snow-daily/1", "synthetic": True, "units": "inches", "grid": G, "days": days}
Path(__file__).resolve().parent.parent.joinpath("examples", "synthetic_snow.json").write_text(json.dumps(out, separators=(",", ":")))
print("days:", len(days), "cells per day:", G["nlat"] * G["nlon"])
