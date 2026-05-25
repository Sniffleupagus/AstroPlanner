#!/usr/bin/env python3
"""Scan astrophotography captures and generate a sky coverage map."""

import argparse
import json
import os

from planner.scanner import scan_all
from planner.skymap import build_skymap, print_summary
from planner.skymap_static import build_skymap_png


def main():
    parser = argparse.ArgumentParser(
        description="Scan captures and generate sky coverage map."
    )
    parser.add_argument("--mask", default="masks/horizon.json",
                        help="Horizon mask JSON file (default: masks/horizon.json)")
    parser.add_argument("--lat", type=float, help="Observer latitude (degrees)")
    parser.add_argument("--lon", type=float, help="Observer longitude (degrees)")
    args = parser.parse_args()

    records = scan_all()
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
