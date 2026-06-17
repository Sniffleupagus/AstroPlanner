"""Automated horizon boundary scanner using Seestar S50.

Systematically scans the sky dome to find the lowest unobstructed altitude
at each azimuth, producing a horizon mask for the AstroPlanner recommender.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image
from astropy.coordinates import EarthLocation, AltAz, SkyCoord
from astropy.time import Time
import astropy.units as u

from .sky_detect import classify_frame, debayer, DEFAULT_SKY_BRIGHT, DEFAULT_SKY_FRACTION


DEFAULT_HOST = "10.4.14.165"
DEFAULT_PORT = 4700
# Legacy alias
SeestarConnection = None  # removed; use SeestarScope

SETTLE_THRESHOLD_ALT = 0.5  # degrees
SETTLE_THRESHOLD_AZ = 4.2   # degrees — looser for sidereal tracking drift
SLEW_POLL_INTERVAL = 4.0
SLEW_STALL_TIMEOUT = 20.0  # give up only if no progress for this long


def _compass(az_deg):
    """Convert azimuth degrees to a compass direction label."""
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(az_deg / 22.5) % 16
    return directions[idx]


def _fmt_elapsed(seconds):
    """Format seconds as a human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


class SeestarScope:
    """Seestar S50 control using per-command sockets (no persistent connection).

    Uses seestarpy's native connection.send_command which opens/authenticates/
    closes a socket for each command. This avoids conflicts with the stream
    socket (the scope has a limited connection pool).
    """

    def __init__(self, host=DEFAULT_HOST):
        self.host = host
        import seestarpy as ssp
        ssp.connection.DEFAULT_IP = host
        ssp.connection.AVAILABLE_IPS = [host]
        ssp.connection.VERBOSE_LEVEL = 0  # suppress raw JSON spam

    def _send(self, method, params=None):
        """Send a command via seestarpy's native send_command.

        Retries once on auth errors (scope connection pool can be momentarily full).
        """
        from seestarpy.connection import send_command
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        for attempt in range(3):
            try:
                return send_command(msg)
            except KeyboardInterrupt:
                raise
            except Exception:
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise

    def get_location(self):
        """Return (lon, lat) from the scope's GPS."""
        resp = self._send("get_user_location")
        if resp and resp.get("code") == 0:
            return resp["result"]
        return None

    def get_horiz_coord(self):
        """Return (alt, az) in degrees."""
        resp = self._send("scope_get_horiz_coord")
        if resp and resp.get("code") == 0:
            return resp["result"]
        return None

    def start_view(self, ra_hours=None, dec_deg=None, name="_horizon_scan"):
        """Start a view session.

        If ra_hours/dec_deg are None, goes straight to ContinuousExposure
        (no plate-solve). Otherwise triggers AutoGoto first.
        """
        params = {
            "mode": "star",
            "target_name": name,
            "lp_filter": False,
        }
        if ra_hours is not None and dec_deg is not None:
            params["target_ra_dec"] = [ra_hours, dec_deg]
        else:
            params["target_ra_dec"] = [None, None]
        return self._send("iscope_start_view", params)

    def goto(self, ra_hours, dec_deg, target_alt=None, target_az=None):
        """Slew to RA/Dec within an active view session.

        Retries if scope reports 'equipment is moving'.
        """
        if target_alt is not None and target_az is not None:
            print(f"        → Goto: Alt {target_alt:.1f}° Az {target_az:.0f}° "
                  f"({_compass(target_az)}) "
                  f"[RA={ra_hours:.3f}h Dec={dec_deg:.1f}°]")
        for attempt in range(5):
            resp = self._send("scope_goto", [ra_hours, dec_deg])
            if resp and resp.get("code") == 0:
                return resp
            if resp and resp.get("code") == 203:
                print(f"        ⏳ Scope busy (still moving), waiting 3s... "
                      f"(retry {attempt+1}/5)")
                time.sleep(3)
                continue
            print(f"        ⚠ Goto returned unexpected code: {resp}")
            return resp
        print(f"        ⚠ Goto failed after 5 retries (scope kept reporting busy)")
        return resp

    def stop_view(self):
        self._send("iscope_stop_view", {"mode": "star"})

    def get_time(self):
        """Get scope's current time as a datetime."""
        resp = self._send("pi_get_time")
        if resp and resp.get("code") == 0:
            r = resp["result"]
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(r.get("time_zone", "UTC"))
            return datetime(r["year"], r["mon"], r["day"],
                           r["hour"], r["min"], r["sec"], tzinfo=tz)
        return datetime.now(timezone.utc)


