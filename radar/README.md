# nexrad-pipeline

Nightly bird-movement summaries from NEXRAD Level II weather radar for the 14 Mississippi Flyway
states (AL, AR, IL, IN, IA, KY, LA, MI, MN, MS, MO, OH, TN, WI), 41 radar sites.

Output is one JSON file (`public/nightly.json`, schema `nexrad-nightly/1`) holding the last 10 nights.
The duck migration map loads it with its "Load radar data" button.

## What it measures, and what it cannot

For each site and night it reports bird **traffic rate** (birds per km per hour), **column density**
(birds per km²), and the density-weighted **ground speed and direction of travel**.

It measures **all birds aloft**. Radar cannot tell a mallard from a warbler. Late in the season over the
Delta, waterfowl are a large share of nocturnal traffic, but that is an assumption, not a measurement.
Species need other inputs (eBird abundance, state waterfowl surveys, hunter reports); they are not in this pipeline.

## How it works

1. Reads Level II volumes anonymously from `s3://noaa-nexrad-level2/YYYY/MM/DD/SITE/` (us-east-1, no credentials). That bucket refused anonymous
   access when first run on real data (Nov 2026), so it falls back to the Unidata mirror `s3://unidata-nexrad-level2/` (same keys, same files).
2. Keeps volumes taken with the sun below -6° at that site, grouped into "nights" by local evening date,
   thinned to one per `--stride-min` minutes (default 60).
3. Per volume (follows vol2bird/bioRad, Dokter et al. 2011, 2019): the low elevations are split cuts, a surveillance sweep with rho_hv
   and no velocity then a Doppler sweep with velocity and no rho_hv, so each Doppler sweep borrows rho_hv from its surveillance partner;
   gates 5-35 km out; drops precipitation
   with a dual-pol rho_hv >= 0.95 test (pointwise and smoothed); drops echoes above eta 36,000 cm²/km³;
   fits a uniform wind to radial velocities in 200 m layers (VVP); eta = (π⁵/λ⁴)|K|²Z at λ = 10.7 cm;
   density = eta / 11 cm² per bird; layers whose velocity residual is under 1 m/s are set to zero.
4. Aggregates to the night and caches `data/cache/<night>/<site>.json`, so reruns only do new work.

## Setup

```
pip install -e .          # numpy, scipy, boto3, arm_pyart
python -m unittest discover -s tests -t .      # 27 tests on synthetic radar volumes, no network needed
```

## Validation (done once on real data, Nov 12 2026)

Checked at one site, KNQA (Memphis), for two nights, against BirdCast's own vol2bird profiles for the
**same Level II files** (BirdCast publishes them: `https://birdcastdata.s3.amazonaws.com/nexrad/daily/<SITE>/<YYYY>/<SITE>_vpts_<YYYYMMDD>.csv`,
one per radar per UTC day, `source_file` is the Level II file name). Reproduce with
`python scripts/compare_birdcast.py --site KNQA --night 2026-11-10 --stride-min 60` (about 130 MB of downloads per night).

| Night (13 volumes each) | Column density: correlation, mine / BirdCast | Traffic rate: correlation, mine / BirdCast | Direction: median difference | Mean traffic rate, mine vs BirdCast |
|---|---|---|---|---|
| Nov 10 (busy, 5 to 7 m/s) | 0.99, 1.03 | 0.99, 0.99 | 5 deg (90th pct 14 deg) | 441 vs 436 birds/km/h |
| Nov 9 (weaker, 1 to 3 m/s) | 0.99, 1.14 | 0.97, 0.90 | 2 deg (90th pct 35 deg) | 224 vs 236 birds/km/h |

What that means: the shape of the night and the size of the flight agree with the reference to within about 10 to 15 percent. Direction is
reliable when birds are moving at a few m/s or more; in the calm early hours of Nov 9 (speeds 1 to 2 m/s) it differs by up to 80 degrees, because
a weak, scattered flow has no clear direction. Density ran 10 to 40 percent high in some low-density hours. This is two nights at one site, in
November, so it is evidence, not proof: repeat on a night with rain, on a night over 1,000 birds/km/h, and at a second site before trusting it everywhere.

What the first real run found and fixed:
1. **NOAA's bucket refused anonymous access.** `noaa-nexrad-level2` returned AccessDenied for listing and downloads (any date). The Unidata
   mirror `unidata-nexrad-level2` has the same keys and files, so `s3.py` now falls back to it.
2. **Split cuts.** At 0.5 to 1.3 degrees the radar scans twice: a surveillance sweep (reflectivity, rho_hv, no velocity) then a Doppler
   sweep (velocity, no rho_hv). The code skipped every Doppler sweep, so velocity came only from sweeps above about 1.8 degrees and speeds ran 10 to
   30 percent high (traffic rate 1.15 times BirdCast). Each Doppler sweep now borrows rho_hv from its surveillance partner; the ratio is 0.99.
3. **Site list.** 40 of 41 coordinates match the NWS station list within 0.05 degrees. `KLIX` (Slidell, LA) is not in the NWS list and has no
   volumes in the archive, so check or drop it. It does not affect Arkansas.

Not yet checked: vol2bird as an alternative engine and nights with heavy rain.


Tuning knobs, if it disagrees on another night: `Params` in `physics.py` (rho_hv, sd_vvp_min, range, rcs). For a vetted engine, swap in `vol2bird` and keep everything downstream.

## Commands

```
nexrad-pipeline run --night 2026-11-09 --nights 10      # backfill (cached nights are skipped)
nexrad-pipeline export --days 10 --dest public/nightly.json
nexrad-pipeline daily --days 10                         # run + export, what the schedule calls
nexrad-pipeline seed --from nightly.json --out data      # rebuild the cache from a published file (a fresh machine skips finished nights)
```

## Running it daily

`feed/.github/workflows/radar.yml` (copied into the public `duck-tracker-feed` repo by `tools/sync_feed.sh`) runs at 15:30 UTC, restores the finished
nights from the last published file (`seed`), processes the missing ones, and publishes `nightly.json` on the repo's `radar-data` branch. The hourly Pages
deploy (`fronts.yml`) then serves it at `.../nightly.json` next to `fronts.json` and `snow.json`, and the app loads it. Run it by hand from the Actions tab
with `sites=KNQA days=2` for a quick check. Budget: roughly 575 volumes per night across 41 sites at a 60-minute stride, around 5-6 GB downloaded and
well under an hour on 4 cores. The first 10-night backfill is about 10x that; `--budget-minutes` stops cleanly and the next run resumes. The workflow
publishes nothing if no night was processed, so a failed run leaves the last good file up (the app ignores a file older than 72 hours).

## Known limits

- No velocity dealiasing; very fast tailwind nights can fold and bias speed/direction.
- Simpler clutter handling than vol2bird; sites near cities may show residual near-field contamination.
- Insects can pass as birds in warm months. In November this is minor.
- Streaks drawn from this data on the map extrapolate each site's local flow downstream; they are not tracked birds.
- Next steps: airspeed (subtract HRRR winds) to separate fast waterfowl-like targets from slower songbirds; MistNet; more sites.
