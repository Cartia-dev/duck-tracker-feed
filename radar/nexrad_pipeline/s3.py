"""Anonymous access to the NEXRAD Level II archive on AWS.

Two buckets hold the same keys. NOAA's `noaa-nexrad-level2` answered AccessDenied to anonymous listing and downloads when this was
first run on real data (Nov 2026), so the Unidata mirror `unidata-nexrad-level2` is the fallback; the first bucket that works is used.

Key layout: <YYYY>/<MM>/<DD>/<SITE>/<SITE><YYYYMMDD>_<HHMMSS>_V06 (older files may end in .gz).
Files ending in _MDM are metadata, not volumes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

BUCKETS = ("noaa-nexrad-level2", "unidata-nexrad-level2")
BUCKET = BUCKETS[0]
_active: str | None = None   # the bucket that last worked in this process
_NAME = re.compile(r"^(?P<site>[A-Z0-9]{4})(?P<d>\d{8})_(?P<t>\d{6})")


@dataclass(frozen=True)
class VolumeFile:
    key: str
    site: str
    time: datetime  # UTC, timezone-aware
    size: int = 0


def parse_key(key: str, size: int = 0) -> VolumeFile | None:
    name = key.rsplit("/", 1)[-1]
    if name.endswith("_MDM"):
        return None
    m = _NAME.match(name)
    if not m:
        return None
    t = datetime.strptime(m["d"] + m["t"], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return VolumeFile(key=key, site=m["site"], time=t, size=size)


def thin(files: list[VolumeFile], stride_min: float) -> list[VolumeFile]:
    """Keep roughly one volume every stride_min minutes."""
    out: list[VolumeFile] = []
    last = None
    for f in sorted(files, key=lambda x: x.time):
        if last is None or (f.time - last).total_seconds() >= stride_min * 60.0:
            out.append(f)
            last = f.time
    return out


def client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    return boto3.client("s3", region_name="us-east-1", config=Config(signature_version=UNSIGNED))


def _denied(e) -> bool:
    return getattr(e, "response", {}).get("Error", {}).get("Code") in ("AccessDenied", "403", "NoSuchBucket", "404", "NoSuchKey")


def _order() -> list[str]:
    return [_active] + [b for b in BUCKETS if b != _active] if _active else list(BUCKETS)


def list_volumes(s3, site: str, day: date) -> list[VolumeFile]:
    global _active
    from botocore.exceptions import ClientError

    prefix = f"{day:%Y/%m/%d}/{site}/"
    last = None
    for bucket in _order():
        out: list[VolumeFile] = []
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    vf = parse_key(obj["Key"], obj.get("Size", 0))
                    if vf:
                        out.append(vf)
        except ClientError as e:
            if not _denied(e):
                raise
            last = e
            continue
        _active = bucket
        return out
    raise last


def download(s3, key: str, dest: str) -> None:
    global _active
    from botocore.exceptions import ClientError

    last = None
    for bucket in _order():
        try:
            s3.download_file(bucket, key, dest)
            _active = bucket
            return
        except ClientError as e:
            if not _denied(e):
                raise
            last = e
    raise last