def altaz_to_radec(alt_deg, az_deg, location, obstime):
    """Convert alt/az to RA/Dec for goto commands.

    Parameters
    ----------
    alt_deg, az_deg : float
        Target altitude and azimuth in degrees.
    location : EarthLocation
        Observer's location.
    obstime : Time
        Current observation time.

    Returns
    -------
    (ra_hours, dec_deg) : tuple of float
    """
    altaz_frame = AltAz(obstime=obstime, location=location)
    coord = SkyCoord(alt=alt_deg * u.deg, az=az_deg * u.deg, frame=altaz_frame)
    icrs = coord.icrs
    return icrs.ra.hour, icrs.dec.deg


def wait_for_slew(scope, target_az, target_alt,
                  thresh_alt=SETTLE_THRESHOLD_ALT,
                  thresh_az=SETTLE_THRESHOLD_AZ):
    """Poll until the scope settles near the target position.

    Checks both altitude AND azimuth convergence. Keeps waiting as long
    as the scope is making progress OR still physically moving. Only
    times out if the scope has truly stopped moving with no improvement.
    """
    started = time.time()
    best_dist = 999.0
    last_progress_time = time.time()
    prev_alt, prev_az = None, None

    while True:
        time.sleep(SLEW_POLL_INTERVAL)
        try:
            pos = scope.get_horiz_coord()
        except Exception:
            continue
        if pos is None:
            continue
        alt, az = pos  # scope returns [alt, az]
        dalt = abs(alt - target_alt)
        daz = min(abs(az - target_az), 360 - abs(az - target_az))
        elapsed = time.time() - started

        if dalt < thresh_alt and daz < thresh_az:
            time.sleep(1)  # let vibrations settle
            print(f"        Scope arrived: Alt {alt:.1f}° Az {az:.0f}° "
                  f"({_fmt_elapsed(elapsed)} slew)")
            return True

        # Combined distance for progress tracking
        dist = dalt + daz
        if dist < best_dist - 0.1:
            best_dist = dist
            last_progress_time = time.time()

        # Also reset stall timer if the scope is still physically moving
        if prev_alt is not None:
            moved_alt = abs(alt - prev_alt)
            moved_az = min(abs(az - prev_az), 360 - abs(az - prev_az))
            if moved_alt > 0.3 or moved_az > 0.3:
                last_progress_time = time.time()
        prev_alt, prev_az = alt, az

        stall_time = time.time() - last_progress_time
        if stall_time > SLEW_STALL_TIMEOUT:
            print(f"        Slew stalled at Alt {alt:.1f}° Az {az:.0f}° "
                  f"(settled, off by Alt {dalt:.1f}° Az {daz:.0f}°)")
            return False

        print(f"        Slewing... Alt {alt:.1f}° Az {az:.0f}° "
              f"(off by Alt {dalt:.1f}° Az {daz:.0f}°, "
              f"{_fmt_elapsed(elapsed)} elapsed)")


