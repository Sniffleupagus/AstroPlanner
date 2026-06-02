#!/usr/bin/env python3
"""CLI entry point for the Seestar horizon scanner.

Usage:
    python scan_horizon.py                          # full 360° scan
    python scan_horizon.py --az-start 90 --az-end 270  # partial scan
    python scan_horizon.py --az 180                 # single azimuth
    python scan_horizon.py --coarse-only --coarse 30   # coarse only, no fine pass
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
    parser.add_argument("--az-start", type=float, default=None,
                        help="Starting azimuth for partial scan (degrees, 0-360)")
    parser.add_argument("--az-end", type=float, default=None,
                        help="Ending azimuth for partial scan (degrees, 0-360)")
    parser.add_argument("--az", type=float, default=None,
                        help="Single azimuth to scan and update (degrees)")
    parser.add_argument("--coarse-only", action="store_true",
                        help="Skip the fine refinement pass")
    parser.add_argument("--sky-bright", type=float, default=None,
                        help="Per-pixel brightness floor for 'bright' classification "
                             "(0-1, default: 0.8). Lower at dusk/dawn.")
    parser.add_argument("--sky-fraction", type=float, default=None,
                        help="Fraction of pixels that must be bright to classify as sky "
                             "(0-1, default: 0.95). Lower to tolerate minor obstructions.")

    args = parser.parse_args()

    kwargs = dict(
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
        az_start=args.az_start,
        az_end=args.az_end,
        az_only=args.az,
        coarse_only=args.coarse_only,
    )
    if args.sky_bright is not None:
        kwargs["sky_bright"] = args.sky_bright
    if args.sky_fraction is not None:
        kwargs["sky_fraction"] = args.sky_fraction
    mask = scan_horizon(**kwargs)

    from planner.horizon_mask import HorizonMask
    hm = HorizonMask(mask["boundary"], mask["margin_degrees"])
    print(f"\n{hm.summary()}")


if __name__ == "__main__":
    main()
