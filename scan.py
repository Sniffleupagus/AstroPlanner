#!/usr/bin/env python3
"""Scan astrophotography captures and generate a sky coverage map."""

from planner.scanner import scan_all
from planner.skymap import build_skymap, print_summary
from planner.skymap_static import build_skymap_png


def main():
    records = scan_all()
    print_summary(records)
    build_skymap(records, output_path="skymap.html")
    build_skymap_png(records, output_path="skymap.png")


if __name__ == "__main__":
    main()