def capture_frame(host=DEFAULT_HOST, wait_for_new=False, timeout=15.0,
                   verbose=True):
    """Capture a single frame and return as numpy array.

    Uses seestarpy's get_live_image for reliable frame acquisition with
    proper ack-frame skipping.  If wait_for_new is True, retries up to
    timeout seconds until the frame content changes.

    Returns (H, W, 3) uint16 RGB (Bayer frames are demosaiced automatically).
    """
    from seestarpy.stream import (
        get_live_image, decode_payload, _decompress_payload, _ZIP_LOCAL_SIG,
    )

    cap_start = time.time()
    deadline = cap_start + timeout
    prev_checksum = None
    attempts = 0

    while True:
        if verbose and attempts == 0:
            print(f"        📷 Requesting frame from camera...", flush=True)

        per_call_timeout = min(10.0, max(2.0, deadline - time.time()))
        header, payload = get_live_image(
            ip=host, method="get_current_img",
            fallback=False, read_timeout=per_call_timeout,
        )
        try:
            pixels = decode_payload(payload, header)
        except ValueError:
            # decode_payload assumes ZIP = RGB, but get_current_img can
            # return ZIP-compressed Bayer.  Fall back to manual decode.
            w = header['width']
            h = header['height']
            if _ZIP_LOCAL_SIG in payload:
                raw = _decompress_payload(payload)
                if len(raw) == h * w * 2:
                    pixels = np.frombuffer(raw, dtype=np.uint16).reshape(h, w)
                else:
                    raise
            else:
                raise
        pixels = debayer(pixels)
        attempts += 1
        elapsed = time.time() - cap_start

        if not wait_for_new:
            if verbose:
                w, h = header['width'], header['height']
                print(f"        📷 Frame received: {w}x{h}, "
                      f"{len(payload)/1024:.0f}KB ({elapsed:.1f}s)")
            return pixels

        checksum = (len(payload), payload[:1024], payload[-1024:])
        if prev_checksum is None or checksum != prev_checksum:
            if verbose:
                w, h = header['width'], header['height']
                wait_note = f" (attempt {attempts})" if attempts > 1 else ""
                print(f"        📷 Fresh frame: {w}x{h}, "
                      f"{len(payload)/1024:.0f}KB ({elapsed:.1f}s){wait_note}")
            return pixels

        prev_checksum = checksum
        if time.time() >= deadline:
            if verbose:
                print(f"        📷 ⚠ Frame unchanged after {attempts} attempts — "
                      f"using it anyway.")
            return pixels

        if verbose and attempts % 3 == 0:
            print(f"        📷 Waiting for fresh frame... "
                  f"(attempt {attempts}, {elapsed:.1f}s)", flush=True)
        time.sleep(1)


def _log_classify(result, alt, az, context=""):
    """Print a human-readable summary of a frame classification."""
    is_sky = result.get("is_sky")
    bright = result.get("brightness", 0)
    bright_frac = result.get("bright_fraction", 0)
    dark_frac = result.get("dark_fraction", 0)
    verdict = "SKY ☀" if is_sky else "OBSTRUCTION ■"
    ctx = f" {context}" if context else ""
    print(f"        Frame{ctx} → {verdict}  "
          f"mean={bright:.2f}  bright={bright_frac:.1%}  dark={dark_frac:.1%}")


