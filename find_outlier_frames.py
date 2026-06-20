#!/usr/bin/env python3
"""Find frames with bad plate solves in a Siril processing directory.

Reads CRPIX1/CRPIX2 from registered frames (r_pp_lights_*.fit) and flags
any that are far from the median position. One bad plate solve can expand
the stacked canvas by thousands of pixels.

Usage:
  python find_outlier_frames.py Stack_Work/dark_shark_d3
  python find_outlier_frames.py Stack_Work/dark_shark_d3 --max-offset 2.0
  python find_outlier_frames.py Stack_Work/dark_shark_d3 --max-offset 1.5 --unselect
"""

import argparse
import glob
import os
import statistics
import sys

from astropy.io import fits


def load_conversion_map(process_dir: str) -> dict[int, str]:
    """Map frame number -> original filename from lights_conversion.txt."""
    conv_file = os.path.join(process_dir, "lights_conversion.txt")
    mapping = {}
    if not os.path.exists(conv_file):
        return mapping
    with open(conv_file) as f:
        for i, line in enumerate(f, 1):
            parts = line.strip().split("' -> '")
            if parts:
                orig = parts[0].lstrip("'")
                mapping[i] = os.path.basename(orig)
    return mapping


def find_outliers(work_dir: str, max_offset_deg: float | None = None,
                  threshold_sigma: float = 5.0, quiet: bool = False):
    process_dir = os.path.join(work_dir, "process")
    if not os.path.isdir(process_dir):
        print(f"ERROR: No process/ directory in {work_dir}", file=sys.stderr)
        sys.exit(1)

    # Auto-detect registered frames — find the r_* prefix with the most files.
    # Could be r_pp_lights, r_bkg_pp_lights, etc. depending on pipeline steps.
    from collections import Counter
    all_r_fits = sorted(glob.glob(os.path.join(process_dir, "r_*_[0-9]*.fit")))
    if not all_r_fits:
        print(f"ERROR: No registered frames (r_*_*.fit) in {process_dir}", file=sys.stderr)
        sys.exit(1)

    prefixes = Counter()
    for f in all_r_fits:
        base = os.path.basename(f)
        prefix = base.rsplit("_", 1)[0]
        prefixes[prefix] += 1
    best_prefix = prefixes.most_common(1)[0][0]
    reg_files = sorted(f for f in all_r_fits
                       if os.path.basename(f).rsplit("_", 1)[0] == best_prefix)
    if not quiet:
        print(f"Using: {best_prefix}_*.fit ({len(reg_files)} files)")

    conversion = load_conversion_map(process_dir)

    frames = []
    for f in reg_files:
        h = fits.getheader(f, 0)
        cp1 = h.get("CRPIX1")
        cp2 = h.get("CRPIX2")
        if cp1 is None or cp2 is None:
            continue
        basename = os.path.basename(f)
        num = int(basename.rsplit("_", 1)[1].replace(".fit", ""))
        frames.append((num, cp1, cp2, basename))

    if len(frames) < 3:
        if not quiet:
            print(f"Only {len(frames)} frames with WCS — not enough to detect outliers")
        return []

    px_scale = None
    h0 = fits.getheader(reg_files[0], 0)
    cdelt = h0.get("CDELT1") or h0.get("CD1_1")
    if cdelt:
        px_scale = abs(cdelt)

    med1 = statistics.median([f[1] for f in frames])
    med2 = statistics.median([f[2] for f in frames])

    # Use MAD for robust std estimate (stdev is inflated by the outliers)
    mad1 = statistics.median([abs(f[1] - med1) for f in frames])
    mad2 = statistics.median([abs(f[2] - med2) for f in frames])
    robust_std1 = max(mad1 * 1.4826, 1.0)
    robust_std2 = max(mad2 * 1.4826, 1.0)

    if not quiet:
        print(f"Frames: {len(frames)}")
        print(f"Median CRPIX: ({med1:.1f}, {med2:.1f})")
        print(f"Robust StdDev: ({robust_std1:.1f}, {robust_std2:.1f}) px")
        if px_scale:
            print(f"Pixel scale: {px_scale * 3600:.2f} arcsec/px")
        print()

    if max_offset_deg is not None and px_scale is None:
        print("ERROR: No pixel scale in headers, can't use --max-offset", file=sys.stderr)
        sys.exit(1)

    outliers = []
    for num, cp1, cp2, basename in frames:
        dx = cp1 - med1
        dy = cp2 - med2
        offset_px = (dx ** 2 + dy ** 2) ** 0.5
        offset_deg = offset_px * px_scale if px_scale else None

        if max_offset_deg is not None:
            is_outlier = offset_deg is not None and offset_deg > max_offset_deg
        else:
            sigma_x = abs(dx) / robust_std1
            sigma_y = abs(dy) / robust_std2
            is_outlier = max(sigma_x, sigma_y) >= threshold_sigma

        if is_outlier:
            orig = conversion.get(num, "?")
            sigma_x = abs(dx) / robust_std1
            sigma_y = abs(dy) / robust_std2
            outliers.append((num, dx, dy, max(sigma_x, sigma_y), offset_deg, orig))

    if quiet:
        for num, dx, dy, sigma, offset_deg, orig in outliers:
            print(orig)
        return outliers

    if max_offset_deg is not None:
        label = f">{max_offset_deg}° from median"
    else:
        label = f">{threshold_sigma}σ from median"

    if outliers:
        print(f"OUTLIERS ({label}):")
        for num, dx, dy, sigma, offset_deg, orig in sorted(outliers, key=lambda x: -(x[4] or 0)):
            deg_str = f"{offset_deg:.1f}°" if offset_deg else "?"
            print(f"  frame {num:4d}  {deg_str:>7s} offset  ({sigma:.0f}σ)  {orig}")
        print(f"\n{len(outliers)} outlier(s) found out of {len(frames)} frames")
    else:
        print("No outliers found — all frames look well-registered.")

    return outliers


def main():
    parser = argparse.ArgumentParser(description="Find frames with bad plate solves")
    parser.add_argument("work_dir", help="Stack_Work subdirectory (e.g. Stack_Work/dark_shark_d3)")
    parser.add_argument("--max-offset", type=float, default=None,
                        help="Max allowed offset in degrees (e.g. 2.0). Easiest to reason about.")
    parser.add_argument("--threshold", type=float, default=5.0,
                        help="Sigma threshold (default: 5). Used only if --max-offset not given.")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Just print original filenames (for piping to rm/xargs)")
    parser.add_argument("--unselect", action="store_true",
                        help="Print Siril 'unselect' commands to paste into console")
    args = parser.parse_args()

    outliers = find_outliers(args.work_dir, max_offset_deg=args.max_offset,
                             threshold_sigma=args.threshold, quiet=args.quiet)

    if outliers and args.unselect and not args.quiet:
        print("\nSiril console commands to exclude these frames:")
        for num, *_ in outliers:
            print(f"  unselect {num}")


if __name__ == "__main__":
    main()
