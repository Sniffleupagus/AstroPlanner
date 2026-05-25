#!/usr/bin/env python3
"""Scan astrophotography captures and generate a sky coverage map."""

import argparse
import json
import os

from planner.scanner import scan_all
from planner.skymap import build_skymap, print_summary
from planner.skymap_static import build_skymap_png
from planner.capture_cache import CaptureCache, find_db


_DEFAULT_ARCHIVE = "/mnt/zarchive/Pictures/Astrophotography"


def main():
    parser = argparse.ArgumentParser(
        description="Scan captures and generate sky coverage map."
    )
    parser.add_argument("--mask", default="masks/horizon.json",
                        help="Horizon mask JSON file (default: masks/horizon.json)")
    parser.add_argument("--lat", type=float, help="Observer latitude (degrees)")
    parser.add_argument("--lon", type=float, help="Observer longitude (degrees)")
    parser.add_argument("--archive", default=_DEFAULT_ARCHIVE,
                        help="Astrophotography archive base path")
    args = parser.parse_args()

    db_path = find_db(args.archive)
    cache = None
    if db_path:
        print(f"Using cache: {db_path}")
        cache = CaptureCache(db_path, read_only=True)
    else:
        print("No cache found — scanning all files (run update_cache.py to build one)")

    records = scan_all(args.archive, cache)
    if cache:
        cache.close()
    print_summary(records)

    mask_path = args.mask if os.path.exists(args.mask) else None
    lat, lon = args.lat, args.lon

    if mask_path and (lat is None or lon is None):
        with open(mask_path) as f:
            loc = json.load(f).get("location", {})
        lat = lat if lat is not None else loc.get("lat")
        lon = lon if lon is not None else loc.get("lon")

    build_skymap(records, output_path="skymap.html",
                 horizon_mask_path=mask_path, lat=lat, lon=lon)
    build_skymap_png(records, output_path="skymap.png")


if __name__ == "__main__":
    main()