def find_boundary(scope, az_deg, location, obstime, host=DEFAULT_HOST,
                  alt_min=5.0, alt_max=85.0, step_size=3.0,
                  start_alt=None, confirm_count=2,
                  sky_bright=DEFAULT_SKY_BRIGHT,
                  sky_fraction=DEFAULT_SKY_FRACTION,
                  gain=None, exposure_ms=10):
    """Find the sky/obstruction boundary by stepping up from a starting altitude.

    Uses small incremental steps (no big jumps) to avoid triggering meridian
    flips in EQ mode. Requires multiple consecutive sky readings to confirm
    the boundary (handles holes in tree canopy).

    Returns (boundary_alt, gain) — the lowest altitude where confirmed sky
    begins, and the (possibly adjusted) gain value.
    """
    # Start at hint or low
    if start_alt is not None:
        current = max(alt_min, min(alt_max, start_alt))
        print(f"      Starting at Alt {current:.1f}° (previous boundary hint)")
    else:
        current = alt_min
        print(f"      Starting at Alt {current:.1f}° (bottom of search range)")

    def _save_preview(pixels):
        """Save current frame to horizon_scan.jpg for monitoring."""
        try:
            if pixels.dtype == np.uint16:
                img8 = (pixels / 256).astype(np.uint8)
            else:
                img8 = pixels
            pil_img = Image.fromarray(img8, mode="RGB")
            pil_img.save("horizon_scan.jpg", quality=85)
        except Exception:
            pass

    def _check_alt(alt, label=""):
        nonlocal obstime, gain, exposure_ms
        obstime = Time.now()
        ra_h, dec_d = altaz_to_radec(alt, az_deg, location, obstime)

        # Grab reference frame before slewing (to detect when it changes)
        try:
            ref_frame = capture_frame(host, wait_for_new=False, timeout=5,
                                      verbose=False)
            ref_hash = ref_frame.tobytes()[:4096]
        except Exception:
            ref_hash = None

        # Slew to target
        scope.goto(ra_h, dec_d, target_alt=alt, target_az=az_deg)
        time.sleep(1)
        if not wait_for_slew(scope, az_deg, alt):
            print(f"        ⚠ Slew didn't settle — retrying with 2x margin")
            obstime = Time.now()
            ra_h, dec_d = altaz_to_radec(alt, az_deg, location, obstime)
            scope.goto(ra_h, dec_d, target_alt=alt, target_az=az_deg)
            time.sleep(1)
            if not wait_for_slew(scope, az_deg, alt,
                                 thresh_alt=SETTLE_THRESHOLD_ALT * 2,
                                 thresh_az=SETTLE_THRESHOLD_AZ * 2):
                print(f"        ⚠ Retry slew still not settled, continuing anyway")

        # Get a fresh frame from this position.
        # If camera is stuck (no new frame after slew), it's likely overwhelmed
        # from prior overexposure. Reduce gain, re-slew, retry.
        pixels = None
        max_stuck_retries = 5
        for stuck_attempt in range(max_stuck_retries):
            deadline = time.time() + 45.0
            fetch_attempts = 0
            got_fresh = False
            while time.time() < deadline:
                try:
                    frame = capture_frame(host, wait_for_new=False, timeout=5,
                                          verbose=False)
                except Exception as e:
                    fetch_attempts += 1
                    print(f"        Frame grab failed: {e} (retry {fetch_attempts})")
                    time.sleep(2)
                    continue
                fetch_attempts += 1
                if ref_hash is None or frame.tobytes()[:4096] != ref_hash:
                    pixels = frame
                    got_fresh = True
                    break
                if fetch_attempts == 1:
                    print(f"        📷 Waiting for fresh frame...", end="", flush=True)
                elif fetch_attempts % 5 == 0:
                    print(f" {fetch_attempts}s...", end="", flush=True)
                time.sleep(1)

            if got_fresh:
                break

            # Camera stuck — reduce exposure and/or gain, re-slew, try again
            adjustments = []
            if exposure_ms > 1:
                exposure_ms -= 1
                scope._send("set_setting", {"exp_ms": {"continuous": exposure_ms}})
                adjustments.append(f"exposure→{exposure_ms}ms")
            if gain is not None and gain > 1:
                gain -= 10 if gain > 10 else 1
                scope._send("set_control_value", ["gain", gain])
                adjustments.append(f"gain→{gain}")
            if adjustments:
                print(f"\n        ⚠ Camera stuck — reducing {', '.join(adjustments)}, re-slewing")
            else:
                print(f"\n        ⚠ Camera stuck, gain and exposure at minimum — re-slewing")
            time.sleep(3)
            obstime = Time.now()
            ra_h, dec_d = altaz_to_radec(alt, az_deg, location, obstime)
            scope.goto(ra_h, dec_d, target_alt=alt, target_az=az_deg)
            time.sleep(2)
            wait_for_slew(scope, az_deg, alt)
            ref_hash = None
        else:
            print(f"        ⚠ Could not get fresh frame after "
                  f"{max_stuck_retries} retries")

        if pixels is None:
            print(f"        ⚠ No frame obtained — cannot classify")
            return {"is_sky": None, "failed": True}

        # Classify the frame
        bright = pixels.mean() / (65535.0 if pixels.dtype == np.uint16 else 255.0)
        wait_note = f" (attempt {fetch_attempts})" if fetch_attempts > 1 else ""
        print(f"        📷 Fresh frame captured (mean={bright:.2f}){wait_note}")

        result = classify_frame(pixels, sky_bright=sky_bright,
                                   sky_fraction=sky_fraction)
        _save_preview(pixels)
        _log_classify(result, alt, az_deg, label)

        # If nearly saturated and uniformly bright, preemptively reduce gain
        # for the next capture — camera will likely get stuck otherwise.
        if (bright > 0.95 and result.get("bright_fraction", 0) >= 1.0
                and gain is not None and gain > 1):
            gain -= 10 if gain > 10 else 1
            if exposure_ms > 1:
                exposure_ms -= 1
                scope._send("set_setting", {"exp_ms": {"continuous": exposure_ms}})
            print(f"        💡 Nearly saturated — reducing gain to {gain}, "
                  f"exposure to {exposure_ms}ms for next capture")
            scope._send("set_control_value", ["gain", gain])
        elif (bright < 0.75 and result.get("bright_fraction", 0) >= 1.0
                and gain is not None and gain < 220):
            gain += 5
            exposure_ms += 1
            print(f"        💡 Dim Sky — increasing gain to {gain}, "
                  f"exposure to {exposure_ms}ms for next capture")
            scope._send("set_control_value", ["gain", gain])
            scope._send("set_setting", {"exp_ms": {"continuous": exposure_ms}})
        return result

    result = _check_alt(current, "start")

    if result.get("failed"):
        print(f"      → Cannot get frame at starting altitude — skipping azimuth")
        return None, gain, exposure_ms

    if result["is_sky"]:
        # Already sky — step DOWN to find where obstruction starts
        print(f"      Sky at start — stepping down to find obstruction...")
        sky_streak = 1
        while current > alt_min:
            current -= step_size
            current = max(current, alt_min)
            result = _check_alt(current, "stepping down")
            if result.get("failed"):
                continue
            if not result["is_sky"]:
                # Found obstruction. Boundary is one step above.
                boundary = current + step_size
                print(f"      → Boundary found: obstruction at {current:.1f}°, "
                      f"sky confirmed above {boundary:.1f}°")
                return boundary, gain, exposure_ms
        # Hit the bottom — sky all the way down
        print(f"      → Sky visible all the way to {alt_min:.1f}°!")
        return alt_min, gain, exposure_ms
    else:
        # Obstruction — step UP until we get confirmed sky
        print(f"      Obstructed at start — stepping up to find sky...")
        sky_streak = 0
        step_num = 0
        while current < alt_max:
            current += step_size
            current = min(current, alt_max)
            step_num += 1
            result = _check_alt(current, f"step up #{step_num}")
            if result.get("failed"):
                sky_streak = 0
                continue
            if result["is_sky"]:
                sky_streak += 1
                if sky_streak >= confirm_count:
                    # Confirmed! Boundary is where the streak started.
                    boundary = current - (sky_streak - 1) * step_size
                    print(f"      → Boundary confirmed: {sky_streak} consecutive sky "
                          f"readings, sky starts at {boundary:.1f}°")
                    return boundary, gain, exposure_ms
                else:
                    print(f"        ({sky_streak}/{confirm_count} consecutive sky "
                          f"readings needed to confirm — could be gap in tree)")
            else:
                if sky_streak > 0:
                    print(f"        (streak broken — was probably a gap in foliage)")
                sky_streak = 0

        # Hit the top — no sky found
        if sky_streak > 0:
            boundary = current - (sky_streak - 1) * step_size
            print(f"      → Reached {alt_max:.1f}°, boundary at {boundary:.1f}°")
            return boundary, gain

        # Too dark — increase gain and retry from the top down
        while gain is not None and gain < 220:
            gain += 10
            print(f"      → ⚠ No sky found up to {alt_max:.1f}° — "
                  f"too dark? Increasing gain to {gain}, retrying from top")
            scope._send("set_control_value", ["gain", gain])
            time.sleep(2)
            current = alt_max
            result = _check_alt(current, "retry from top")
            if result.get("failed"):
                print(f"      → Frame capture failed at top with gain {gain}")
                continue
            if result["is_sky"]:
                print(f"      Sky at {alt_max:.1f}° with gain {gain} — "
                      f"stepping down to find obstruction...")
                while current > alt_min:
                    current -= step_size
                    current = max(current, alt_min)
                    result = _check_alt(current, "stepping down (gain adjusted)")
                    if result.get("failed"):
                        break
                    if not result["is_sky"]:
                        boundary = current + step_size
                        print(f"      → Boundary found: obstruction at {current:.1f}°, "
                              f"sky confirmed above {boundary:.1f}°")
                        return boundary, gain, exposure_ms
                if not result.get("failed"):
                    print(f"      → Sky visible all the way to {alt_min:.1f}°!")
                    return alt_min, gain, exposure_ms
            else:
                print(f"      → Still no sky at {alt_max:.1f}° with gain {gain}")

        print(f"      → ⚠ No sky found up to {alt_max:.1f}° even at gain {gain}!")
        return None, gain, exposure_ms


