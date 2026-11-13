#!/usr/bin/env python3
"""Write nightly.json (schema nexrad-nightly/1, see docs/DATA_CONTRACTS.md) from BirdCast's public NEXRAD vertical profiles.

  python3 feed/fetch_birdcast.py --out nightly.json                    # the last 10 complete nights, all sites
  python3 feed/fetch_birdcast.py --out nightly.json --reuse prev.json  # keep older nights from the last file, fetch only what is new

BirdCast (Cornell Lab of Ornithology, Colorado State University, UMass Amherst) runs vol2bird on every NEXRAD volume and publishes one CSV per radar per UTC day:
https://birdcastdata.s3.amazonaws.com/nexrad/daily/<SITE>/<YYYY>/<SITE>_vpts_<YYYYMMDD>.csv   (about 1.5 MB; a day's file appears about 01:05 UTC the next day)
That replaces processing Level II volumes ourselves (the pipeline/ code, still in the project, agreed with it on clean nights and is kept for checking it).

Per profile: column density = sum of density x layer thickness, traffic rate = sum of density x ground speed x thickness, using layers with a velocity fit.
Per night: profiles taken with the sun below -6 degrees, grouped by local evening date, averaged the same way the pipeline did.
Rain: BirdCast leaves layers without a velocity fit when rain gets in the way, and those profiles are left out. As a second opinion the hourly weather record (Open-Meteo)
is checked at each radar site: a profile within an hour and a half of an hour with 0.1 mm or more is left out too. A night with more than half its profiles left out for
rain is reported as "rain" with no numbers; mostly missing profiles are "no_data". If the weather record cannot be fetched the run carries on without it and says so.
Standard library only.
"""
import argparse, csv, io, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

BASE = "https://birdcastdata.s3.amazonaws.com/nexrad/daily/{site}/{y}/{site}_vpts_{ymd}.csv"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
VERSION = 2                  # same record format as the pipeline's: 2 = rain screened
SOURCE = "BirdCast NEXRAD vertical profiles (vol2bird), Cornell Lab of Ornithology, Colorado State University and UMass Amherst"
TWILIGHT = -6.0              # profiles count as night when the sun is below this at the radar
PUBLISH_LAG = timedelta(hours=1, minutes=10)   # a UTC day's file is posted about 01:05 UTC the next day
MAX_RAIN_SHARE = 0.5
MIN_USABLE = 0.4             # a night needs a velocity fit in at least this share of its profiles
MIN_LAYERS = 3               # layers with density and velocity for a profile to count
WET_MM, BUFFER_H = 0.1, 1.5
HERE = os.path.dirname(os.path.abspath(__file__))
SITE_FILES = [os.path.join(HERE, "sites_mississippi_flyway.json"), os.path.join(HERE, "..", "pipeline", "config", "sites_mississippi_flyway.json")]


# ---- sun and nights (same maths as pipeline/nexrad_pipeline/solar.py) ----
def _naive_utc(t):
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def julian_day(t):
    t = _naive_utc(t)
    y, m, d = t.year, t.month, t.day + (t.hour + (t.minute + t.second / 60.0) / 60.0) / 24.0
    if m <= 2:
        y, m = y - 1, m + 12
    a = y // 100
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + 2 - a + a // 4 - 1524.5


def solar_elevation(t, lat, lon):
    n = julian_day(t) - 2451545.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = math.radians((280.460 + 0.9856474 * n) % 360.0 + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    ha = math.radians((280.46061837 + 360.98564736629 * n) % 360.0 + lon) - ra
    s = math.sin(math.radians(lat)) * math.sin(dec) + math.cos(math.radians(lat)) * math.cos(dec) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def night_id(t, lon):
    """Date of the evening on which the night containing t began, by local mean solar time."""
    return (_naive_utc(t) + timedelta(hours=lon / 15.0) - timedelta(hours=12)).date()


def latest_complete_night(now=None):
    """Evening date of the newest night whose UTC-day files are all published."""
    now = now or datetime.now(timezone.utc)
    newest_day = (now - PUBLISH_LAG).date() - timedelta(days=1)   # the newest UTC day with a file
    return newest_day - timedelta(days=1)                        # a night needs that day and the one before it


# ---- reading profiles ----
def num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def parse_profiles(text):
    """[{t: UTC datetime, radar_h, layers: [(height m, dens, ff, u, v)]}] in file order."""
    out, cur, key = [], None, None
    for r in csv.DictReader(io.StringIO(text)):
        if r["datetime"] != key:
            key = r["datetime"]
            cur = {"t": datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), "radar_h": num(r.get("radar_height")) or 0.0, "layers": []}
            out.append(cur)
        cur["layers"].append((num(r["height"]), num(r["dens"]), num(r["ff"]), num(r["u"]), num(r["v"])))
    return out


