# Fronts feed

Hourly `fronts.json` (schema `fronts-wpc/1`) built from the NWS Weather Prediction Center coded surface bulletins, published on GitHub Pages
so the duck tracker app can read it from a browser (the NWS text server sends no CORS headers; Pages sends `access-control-allow-origin: *`).

Contains only public NOAA data. Nothing about any property. Keep it that way: this repo is public.

- `fetch_fronts.py` is the whole job (standard library, Python 3.9+). Run it locally with `python3 fetch_fronts.py --out fronts.json`.
- `.github/workflows/fronts.yml` runs it hourly and deploys the file with the Pages actions, so there is no commit history to clutter.
- If the job fails the last good file stays up; the app refuses a file issued more than 36 hours ago and falls back to fronts inferred from the weather forecast.
- GitHub pauses scheduled workflows in a public repo after 60 days without a commit. Re-enable it from the Actions tab (or push any commit) if the feed goes stale.

The canonical copy of `fetch_fronts.py` and the workflow lives in the private app repo under `feed/`; copy changes here.
