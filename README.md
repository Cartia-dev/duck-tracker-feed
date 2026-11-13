# Weather feed

Three public NOAA-derived files, published on GitHub Pages:

- `fronts.json` (`fronts-wpc/1`), hourly, from the NWS Weather Prediction Center coded surface bulletins (`fetch_fronts.py`).
- `nightly.json` (`nexrad-nightly/1`), daily at 02:30 UTC, bird movement over the Mississippi and Central Flyway radars (80 sites, one `sites_<flyway>_flyway.json` file each), built from BirdCast's public vertical-profile archive (`fetch_birdcast.py`, `radar.yml`; credit: BirdCast, Cornell Lab of Ornithology, Colorado State University, UMass Amherst) and kept on the `radar-data` branch; the hourly deploy copies it into the site. Rain: profiles BirdCast could not fit are left out, plus any within 1.5 h of a wet hour in the Open-Meteo record; a night more than half rain is reported as "rain" with no numbers.
- Not published: `fetch_migration.py`, `migration_assumptions.json` and `migration_reference.json` are a shelved attempt at a duck migration tracker. It does not work with radar alone (the radar total is mostly songbirds until mid-November); see `docs/MODEL.md` in the app repo. Nothing in the workflows runs it.
- `snow.json` (`snow-daily/1`), every 6 hours, from NOAA SNODAS snow depth plus the Open-Meteo model's change for forecast days (`fetch_snow.py`, needs numpy).

They are on GitHub Pages
so the duck tracker app can read it from a browser (the NWS text server sends no CORS headers; Pages sends `access-control-allow-origin: *`).

Contains only public NOAA data. Nothing about any property. Keep it that way: this repo is public.

- `fetch_fronts.py` and `fetch_birdcast.py` (standard library) and `fetch_snow.py` (numpy) are the jobs. Run them locally with `--out fronts.json` / `--out nightly.json` / `--out snow.json`. `python3 -m unittest test_birdcast test_migration` (stdlib) and `test_feed` (needs numpy) run the offline tests.
- `.github/workflows/fronts.yml` runs hourly (snow only when its file is over 5.5 h old) and deploys the file with the Pages actions, so there is no commit history to clutter.
- If the job fails the last good file stays up; the app refuses a file issued more than 36 hours ago and falls back to fronts inferred from the weather forecast.
- GitHub pauses scheduled workflows in a public repo after 60 days without a commit. Re-enable it from the Actions tab (or push any commit) if the feed goes stale.

The canonical copy of everything here lives in the private app repo (`feed/` and `pipeline/`); `tools/sync_feed.sh <clone>` copies it over, together with the radar site list. Nothing is edited here by hand.
