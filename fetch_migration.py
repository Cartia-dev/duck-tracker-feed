#!/usr/bin/env python3
"""Write migration.json (schema migration-progress/1, see docs/DATA_CONTRACTS.md): how far this season's Mississippi Flyway duck migration has got.

  python3 feed/fetch_migration.py --out migration.json [--reuse prev.json]       # the daily job
  python3 feed/fetch_migration.py --build-reference --out migration_reference.json   # once: two past seasons, about 5 GB of BirdCast files

The idea: a pool of migratory ducks starts north and ends in the winter range. Radar says how fast birds cross each of four lines of latitude (45, 42, 39, 36 N).
Cumulative net southward passage at a line, divided by what a normal season (two past seasons) adds up to, is the share of the crossing population that has got across.
No duck count is read off the radar. Radar sees every bird, so each night is weighted by an assumed duck share that rises through the fall (migration_assumptions.json),
and the pool, its flyway share and where it starts and ends are all assumptions in that file. Output is a share of the pool by zone, and millions only as that share of the assumed pool.
Nights without data (rain, gaps) are filled from neighbouring nights, which undercounts frontal flights that arrive in rain. Standard library only.
"""
import argparse, json, os, sys
from datetime import date, datetime, timedelta, timezone

import fetch_birdcast as fb

HERE = os.path.dirname(os.path.abspath(__file__))
ASSUMPTIONS = os.path.join(HERE, "migration_assumptions.json")
REFERENCE = os.path.join(HERE, "migration_reference.json")
SCHEMA = "migration-progress/1"
FILL_WINDOW = 5          # a missing night borrows from valid nights within this many days either side
KEEP = ("status", "south_km", "south_mean", "n_ok", "n_attempted", "n_rain", "rain_check", "v")   # what is stored per site-night so the next run can reuse it


def load_assumptions(path=ASSUMPTIONS):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def md(text, year):
    m, d = text.split("-")
    return date(year, int(m), int(d))


def season_dates(a, year):
    """First and last night of the season that starts in `year`."""
    start = md(a["season_start"], year)
    return start, md(a["season_end"], year + 1)


def season_containing(a, day):
    """(start, end) if `day` falls inside a season, else None (off season)."""
    for y in (day.year - 1, day.year):
        s, e = season_dates(a, y)
        if s <= day <= e:
            return s, e
    return None


def weight_on(day, start, knots):
    """Assumed duck share of radar traffic on this night: linear between the knots, flat outside them."""
    pts = []
    for text, w in knots:
        k = md(text, start.year)
        pts.append((k if k >= start else md(text, start.year + 1), w))
    pts.sort()
    if day <= pts[0][0]:
        return pts[0][1]
    if day >= pts[-1][0]:
        return pts[-1][1]
    for (d0, w0), (d1, w1) in zip(pts, pts[1:]):
        if d0 <= day <= d1:
            return w0 + (w1 - w0) * (day - d0).days / max(1, (d1 - d0).days)


def boundary_series(nights, sites, days):
    """Per night: mean net southward passage (birds per km of front) over the boundary's sites that have good data, or None."""
    out = []
    for d in days:
        recs = nights.get(d.isoformat(), {})
        vals = [recs[s]["south_km"] for s in sites if s in recs and recs[s].get("status") == "ok" and recs[s].get("south_km") is not None]
        out.append(sum(vals) / len(vals) if vals else None)
    return out


def fill_series(vals, window=FILL_WINDOW):
    """Gaps take the mean of the nearest valid nights either side (or the one side that has data). Returns (filled, n_filled, n_unfilled).
    A night that cannot be filled counts as no net movement."""
    out, filled, unfilled = [], 0, 0
    for i, v in enumerate(vals):
        if v is not None:
            out.append(v)
            continue
        a = next((vals[j] for j in range(i - 1, max(-1, i - window - 1), -1) if vals[j] is not None), None)
        b = next((vals[j] for j in range(i + 1, min(len(vals), i + window + 1)) if vals[j] is not None), None)
        near = [x for x in (a, b) if x is not None]
        if near:
            out.append(sum(near) / len(near))
            filled += 1
        else:
            out.append(0.0)
            unfilled += 1
    return out, filled, unfilled


def cumulative(vals, weights):
    out, t = [], 0.0
    for v, w in zip(vals, weights):
        t += v * w
        out.append(t)
    return out


