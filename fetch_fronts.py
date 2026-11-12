#!/usr/bin/env python3
"""Write a fronts-wpc/1 file (see docs/DATA_CONTRACTS.md) from NWS Weather Prediction Center coded surface bulletins.

  python3 feed/fetch_fronts.py --out fronts.json
  python3 feed/fetch_fronts.py --out pipeline/examples/fronts_sample.json    # the test fixture

Sources (plain text, no key, no CORS headers, which is why a browser cannot read them directly and this runs as a job):
  CODSUS  surface analysis, every 3 h    https://tgftp.nws.noaa.gov/data/raw/as/asus01.kwbc.cod.sus.txt
  CODSRP  forecast progs, 12 h steps     https://tgftp.nws.noaa.gov/data/raw/fs/fsus02.kwbc.cod.srp.txt
feed/.github/workflows/fronts.yml runs it hourly and publishes the file on GitHub Pages (see feed/README.md); the app reads it from FRONTS_URL.
Standard library only.
Format: https://www.wpc.ncep.noaa.gov/html/read_coded_bull.shtml (each front is a run of lat/lon pairs, 4 digits = LLoo, 5 digits = LLooo, degrees west).
"""
import argparse, json, re, urllib.request
from datetime import datetime, timedelta, timezone

URLS = [("analysis", "https://tgftp.nws.noaa.gov/data/raw/as/asus01.kwbc.cod.sus.txt"),
        ("forecast", "https://tgftp.nws.noaa.gov/data/raw/fs/fsus02.kwbc.cod.srp.txt")]
TYPES = {"COLD": "cold", "WARM": "warm", "STNRY": "stationary", "OCFNT": "occluded", "TROF": "trough"}
BOX = (-135, -60, 15, 62)   # lon0, lon1, lat0, lat1: keep fronts with a point in the central and eastern US and nearby
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def nearest_date(day, hh, mm, ref):
    """The UTC datetime with this day of month and time that is closest to ref (bulletins only say ddhhmm)."""
    best = None
    for dm in (-1, 0, 1):
        y, m = ref.year, ref.month + dm
        y, m = (y - 1, 12) if m == 0 else (y + 1, 1) if m == 13 else (y, m)
        try:
            c = datetime(y, m, day, hh, mm, tzinfo=timezone.utc)
        except ValueError:
            continue
        if best is None or abs(c - ref) < abs(best - ref):
            best = c
    return best


def parse_pairs(tokens):
    pts = []
    for t in tokens:
        if not t.isdigit() or len(t) not in (4, 5):
            continue   # intensity codes (WK, MDT, STG) and anything unexpected
        lat, lon = int(t[:2]), int(t[2:])
        if 0 <= lat <= 90 and 0 < lon <= 180:
            pts.append([-float(lon), float(lat)])
    return pts


def parse(text, kind):
    lines = [l.rstrip() for l in text.splitlines()]
    head = re.match(r"^[A-Z]{4}\d{2} [A-Z]{4} (\d{2})(\d{2})(\d{2})", lines[0])
    local = re.search(r"([A-Z]{3}) (\d{1,2}) (\d{4})\s*$", "\n".join(lines[:10]), re.M)
    if not head or not local:
        raise ValueError("unrecognised bulletin header")
    ref = datetime(int(local.group(3)), MONTHS[local.group(1)], int(local.group(2)), 12, tzinfo=timezone.utc)
    issued = nearest_date(int(head.group(1)), int(head.group(2)), int(head.group(3)), ref)
    frames, cur = [], None
    for l in lines[1:]:
        m = re.search(r"VALID (\d{2})(\d{2})(\d{2})Z", l)
        if m and ("PROG" in l or kind == "analysis"):
            cur = {"valid_utc": nearest_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), issued).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "kind": kind, "fronts": []}
            frames.append(cur)
            continue
        tok = l.split()
        if cur is not None and tok and tok[0] in TYPES:
            pts = parse_pairs(tok[1:])
            if len(pts) >= 2 and any(BOX[0] <= p[0] <= BOX[1] and BOX[2] <= p[1] <= BOX[3] for p in pts):
                cur["fronts"].append({"type": TYPES[tok[0]], "pts": pts})
    return issued, frames


def build():
    issued, frames = None, []
    for kind, url in URLS:
        with urllib.request.urlopen(url, timeout=30) as r:
            iss, fr = parse(r.read().decode("ascii", "replace"), kind)
        issued = iss if issued is None or iss > issued else issued
        frames += fr
    frames.sort(key=lambda f: f["valid_utc"])
    return {"schema": "fronts-wpc/1", "source": "NWS Weather Prediction Center coded surface bulletins (CODSUS, CODSRP)",
            "issued_utc": issued.strftime("%Y-%m-%dT%H:%M:%SZ"), "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "synthetic": False, "frames": frames}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fronts.json")
    a = ap.parse_args()
    doc = build()
    with open(a.out, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    print("wrote", a.out, "| issued", doc["issued_utc"])
    for fr in doc["frames"]:
        n = {}
        for x in fr["fronts"]:
            n[x["type"]] = n.get(x["type"], 0) + 1
        print(" ", fr["valid_utc"], fr["kind"], n)
