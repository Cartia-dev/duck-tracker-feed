"""Write examples/synthetic_nightly.json: FAKE data in the real export schema, for testing the map loader.

The values are invented (a southward push behind a front, stronger at northern sites early in the
window). They are flagged "synthetic": true and the map labels them as such. Never mistake this for radar data.
"""
import json
import math
import random
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nexrad_pipeline import aggregate
from nexrad_pipeline.sites import load_config, load_sites

end = date(2026, 11, 9)
rnd = random.Random(4)
sites = load_sites()
cfg = load_config()
with tempfile.TemporaryDirectory() as d:
    for k in range(10):
        night = end - timedelta(days=k)
        push = 0.35 + 0.65 * abs(math.sin((k + 1) * 0.9))          # pulses with passing fronts
        edge = 41.0 - 0.3 * (9 - k)                                # migration edge drifting south
        for s in sites:
            lat_factor = math.exp(-((s.lat - edge) / 6.0) ** 2)
            mtr = max(0.0, 2400 * push * lat_factor * rnd.uniform(0.6, 1.3))
            if mtr < 5:
                aggregate.write_record(d, night, s.id, aggregate.aggregate_night([], 6)); continue
            direction = math.radians(rnd.gauss(195, 14))
            speed = rnd.uniform(10, 17)
            vols = [dict(colden=mtr / (speed * 3.6) * rnd.uniform(.8, 1.2), mtr=mtr * rnd.uniform(.7, 1.3),
                         u_bar=speed * math.sin(direction), v_bar=speed * math.cos(direction), mean_h=900.0, precip_frac=0.03)
                    for _ in range(10)]
            aggregate.write_record(d, night, s.id, aggregate.aggregate_night(vols, 10, hours_span=9))
    out = aggregate.export(d, sites, end, 10, cfg["states"], synthetic=True)
Path("examples").mkdir(exist_ok=True)
Path("examples/synthetic_nightly.json").write_text(json.dumps(out, separators=(",", ":")))
print("nights:", len(out["nights"]), "sites:", len(out["sites"]))