def crossing_index(curve, target):
    """Fractional day index at which a rising curve first reaches `target` (None if it never does)."""
    for i, v in enumerate(curve):
        if v >= target:
            if i == 0 or curve[i] == curve[i - 1]:
                return float(i)
            return i - 1 + (target - curve[i - 1]) / (curve[i] - curve[i - 1])
    return None


def zone_names(lats):
    names = [f"North of {lats[0]}°N"]
    names += [f"{lats[i + 1]}–{lats[i]}°N" for i in range(len(lats) - 1)]
    names.append(f"South of {lats[-1]}°N")
    return names


def shares_from_progress(progress, bounds):
    """Share of the pool north of each line, then per zone. Birds cannot be north of a southern line without being north of the ones above it."""
    north = [b["north_at_end"] + (b["north_at_start"] - b["north_at_end"]) * (1.0 - p) for p, b in zip(progress, bounds)]
    for i in range(1, len(north)):
        north[i] = max(north[i], north[i - 1])
    zones = [north[0]] + [north[i] - north[i - 1] for i in range(1, len(north))] + [1.0 - north[-1]]
    return zones


def compute(a, current, reference, start, through):
    """current: {lat: raw nightly series from season start to through}; reference: {lat: {year: raw series over the whole season}}.
    Returns the migration-progress document body."""
    bounds = a["boundaries"]
    n_days = (through - start).days + 1
    days = [start + timedelta(days=i) for i in range(n_days)]
    w = [weight_on(d, start, a["duck_weight"]) for d in days]
    pool = a["ducks_total_millions"] * a["migratory_share"] * a["flyway_share"] * a.get("fall_flight_factor", 1.0)
    season_len = (season_dates(a, start.year)[1] - start).days + 1
    w_full = [weight_on(start + timedelta(days=i), start, a["duck_weight"]) for i in range(season_len)]
    per_b, normals = [], []
    for b in bounds:
        key = str(b["lat"])
        cur, nf, nu = fill_series(current[key])
        cum = cumulative(cur, w)
        ref_curves = []
        for yr, vals in reference[key].items():
            if len(vals) != season_len:
                continue   # a reference season of a different length cannot be lined up by day
            filled, _, _ = fill_series(vals)
            ref_curves.append(cumulative(filled, w_full))
        if not ref_curves:
            raise ValueError(f"no usable reference season at {key} N")
        ref = [sum(c[i] for c in ref_curves) / len(ref_curves) for i in range(season_len)]
        total = ref[-1]
        if not total > 0:
            raise ValueError(f"the reference seasons show no net southward passage at {key} N")
        prog = [min(1.0, max(0.0, c / total)) for c in cum]
        norm = [min(1.0, max(0.0, r / total)) for r in ref]
        per_b.append(dict(b=b, prog=prog, norm=norm, filled=nf, unfilled=nu, cum=cum, ref=ref, total=total))
    # history of zone shares day by day, and the normal-year shares for the same days
    history = []
    for i, d in enumerate(days):
        z = shares_from_progress([x["prog"][i] for x in per_b], bounds)
        history.append({"date": d.isoformat(), "shares": [round(v, 4) for v in z]})
    names = zone_names([b["lat"] for b in bounds])
    now = [x["prog"][-1] for x in per_b]
    zones_now = shares_from_progress(now, bounds)
    zones_norm = shares_from_progress([x["norm"][n_days - 1] for x in per_b], bounds)
    out_b = []
    for x in per_b:
        j = crossing_index(x["norm"], x["prog"][-1]) if x["prog"][-1] > 0 else 0.0
        ahead = None if j is None else round(j - (n_days - 1), 1)   # positive: a normal year gets here later than today, so this season is ahead
        out_b.append({"lat": x["b"]["lat"], "sites": x["b"]["sites"], "progress": round(x["prog"][-1], 4), "normal_progress": round(x["norm"][n_days - 1], 4),
                      "days_ahead": ahead, "nights_filled": x["filled"], "nights_unfilled": x["unfilled"]})
    return {"pool_millions": round(pool, 2),
            "boundaries": out_b,
            "zones": [{"name": n, "share": round(s, 4), "millions": round(s * pool, 2), "normal_share": round(ns, 4)} for n, s, ns in zip(names, zones_now, zones_norm)],
            "history": history}


# ---- the jobs ----
def all_sites(a):
    return sorted({s for b in a["boundaries"] for s in b["sites"]})