def summarise(p):
    """colden (birds/km^2), mtr (birds/km/h), mean u, v (m/s), mean height above the radar (m), or None without enough layers with a velocity fit."""
    hs = sorted(h for h, *_ in p["layers"] if h is not None)
    steps = sorted(b - a for a, b in zip(hs, hs[1:]) if b > a)
    dz = (steps[len(steps) // 2] if steps else 100.0) / 1000.0   # layer thickness in km
    col = mtr = su = sv = sh = 0.0
    n = 0
    for h, dens, ff, u, v in p["layers"]:
        if h is None or dens is None or ff is None or u is None or v is None:
            continue
        n += 1
        col += dens * dz
        mtr += dens * ff * 3.6 * dz
        su += dens * u * dz
        sv += dens * v * dz
        sh += dens * (h - p["radar_h"]) * dz
    if n < MIN_LAYERS:
        return None
    return {"colden": col, "mtr": mtr, "u_bar": su / col if col > 0 else None, "v_bar": sv / col if col > 0 else None, "mean_h": sh / col if col > 0 else None}


def aggregate_night(profs, n_attempted, n_rain, hours_span):
    """The nightly record, the same fields and rules as pipeline/nexrad_pipeline/aggregate.py."""
    n_ok = len(profs)
    rec = {"v": VERSION, "status": "ok", "n_attempted": n_attempted, "n_ok": n_ok, "n_rain": n_rain, "n_failed": n_attempted - n_ok - n_rain, "hours_span": round(hours_span, 2)}
    if n_attempted == 0:
        rec["status"] = "no_data"
        return rec
    if n_rain / n_attempted > MAX_RAIN_SHARE:
        rec["status"] = "rain"
        return rec
    if n_ok < MIN_USABLE * n_attempted or n_ok == 0:
        rec["status"] = "no_data"
        return rec
    col, mtr = [p["colden"] for p in profs], [p["mtr"] for p in profs]
    rec["colden_mean"] = round(sum(col) / n_ok, 2)
    rec["mtr_mean"] = round(sum(mtr) / n_ok, 1)
    rec["mtr_peak"] = round(max(mtr), 1)
    rec["precip_frac_mean"] = None   # BirdCast does not publish it
    hh = [p["mean_h"] for p in profs if p["mean_h"] is not None]
    rec["mean_height_m"] = round(sum(hh) / len(hh)) if hh else None
    ok = [p for p in profs if p["u_bar"] is not None and p["colden"] > 0]
    if ok:
        w = sum(p["colden"] for p in ok)
        ub, vb = sum(p["u_bar"] * p["colden"] for p in ok) / w, sum(p["v_bar"] * p["colden"] for p in ok) / w
        sp = sum(math.hypot(p["u_bar"], p["v_bar"]) * p["colden"] for p in ok) / w
        rec["speed_ms"] = round(math.hypot(ub, vb), 2)
        rec["dir_deg"] = round((math.degrees(math.atan2(ub, vb)) + 360.0) % 360.0, 1)
        rec["dir_r"] = round(math.hypot(ub, vb) / sp, 3) if sp > 0 else None
    else:
        rec["speed_ms"] = rec["dir_deg"] = rec["dir_r"] = None
    return rec


# ---- the weather record as a second opinion on rain (same checks as pipeline/nexrad_pipeline/rain.py) ----
def fetch_wet_hours(sites, first, timeout=60.0):
    past = max(1, min(92, (datetime.now(timezone.utc) - first).days + 2))
    out = {}
    for i in range(0, len(sites), 20):
        chunk = sites[i:i + 20]
        q = urllib.parse.urlencode({"latitude": ",".join(str(s["lat"]) for s in chunk), "longitude": ",".join(str(s["lon"]) for s in chunk),
                                    "hourly": "precipitation", "timezone": "UTC", "past_days": past, "forecast_days": 1})
        with urllib.request.urlopen(f"{OPEN_METEO}?{q}", timeout=timeout) as r:
            res = json.load(r)
        res = res if isinstance(res, list) else [res]
        if len(res) != len(chunk):
            raise ValueError("unexpected weather response")
        for s, p in zip(chunk, res):
            h = p["hourly"]
            out[s["id"]] = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t, v in zip(h["time"], h["precipitation"]) if (v or 0) >= WET_MM]
    return out


def is_wet(t, wet_hours):
    return bool(wet_hours) and any(abs((w - t).total_seconds()) <= BUFFER_H * 3600 for w in wet_hours)


# ---- the job ----
def get_day(site, ymd, tries=3):
    url = BASE.format(site=site, y=ymd[:4], ymd=ymd)
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None   # not posted (yet), or this radar had no data that day
        except Exception:
            pass
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"could not read {url}")


