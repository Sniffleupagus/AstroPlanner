#!/usr/bin/env python3
"""Point the scope at an Alt/Az, take a photo, save it, and classify it.

Usage:
    python look.py 30 180              # Alt 30°, Az 180° (S)
    python look.py 15 0 --gain 30      # Alt 15°, Az 0° (N), low gain
    python look.py 45 90 --exposure 5  # Alt 45°, Az 90° (E), 5ms
    python look.py 20 270 -o west.jpg  # save to west.jpg instead
"""

import argparse
import time
import sys

import numpy as np
from PIL import Image
from astropy.coordinates import EarthLocation
from astropy.time import Time
import astropy.units as u

from planner.horizon_scan import (
    SeestarScope, altaz_to_radec, capture_frame, wait_for_slew,
    _compass, DEFAULT_HOST,
)
from planner.sky_detect import classify_frame, DEFAULT_SKY_BRIGHT, DEFAULT_SKY_FRACTION


def main():
    parser = argparse.ArgumentParser(
        description="Point scope at Alt/Az, capture frame, classify sky vs obstruction."
    )
    parser.add_argument("alt", type=float, help="Altitude in degrees (0=horizon, 90=zenith)")
    parser.add_argument("az", type=float, help="Azimuth in degrees (0=N, 90=E, 180=S, 270=W)")
    parser.add_argument("-o", "--output", default="horizon.jpg",
                        help="Output image path (default: horizon.jpg)")
    parser.add_argument("--gain", type=int, default=50,
                        help="Sensor gain (default: 50)")
    parser.add_argument("--exposure", type=int, default=10,
                        help="Exposure in ms (default: 10)")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Seestar IP (default: {DEFAULT_HOST})")
    parser.add_argument("--sky-bright", type=float, default=DEFAULT_SKY_BRIGHT,
                        help=f"Per-pixel brightness floor (default: {DEFAULT_SKY_BRIGHT})")
    parser.add_argument("--sky-fraction", type=float, default=DEFAULT_SKY_FRACTION,
                        help=f"Fraction of pixels that must be bright (default: {DEFAULT_SKY_FRACTION})")
    args = parser.parse_args()

    alt, az = args.alt, args.az
    direction = _compass(az)

    print(f"Target: Alt {alt:.1f}° Az {az:.0f}° ({direction})")
    print(f"Camera: gain={args.gain}, exposure={args.exposure}ms")
    print()

    scope = SeestarScope(args.host)

    # Get location
    loc_data = scope.get_location()
    if loc_data is None:
        print("ERROR: Could not get location from scope")
        sys.exit(1)
    lon, lat = loc_data
    location = EarthLocation(lon=lon * u.deg, lat=lat * u.deg, height=50 * u.m)
    print(f"Location: {lat:.4f}°N, {lon:.4f}°E")

    # Configure gain/exposure
    print("Configuring camera...")
    scope.start_view()
    time.sleep(2)
    scope._send("set_control_value", ["gain", args.gain])
    scope._send("set_setting", {"exp_ms": {"continuous": args.exposure}})
    time.sleep(1)

    # Grab a reference frame BEFORE slewing (so we can tell when it changes)
    print("Capturing reference frame (pre-slew)...")
    try:
        ref_frame = capture_frame(args.host, wait_for_new=False, timeout=5)
        ref_hash = ref_frame.tobytes()[:4096]
        print(f"  Got reference frame to compare against")
    except Exception as e:
        ref_hash = None
        print(f"  No reference frame: {e}")

    # Slew to target
    obstime = Time.now()
    ra_h, dec_d = altaz_to_radec(alt, az, location, obstime)
    print(f"Slewing to Alt {alt:.1f}° Az {az:.0f}° ({direction})  "
          f"[RA={ra_h:.3f}h Dec={dec_d:.1f}°]")
    scope.goto(ra_h, dec_d)

    if not wait_for_slew(scope, az, alt):
        print("⚠ Slew may not have fully settled")
    print()

    # Keep grabbing frames until we get one that differs from pre-slew
    print("Capturing frame (waiting for post-slew image)...")
    deadline = time.time() + 15.0
    pixels = None
    attempts = 0
    while time.time() < deadline:
        try:
            frame = capture_frame(args.host, wait_for_new=False, timeout=5)
        except Exception as e:
            print(f"  Frame grab failed: {e}")
            time.sleep(1)
            continue
        attempts += 1
        if ref_hash is None or frame.tobytes()[:4096] != ref_hash:
            pixels = frame
            if attempts > 1:
                print(f"  Got fresh frame after {attempts} attempts")
            break
        print(f"  Still getting pre-slew image, retrying... ({attempts})", flush=True)
        time.sleep(1)
    else:
        print(f"  ⚠ Could not get a new frame, using latest anyway")
        pixels = frame

    if pixels is None:
        print("ERROR: No frame captured")
        scope.stop_view()
        sys.exit(1)

    # Save as JPEG (capture_frame already debayers to RGB)
    if pixels.dtype == np.uint16:
        img8 = (pixels / 256).astype(np.uint8)
    else:
        img8 = pixels

    pil_img = Image.fromarray(img8, mode="RGB")

    pil_img.save(args.output, quality=90)
    import os, hashlib
    fsize = os.path.getsize(args.output)
    fhash = hashlib.md5(pixels.tobytes()[:4096]).hexdigest()[:8]
    print(f"Saved: {args.output} ({pil_img.size[0]}x{pil_img.size[1]}, "
          f"{fsize/1024:.0f}KB, hash={fhash})")
    print()

    # Classify
    result = classify_frame(pixels, sky_bright=args.sky_bright,
                            sky_fraction=args.sky_fraction)
    brightness = result["brightness"]
    bright_frac = result["bright_fraction"]
    dark_frac = result["dark_fraction"]
    is_sky = result["is_sky"]

    verdict = "SKY ☀" if is_sky else "OBSTRUCTION ■"
    print(f"Classification: {verdict}")
    print(f"  mean brightness = {brightness:.4f}")
    print(f"  bright pixels   = {bright_frac:.1%}  (need >{args.sky_fraction:.0%} above {args.sky_bright})")
    print(f"  dark pixels     = {dark_frac:.1%}  (below 0.3)")
    print()

    # Extra stats for debugging thresholds
    if pixels.dtype == np.uint16:
        norm = pixels.astype(np.float64) / 65535.0
    else:
        norm = pixels.astype(np.float64) / 255.0

    pmin, pmax = norm.min(), norm.max()
    std = norm.std()
    median = np.median(norm)

    print(f"  Pixel stats:")
    print(f"    min={pmin:.4f}  max={pmax:.4f}  median={median:.4f}  std={std:.4f}")

    if pixels.ndim == 3 and pixels.shape[2] == 3:
        r = pixels[:, :, 0].mean()
        g = pixels[:, :, 1].mean()
        b = pixels[:, :, 2].mean()
        total = r + g + b
        print(f"    color: R={r/total:.0%} G={g/total:.0%} B={b/total:.0%}")

    print()
    scope.stop_view()
    print("Done. View the image:")
    print(f"  xdg-open {args.output}")


if __name__ == "__main__":
    main()