def trim(nights):
    return {k: {s: {f: r[f] for f in KEEP if f in r} for s, r in recs.items()} for k, recs in nights.items()}


def build_reference(a, years, out, workers=8):
    ref = {"schema": "migration-reference/1", "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "season_start": a["season_start"], "season_end": a["season_end"],
           "years": [], "boundaries": {str(b["lat"]): {} for b in a["boundaries"]}, "source": fb.SOURCE}
    if os.path.exists(out):
        try:
            old = json.load(open(out, encoding="utf-8"))
            if old.get("schema") == ref["schema"]:
                ref = old
        except ValueError:
            pass
    for y in years:
        if y in ref["years"]:
            print(f"season {y}-{y + 1} already in {out}")
            continue
        start, end = season_dates(a, y)
        n = (end - start).days + 1
        print(f"season {y}-{y + 1}: {n} nights, {len(all_sites(a))} sites", flush=True)
        doc = fb.build(n_nights=n, end=end, site_ids=set(all_sites(a)), weather=True, workers=workers, progress=True)
        days = [start + timedelta(days=i) for i in range(n)]
        for b in a["boundaries"]:
            ref["boundaries"][str(b["lat"])][str(y)] = [None if v is None else round(v, 1) for v in boundary_series(doc["nights"], b["sites"], days)]
        ref["years"].append(y)
        with open(out, "w", encoding="utf-8") as f:   # saved after every season so an interrupted run resumes
            json.dump(ref, f, separators=(",", ":"))
    return ref


def build_current(a, reference, prev=None, workers=8, now=None):
    end = fb.latest_complete_night(now)
    window = season_containing(a, end)
    if window is None:
        return {"schema": SCHEMA, "status": "off_season", "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "message": "The tracker runs from %s to %s." % (a["season_start"], a["season_end"])}
    start, _ = window
    n = (end - start).days + 1
    doc = fb.build(n_nights=n, end=end, reuse=prev, site_ids=set(all_sites(a)), weather=True, workers=workers, progress=True)
    days = [start + timedelta(days=i) for i in range(n)]
    current = {str(b["lat"]): boundary_series(doc["nights"], b["sites"], days) for b in a["boundaries"]}
    refb = {k: {str(y): v for y, v in d.items()} for k, d in reference["boundaries"].items()}
    body = compute(a, current, refb, start, end)
    return {"schema": SCHEMA, "status": "ok", "generated_utc": doc["generated_utc"], "source": doc["source"], "record_version": doc["record_version"],
            "season_start": start.isoformat(), "through": end.isoformat(), "reference_years": reference["years"],
            "assumptions": {k: a[k] for k in ("ducks_total_millions", "migratory_share", "flyway_share", "fall_flight_factor", "duck_weight", "boundaries")},
            **body,
            "nights": trim(doc["nights"]),
            "caveats": ["Radar sees every bird, not just ducks. Each night is weighted by an assumed duck share that rises through the fall.",
                        "The pool, its flyway share, where it starts and where it winters are assumptions, not measurements.",
                        "Nights lost to rain are filled from neighbouring nights, which undercounts frontal flights.",
                        "Radar does not cover Canada, so departures from the northern pool are inferred from birds crossing 45 N."]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="migration.json")
    ap.add_argument("--reuse", default=None, help="the previous migration.json: older nights are kept")
    ap.add_argument("--reference", default=REFERENCE)
    ap.add_argument("--build-reference", action="store_true", help="build the past-season reference file instead (large download, run once)")
    ap.add_argument("--years", nargs="*", type=int, default=None)
    ap.add_argument("--assumptions", default=ASSUMPTIONS)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    a = load_assumptions(args.assumptions)
    if args.build_reference:
        ref = build_reference(a, args.years or a["reference_years"], args.out, args.workers)
        print("reference seasons:", ref["years"])
        sys.exit(0)
    reference = json.load(open(args.reference, encoding="utf-8"))
    prev = None
    if args.reuse and os.path.exists(args.reuse):
        try:
            prev = json.load(open(args.reuse, encoding="utf-8"))
        except ValueError:
            prev = None
    doc = build_current(a, reference, prev, args.workers)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    if doc["status"] == "ok":
        print("wrote %s through %s: %s" % (args.out, doc["through"], "; ".join(f"{z['name']} {z['share']:.0%}" for z in doc["zones"])))
    else:
        print("wrote", args.out, "(off season)")