def _load_existing_boundaries(output_path):
    """Load boundaries from an existing horizon JSON file.

    Returns dict of {azimuth: raw_altitude} (margin removed) and the
    margin that was used, or ({}, None) if the file doesn't exist.
    """
    path = Path(output_path)
    if not path.exists():
        return {}, None
    try:
        with open(path) as f:
            data = json.load(f)
        old_margin = data.get("margin_degrees", 0)
        boundaries = {}
        for entry in data.get("boundary", []):
            raw_alt = entry["min_altitude"] - old_margin
            boundaries[entry["azimuth"]] = max(raw_alt, 0)
        return boundaries, old_margin
    except (json.JSONDecodeError, KeyError):
        return {}, None


def _save_boundaries(boundaries, margin, lat, lon, coarse_step, fine_step,
                     output_path):
    """Write the current boundary state to the output JSON."""
    mask_data = {
        "location": {"lat": lat, "lon": lon},
        "generated": datetime.now(timezone.utc).isoformat(),
        "margin_degrees": margin,
        "coarse_step": coarse_step,
        "fine_step": fine_step,
        "boundary": sorted(
            [{"azimuth": az, "min_altitude": min(alt + margin, 90.0)}
             for az, alt in boundaries.items()],
            key=lambda x: x["azimuth"]
        ),
    }
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(mask_data, f, indent=2)
    return mask_data


