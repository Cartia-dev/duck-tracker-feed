"""Site catalogue for the Mississippi Flyway states."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "sites_mississippi_flyway.json"


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    state: str
    lat: float
    lon: float


def load_config(path: str | Path | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def load_sites(path: str | Path | None = None, only: list[str] | None = None) -> list[Site]:
    cfg = load_config(path)
    sites = [Site(**s) for s in cfg["sites"]]
    if only:
        wanted = {x.upper() for x in only}
        sites = [s for s in sites if s.id in wanted]
        missing = wanted - {s.id for s in sites}
        if missing:
            raise SystemExit(f"Unknown site id(s): {', '.join(sorted(missing))}")
    return sites
