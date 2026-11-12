"""Command line: verify-sites, inspect, run, export, daily, seed."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import aggregate, s3 as s3mod
from .physics import Params
from .sites import Site, load_config, load_sites
from .solar import night_id, solar_elevation

TWILIGHT = -6.0  # civil twilight; volumes are kept when the sun is below this


def latest_complete_night(now: datetime | None = None) -> date:
    """Night that finished this morning. Run between about 14:00 and 18:00 UTC."""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=18)).date()


def process_site_night(site: Site, night: date, root: str, stride_min: float, force: bool, deadline: float) -> tuple[str, str, str]:
    if not force and aggregate.read_record(root, night, site.id) is not None:
        return site.id, night.isoformat(), "cached"
    if time.time() > deadline:
        return site.id, night.isoformat(), "skipped (time budget)"
    from .volume import process_file

    client = s3mod.client()
    files = []
    for d in (night, night + timedelta(days=1)):
        files += s3mod.list_volumes(client, site.id, d)
    night_files = [f for f in files if night_id(f.time, site.lon) == night and solar_elevation(f.time, site.lat, site.lon) < TWILIGHT]
    chosen = s3mod.thin(night_files, stride_min)
    if not night_files:
        aggregate.write_record(root, night, site.id, aggregate.aggregate_night([], 0))
        return site.id, night.isoformat(), "no files in archive yet"
    summaries, errors = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for f in chosen:
            dest = os.path.join(tmp, os.path.basename(f.key))
            try:
                s3mod.download(client, f.key, dest)
                summaries.append(process_file(dest, Params()))
            except Exception as e:  # keep going; one bad volume must not lose the night
                errors.append(f"{os.path.basename(f.key)}: {e}")
            finally:
                if os.path.exists(dest):
                    os.remove(dest)
    span = (chosen[-1].time - chosen[0].time).total_seconds() / 3600.0 if len(chosen) > 1 else 0.0
    rec = aggregate.aggregate_night(summaries, len(chosen), hours_span=span)
    if errors:
        rec["first_error"] = errors[0][:200]
    aggregate.write_record(root, night, site.id, rec)
    return site.id, night.isoformat(), f"{rec['status']} ({rec['n_ok']}/{rec['n_attempted']} volumes)"


def run_nights(sites, nights, root, stride_min, workers, force, budget_min):
    deadline = time.time() + budget_min * 60.0
    tasks = [(s, n) for n in nights for s in sites]
    print(f"{len(tasks)} site-nights, {workers} workers, stride {stride_min} min")
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_site_night, s, n, str(root), stride_min, force, deadline): (s, n) for s, n in tasks}
        for fut in as_completed(futs):
            s, n = futs[fut]
            try:
                print(" ", *fut.result())
            except Exception as e:
                print(f"  {s.id} {n}: FAILED {e}")


def cmd_verify(args):
    cfg = load_config(args.config)
    sites = load_sites(args.config)
    contact = os.environ.get("NEXRAD_CONTACT", "nexrad-pipeline")
    req = urllib.request.Request("https://api.weather.gov/radar/stations?stationType=WSR-88D",
                                 headers={"User-Agent": f"nexrad-pipeline ({contact})", "Accept": "application/geo+json"})
    try:
        feats = json.load(urllib.request.urlopen(req, timeout=30))["features"]
        ref = {}
        for f in feats:
            pr = f.get("properties", {})
            sid = pr.get("id") or pr.get("stationIdentifier")
            lon, lat = f["geometry"]["coordinates"][:2]
            ref[sid] = (lat, lon)
        bad = 0
        for s in sites:
            if s.id not in ref:
                print(f"  {s.id}: not found in NWS station list"); bad += 1; continue
            dlat, dlon = abs(ref[s.id][0] - s.lat), abs(ref[s.id][1] - s.lon)
            if dlat > 0.05 or dlon > 0.05:
                print(f"  {s.id}: config {s.lat},{s.lon} vs NWS {ref[s.id][0]:.4f},{ref[s.id][1]:.4f}"); bad += 1
        print(f"NWS check: {len(sites) - bad}/{len(sites)} sites match")
    except Exception as e:
        print(f"NWS station list unavailable ({e}); skipping coordinate check")
    client = s3mod.client()
    day = latest_complete_night() 
    missing = [s.id for s in sites if not s3mod.list_volumes(client, s.id, day)]
    print(f"S3 check for {day}: " + ("all sites have volumes" if not missing else "no volumes for " + ", ".join(missing)))
    print(f"States covered: {', '.join(cfg['states'])}  ({len(sites)} sites)")


def cmd_inspect(args):
    from .volume import inspect_file
    print(inspect_file(args.file))


def _nights(args) -> list[date]:
    end = date.fromisoformat(args.night) if args.night else latest_complete_night()
    return [end - timedelta(days=k) for k in range(args.nights)]  # newest first


def cmd_run(args):
    sites = load_sites(args.config, args.sites.split(",") if args.sites else None)
    run_nights(sites, _nights(args), args.out, args.stride_min, args.workers, args.force, args.budget_minutes)


def cmd_export(args):
    cfg = load_config(args.config)
    sites = load_sites(args.config)
    end = date.fromisoformat(args.end) if args.end else latest_complete_night()
    data = aggregate.export(args.out, sites, end, args.days, cfg["states"])
    Path(args.dest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.dest).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {args.dest}: {len(data['nights'])} nights, {len(data['sites'])} sites")


def cmd_seed(args):
    """Rebuild the cache from an exported file, so a fresh machine only processes nights that are not in it yet.
    Only nights with status ok are restored; no_data nights are tried again."""
    data = json.loads(Path(args.src).read_text(encoding="utf-8"))
    if data.get("schema") != aggregate.SCHEMA or data.get("synthetic"):
        raise SystemExit(f"{args.src} is not a real {aggregate.SCHEMA} file")
    n = 0
    for night, recs in data.get("nights", {}).items():
        for site_id, rec in recs.items():
            if rec.get("status") == "ok":
                aggregate.write_record(args.out, date.fromisoformat(night), site_id, rec)
                n += 1
    print(f"restored {n} site-nights from {args.src}")


def cmd_daily(args):
    args.night = None
    args.nights = args.days
    cmd_run(args)
    args.end = None
    cmd_export(args)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nexrad-pipeline")
    ap.add_argument("--config", default=None, help="site config JSON (default: Mississippi Flyway states)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--out", default="data", help="working directory (holds cache/)")
        p.add_argument("--sites", default=None, help="comma-separated site ids, default all")
        p.add_argument("--stride-min", type=float, default=60.0, help="process one volume per this many minutes")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--force", action="store_true", help="reprocess even if cached")
        p.add_argument("--budget-minutes", type=float, default=300.0, help="stop starting new site-nights after this long")

    sub.add_parser("verify-sites").set_defaults(fn=cmd_verify)
    p = sub.add_parser("inspect"); p.add_argument("file"); p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("run"); common(p)
    p.add_argument("--night", default=None, help="YYYY-MM-DD evening date of the newest night (default: last night)")
    p.add_argument("--nights", type=int, default=1, help="how many nights to process, going back from --night")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("export"); p.add_argument("--out", default="data"); p.add_argument("--dest", default="public/nightly.json")
    p.add_argument("--end", default=None); p.add_argument("--days", type=int, default=10); p.set_defaults(fn=cmd_export)
    p = sub.add_parser("seed", help="restore the cache from an exported nightly.json"); p.add_argument("--from", dest="src", required=True)
    p.add_argument("--out", default="data"); p.set_defaults(fn=cmd_seed)
    p = sub.add_parser("daily", help="process the last N nights (cached ones are skipped) and export"); common(p)
    p.add_argument("--days", type=int, default=10); p.add_argument("--dest", default="public/nightly.json")
    p.set_defaults(fn=cmd_daily)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