def scan_horizon(host=DEFAULT_HOST, coarse_step=15.0, fine_step=5.0,
                 refine_threshold=5.0, margin=5.0, output_path="masks/horizon.json",
                 alt_min=5.0, alt_max=85.0, start_alt=None,
                 gain=50, exposure_ms=10,
                 az_start=None, az_end=None, az_only=None,
                 coarse_only=False,
                 sky_bright=DEFAULT_SKY_BRIGHT,
                 sky_fraction=DEFAULT_SKY_FRACTION):
    """Run the full multi-pass horizon scan.

    Parameters
    ----------
    host : str
        Seestar IP address.
    coarse_step : float
        Azimuth step for first pass (degrees).
    fine_step : float
        Azimuth step for second pass (degrees).
    refine_threshold : float
        If adjacent coarse samples differ by more than this, do fine pass.
    margin : float
        Safety margin added to detected boundary (degrees).
    output_path : str
        Where to write the resulting mask JSON.
    alt_min, alt_max : float
        Altitude search range (degrees).
    start_alt : float, optional
        Starting altitude hint for the first azimuth. If you know your
        horizon is around 30°, pass 30 to skip scanning from 5° up.
        Subsequent azimuths use the previous boundary as their hint.
    gain : int
        Sensor gain (default 50). Lower = more dynamic range.
        Increase if sky doesn't saturate (overcast).
    exposure_ms : int
        Exposure time in milliseconds (default 10).
        Increase if sky doesn't saturate.
    az_start : float, optional
        Starting azimuth for partial scan (degrees, 0-360).
    az_end : float, optional
        Ending azimuth for partial scan (degrees, 0-360).
    az_only : float, optional
        Single azimuth to scan and update.
    coarse_only : bool
        If True, skip the fine refinement pass.
    """
    scan_start = time.time()
    scope = SeestarScope(host)

    # Load any existing boundaries so we can merge incrementally
    existing_boundaries, _ = _load_existing_boundaries(output_path)
    if existing_boundaries:
        print(f"  Loaded {len(existing_boundaries)} existing boundary readings "
              f"from {output_path}")

    print("=" * 60)
    print("  HORIZON SCAN — Finding where sky meets obstructions")
    print("=" * 60)
    print()
    print("How this works:")
    print("  The scope steps up in 3° increments at each compass direction,")
    print("  takes a photo, and checks if the sensor is fully saturated.")
    print("  Saturated = open sky (nothing else is that bright).")
    print("  Not saturated = obstruction (tree, building, etc).")
    print("  Requires 2 consecutive sky readings to confirm (handles tree gaps).")
    print()

    print("Connecting to Seestar...")

    # Get location and time
    loc_data = scope.get_location()
    if loc_data is None:
        raise RuntimeError("Could not get location from scope")
    lon, lat = loc_data
    location = EarthLocation(lon=lon * u.deg, lat=lat * u.deg, height=50 * u.m)
    print(f"  Location: {lat:.4f}°N, {lon:.4f}°E")

    scope_time = scope.get_time()
    print(f"  Scope time: {scope_time}")
    print()

    # Start camera and configure gain/exposure
    print("Starting camera...")
    scope.start_view()
    time.sleep(3)
    scope._send("set_control_value", ["gain", gain])
    scope._send("set_setting", {"exp_ms": {"continuous": exposure_ms}})
    time.sleep(1)
    print(f"  Camera: gain={gain}, exposure={exposure_ms}ms")
    print(f"  Sky detection: >{sky_fraction:.0%} of pixels must be brighter than {sky_bright}")
    print(f"  Tip: increase --gain or --exposure if sky doesn't fully saturate")
    print(f"  Preview: watch horizon_scan.jpg for the latest captured frame")
    print()

    # Start with existing data and merge new readings on top
    boundaries = dict(existing_boundaries)
    prev_boundary = start_alt

    # Build azimuth list based on mode
    if az_only is not None:
        coarse_azimuths = np.array([az_only])
        print(f"  Mode: single azimuth {az_only:.0f}° ({_compass(az_only)})")
    else:
        if az_start is not None:
            az_s = az_start
            az_e = az_end if az_end is not None else az_s + 360.0
            if az_s <= az_e:
                coarse_azimuths = np.arange(az_s, az_e + coarse_step / 2, coarse_step)
                coarse_azimuths = coarse_azimuths[coarse_azimuths <= az_e]
            else:
                # Wraps around 0, e.g. 300 to 60
                span = (az_e - az_s) % 360
                coarse_azimuths = (az_s + np.arange(0, span + coarse_step / 2, coarse_step)) % 360
                dists = (coarse_azimuths - az_s) % 360
                coarse_azimuths = coarse_azimuths[dists <= span]
            print(f"  Mode: partial scan Az {az_s:.0f}°–{az_e:.0f}° "
                  f"(start exactly at {az_s:.1f}°, step {coarse_step:.0f}°)")
        elif az_end is not None:
            coarse_azimuths = np.arange(0, az_end + coarse_step / 2, coarse_step)
            coarse_azimuths = coarse_azimuths[coarse_azimuths <= az_end]
            print(f"  Mode: partial scan Az 0°–{az_end:.0f}°")
        else:
            coarse_azimuths = np.arange(0, 360, coarse_step)

    # Pass 1: Coarse sweep
    print("=" * 60)
    print(f"  PASS 1: Coarse sweep — {len(coarse_azimuths)} directions, "
          f"every {coarse_step:.0f}°")
    print(f"  Searching altitudes {alt_min:.0f}° to {alt_max:.0f}° at each direction")
    if start_alt is not None:
        print(f"  Starting first azimuth at {start_alt:.0f}° (--start-alt)")
    if coarse_only:
        print(f"  Coarse only — fine refinement pass disabled")
    print("=" * 60)
    print()

    failed_azimuths = []

    pass1_start = time.time()
    for i, az in enumerate(coarse_azimuths):
        obstime = Time.now()
        direction = _compass(az)
        elapsed = _fmt_elapsed(time.time() - pass1_start)
        print(f"  [{i+1}/{len(coarse_azimuths)}] Azimuth {az:.0f}° ({direction}) "
              f"[elapsed: {elapsed}]")
        boundary, gain, exposure_ms = find_boundary(scope, az, location, obstime, host,
                                       alt_min, alt_max,
                                       start_alt=prev_boundary,
                                       sky_bright=sky_bright,
                                       sky_fraction=sky_fraction,
                                       gain=gain, exposure_ms=exposure_ms)
        if boundary is None:
            failed_azimuths.append((az, direction, "could not determine boundary"))
            print(f"    ✗ Azimuth {az:.0f}° ({direction}): FAILED — skipping")
        else:
            boundaries[az] = boundary
            prev_boundary = boundary
            print(f"    ✓ Horizon at {az:.0f}° ({direction}): sky visible above {boundary:.1f}°")

            # Live update — write after every azimuth
            _save_boundaries(boundaries, margin, lat, lon, coarse_step,
                             fine_step, output_path)
            print(f"    💾 {output_path} updated ({len(boundaries)} azimuths)")
        print()

    pass1_elapsed = time.time() - pass1_start
    print(f"  Pass 1 complete in {_fmt_elapsed(pass1_elapsed)}")
    if boundaries:
        lowest = min(boundaries.values())
        highest = max(boundaries.values())
        avg = sum(boundaries.values()) / len(boundaries)
        print(f"  Horizon range: {lowest:.1f}°–{highest:.1f}° (avg {avg:.1f}°)")
    else:
        print(f"  ⚠ No successful boundary readings!")
    print()

    # Pass 2: Fill in at fine_step where neighbors disagree
    if coarse_only or az_only is not None:
        if coarse_only:
            print("  Pass 2: Skipped (--coarse-only)")
        else:
            print("  Pass 2: Skipped (single azimuth mode)")
    else:
        coarse_sorted = sorted(boundaries.keys())
        fine_azimuths = []
        refine_regions = []
        for i in range(len(coarse_sorted)):
            az1 = coarse_sorted[i]
            az2 = coarse_sorted[(i + 1) % len(coarse_sorted)]
            diff = abs(boundaries[az1] - boundaries[az2])
            if diff > refine_threshold:
                refine_regions.append((az1, az2, diff))
                step = fine_step
                if az2 > az1:
                    fill = np.arange(az1 + step, az2, step)
                else:
                    fill = np.arange(az1 + step, az1 + (360 - az1 + az2), step) % 360
                for az in fill:
                    if az not in boundaries:
                        fine_azimuths.append(az)

        if fine_azimuths:
            fine_azimuths.sort()
            print("=" * 60)
            print(f"  PASS 2: Refining — {len(fine_azimuths)} extra directions")
            print(f"  (filling in where adjacent readings differ by >{refine_threshold}°)")
            print("=" * 60)
            for az1, az2, diff in refine_regions:
                print(f"    {az1:.0f}° ({_compass(az1)}) → {az2:.0f}° ({_compass(az2)}): "
                      f"boundary jumps {diff:.1f}°, needs detail")
            print()

            pass2_start = time.time()
            for i, az in enumerate(fine_azimuths):
                obstime = Time.now()
                direction = _compass(az)
                nearest_coarse = min(coarse_sorted, key=lambda c: min(abs(c - az), 360 - abs(c - az)))
                hint = boundaries[nearest_coarse]
                print(f"  [{i+1}/{len(fine_azimuths)}] Azimuth {az:.1f}° ({direction})")
                boundary, gain, exposure_ms = find_boundary(scope, az, location, obstime, host,
                                               alt_min, alt_max,
                                               start_alt=hint,
                                               sky_bright=sky_bright,
                                               sky_fraction=sky_fraction,
                                               gain=gain, exposure_ms=exposure_ms)
                if boundary is None:
                    failed_azimuths.append((az, direction, "could not determine boundary"))
                    print(f"    ✗ Azimuth {az:.1f}° ({direction}): FAILED — skipping")
                else:
                    boundaries[az] = boundary
                    print(f"    ✓ Horizon at {az:.1f}° ({direction}): sky visible above {boundary:.1f}°")

                    _save_boundaries(boundaries, margin, lat, lon, coarse_step,
                                     fine_step, output_path)
                    print(f"    💾 {output_path} updated ({len(boundaries)} azimuths)")
                print()
            print(f"  Pass 2 complete in {_fmt_elapsed(time.time() - pass2_start)}")
        else:
            print("  Pass 2: Skipped (horizon is smooth between all coarse samples)")
    print()

    # Stop view session
    print("Shutting down camera session...")
    scope.stop_view()

    # Final save
    mask_data = _save_boundaries(boundaries, margin, lat, lon, coarse_step,
                                 fine_step, output_path)

    total_elapsed = time.time() - scan_start
    print()
    print("=" * 60)
    print("  SCAN COMPLETE")
    print("=" * 60)
    print(f"  Total time:     {_fmt_elapsed(total_elapsed)}")
    print(f"  Directions:     {len(boundaries)} azimuths sampled")
    print(f"  Safety margin:  +{margin}° added to all boundaries")
    print(f"  Output:         {output_path}")
    print()
    print("  Horizon summary (with margin applied):")
    sorted_b = sorted(boundaries.items())
    for az, alt in sorted_b:
        bar_len = int((alt + margin) / 2)
        bar = "█" * bar_len
        print(f"    {az:5.1f}° {_compass(az):>3s}  {alt+margin:5.1f}°  {bar}")
    print()

    if failed_azimuths:
        print("=" * 60)
        print(f"  ⚠ FAILED POSITIONS ({len(failed_azimuths)}):")
        print("=" * 60)
        for az, direction, reason in failed_azimuths:
            print(f"    {az:5.1f}° ({direction}): {reason}")
        print()
        print("  These azimuths were NOT written to the output file.")
        print("  Re-run with different gain/exposure or try again later.")
        print()

    return mask_data
