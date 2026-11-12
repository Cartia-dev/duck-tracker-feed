"""Low-precision solar position, used to decide which radar volumes were taken at night.

Accuracy is a few hundredths of a degree, which is plenty for a twilight cutoff.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone


def _as_utc_naive(t: datetime) -> datetime:
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def julian_day(t: datetime) -> float:
    t = _as_utc_naive(t)
    y, m = t.year, t.month
    d = t.day + (t.hour + (t.minute + t.second / 60.0) / 60.0) / 24.0
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def solar_elevation(t: datetime, lat: float, lon: float) -> float:
    """Sun elevation in degrees (no refraction). lon is east-positive."""
    n = julian_day(t) - 2451545.0
    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = math.radians(mean_lon + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    gmst = (280.46061837 + 360.98564736629 * n) % 360.0
    ha = math.radians(gmst + lon) - ra
    la = math.radians(lat)
    s = math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def night_id(t: datetime, lon: float) -> date:
    """Date of the *evening* on which the night containing time t began.

    Uses local mean solar time, so a volume at 03:00 local belongs to the previous
    calendar date's night, and the id is stable for the whole night.
    """
    local = _as_utc_naive(t) + timedelta(hours=lon / 15.0)
    return (local - timedelta(hours=12)).date()