def site_nights(site, nights, wet, texts):
    """Records for one site for the given evening dates, from its day files (ymd -> text or None)."""
    profs = []
    for ymd, text in texts.items():
        if text:
            profs += parse_profiles(text)
    out = {}
    for night in nights:
        mine = [p for p in profs if night_id(p["t"], site["lon"]) == night and solar_elevation(p["t"], site["lat"], site["lon"]) < TWILIGHT]
        mine.sort(key=lambda p: p["t"])
        dry = [p for p in mine if not is_wet(p["t"], wet)]
        usable = [s for s in (summarise(p) for p in dry) if s]
        span = (mine[-1]["t"] - mine[0]["t"]).total_seconds() / 3600.0 if len(mine) > 1 else 0.0
        rec = aggregate_night(usable, len(mine), len(mine) - len(dry), span)
        rec["rain_check"] = "BirdCast gaps and weather" if wet is not None else "BirdCast gaps only"
        out[night.isoformat()] = rec
    return out


def load_sites():
    for p in SITE_FILES:
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
    raise SystemExit("sites_mississippi_flyway.json not found")


def build(n_nights=10, end=None, reuse=None, weather=True, workers=8):
    cfg = load_sites()
    sites = cfg["sites"]
    end = end or latest_complete_night()
    nights = [end - timedelta(days=k) for k in range(n_nights)]
    old = {}
    if reuse and reuse.get("record_version") == VERSION and "BirdCast" in str(reuse.get("source", "")):
        for k, recs in reuse.get("nights", {}).items():
            if date.fromisoformat(k) <= end - timedelta(days=2):   # the newest nights are re-read: late files and sites fill in
                old[k] = recs
    todo = [n for n in nights if n.isoformat() not in old]
    result = {n.isoformat(): dict(old[n.isoformat()]) for n in nights if n.isoformat() in old}
    wet = None
    if weather and todo:
        try:
            wet = fetch_wet_hours(sites, datetime.combine(min(todo) - timedelta(days=1), datetime.min.time(), timezone.utc))
        except Exception as e:
            print(f"  WARNING: weather record unavailable ({e}); using BirdCast's own gaps only")
    if todo:
        days = sorted({(n + timedelta(days=d)).strftime("%Y%m%d") for n in todo for d in (0, 1)})
        jobs = [(s, ymd) for s in sites for ymd in days]
        with ThreadPoolExecutor(workers) as ex:
            got = list(ex.map(lambda j: get_day(j[0]["id"], j[1]), jobs))
        texts = {}
        for (s, ymd), text in zip(jobs, got):
            texts.setdefault(s["id"], {})[ymd] = text
        for s in sites:
            recs = site_nights(s, todo, None if wet is None else wet.get(s["id"], []), texts[s["id"]])
            for k, rec in recs.items():
                result.setdefault(k, {})[s["id"]] = rec
    return {"schema": "nexrad-nightly/1", "record_version": VERSION, "source": SOURCE, "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "synthetic": False, "flyway_states": cfg.get("states", []),
            "units": {"colden_mean": "birds/km^2", "mtr_mean": "birds/km/h", "mtr_peak": "birds/km/h", "speed_ms": "m/s, ground speed",
                      "dir_deg": "degrees clockwise from north, direction of travel", "mean_height_m": "m above radar"},
            "night_definition": "evening date; sun below -6 degrees; every BirdCast profile that night (about one every 10 minutes)",
            "sites": {s["id"]: {"name": s["name"], "state": s["state"], "lat": s["lat"], "lon": s["lon"]} for s in sites},
            "nights": {k: result[k] for k in sorted(result, reverse=True) if result[k]}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="nightly.json")
    ap.add_argument("--nights", type=int, default=10)
    ap.add_argument("--end", default=None, help="evening date of the newest night, default the newest complete one")
    ap.add_argument("--reuse", default=None, help="a previous nightly.json: keep its nights older than two days")
    ap.add_argument("--no-weather", action="store_true")
    a = ap.parse_args()
    prev = None
    if a.reuse and os.path.exists(a.reuse):
        try:
            prev = json.load(open(a.reuse, encoding="utf-8"))
        except ValueError:
            prev = None
    doc = build(a.nights, date.fromisoformat(a.end) if a.end else None, prev, not a.no_weather)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    ks = sorted(doc["nights"])
    stat = {}
    for recs in doc["nights"].values():
        for r in recs.values():
            stat[r["status"]] = stat.get(r["status"], 0) + 1
    print(f"wrote {a.out}: nights {ks[0]} to {ks[-1]}, {len(doc['sites'])} sites, records {stat}")
