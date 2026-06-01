#!/usr/bin/env python3
"""CLI entry point for the Seestar horizon scanner.

Usage:
    python scan_horizon.py                    # defaults
    python scan_horizon.py --host 10.4.14.165 --coarse 15 --fine 5 --margin 5
    python scan_horizon.py --output masks/balcony.json
"""

import argparse
from planner.horizon_scan import scan_horizon


def main():
    parser = argparse.ArgumentParser(
        description="Scan the horizon with Seestar S50 to build an obstruction mask."
    )
    parser.add_argument("--host", default="10.4.14.165",
                        help="Seestar IP address (default: 10.4.14.165)")
    parser.add_argument("--coarse", type=float, default=15.0,
                        help="Coarse pass azimuth step in degrees (default: 15)")
    parser.add_argument("--fine", type=float, default=5.0,
                        help="Fine pass azimuth step in degrees (default: 5)")
    parser.add_argument("--margin", type=float, default=5.0,
                        help="Safety margin in degrees added to boundary (default: 5)")
    parser.add_argument("--output", default="masks/horizon.json",
                        help="Output path for mask JSON (default: masks/horizon.json)")
    parser.add_argument("--alt-min", type=float, default=5.0,
                        help="Minimum altitude to search (default: 5)")
    parser.add_argument("--alt-max", type=float, default=85.0,
                        help="Maximum altitude to search (default: 85)")
    parser.add_argument("--start-alt", type=float, default=None,
                        help="Starting altitude hint for first azimuth (default: alt-min). "
                             "Use this if you know your horizon is above a certain altitude "
                             "to skip scanning low obstructions you can already see.")
    parser.add_argument("--gain", type=int, default=50,
                        help="Sensor gain (default: 50). Lower = more dynamic range. "
                             "Increase for overcast until sky saturates.")
    parser.add_argument("--exposure", type=int, default=10,
                        help="Exposure in ms (default: 10). "
                             "Increase for overcast/dim conditions until sky saturates.")
    parser.add_argument("--refine-threshold", type=float, default=5.0,
                        help="Altitude difference threshold to trigger fine pass (default: 5)")

    args = parser.parse_args()

    mask = scan_horizon(
        host=args.host,
        coarse_step=args.coarse,
        fine_step=args.fine,
        refine_threshold=args.refine_threshold,
        margin=args.margin,
        output_path=args.output,
        alt_min=args.alt_min,
        alt_max=args.alt_max,
        start_alt=args.start_alt,
        gain=args.gain,
        exposure_ms=args.exposure,
    )

    from planner.horizon_mask import HorizonMask
    hm = HorizonMask(mask["boundary"], mask["margin_degrees"])
    print(f"\n{hm.summary()}")


if __name__ == "__main__":
    main()
