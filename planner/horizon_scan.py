"""Automated horizon boundary scanner using Seestar S50.

Systematically scans the sky dome to find the lowest unobstructed altitude
at each azimuth, producing a horizon mask for the AstroPlanner recommender.
"""

import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image
from astropy.coordinates import EarthLocation, AltAz, SkyCoord
from astropy.time import Time
import astropy.units as u

from .sky_detect import classify_frame


DEFAULT_HOST = "10.4.14.165"
DEFAULT_PORT = 4700
STREAM_PORT = 4800

# Legacy alias
SeestarConnection = None  # removed; use SeestarScope

SETTLE_THRESHOLD_ALT = 0.5  # degrees
SETTLE_THRESHOLD_AZ = 3.0   # degrees — looser for sidereal tracking drift
SLEW_POLL_INTERVAL = 2.0
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


def wait_for_slew(scope, target_az, target_alt):
    """Poll until the scope settles near the target position.

    Checks both altitude AND azimuth convergence. Keeps waiting as long
    as the scope is making progress. Only times out if the scope stalls
    for SLEW_STALL_TIMEOUT seconds with no improvement.
    """
    started = time.time()
    best_dist = 999.0
    last_progress_time = time.time()

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

        if dalt < SETTLE_THRESHOLD_ALT and daz < SETTLE_THRESHOLD_AZ:
            time.sleep(1)  # let vibrations settle
            print(f"        Scope arrived: Alt {alt:.1f}° Az {az:.0f}° "
                  f"({_fmt_elapsed(elapsed)} slew)")
            return True

        # Combined distance for progress tracking
        dist = dalt + daz
        if dist < best_dist - 0.1:
            best_dist = dist
            last_progress_time = time.time()

        stall_time = time.time() - last_progress_time
        if stall_time > SLEW_STALL_TIMEOUT:
            print(f"        Slew stalled at Alt {alt:.1f}° Az {az:.0f}° "
                  f"(no progress for {SLEW_STALL_TIMEOUT:.0f}s, "
                  f"off by Alt {dalt:.1f}° Az {daz:.0f}°)")
            return False

        print(f"        Slewing... Alt {alt:.1f}° Az {az:.0f}° "
              f"(off by Alt {dalt:.1f}° Az {daz:.0f}°, "
              f"{_fmt_elapsed(elapsed)} elapsed)")


_last_image_id = 0
_last_frame_checksum = None


def capture_frame(host=DEFAULT_HOST, wait_for_new=False, timeout=15.0,
                   verbose=True):
    """Capture a single frame and return as numpy array.

    Keeps a persistent connection to the stream port and reads frames
    until a fresh one arrives (different content from the last capture).

    Returns (H, W, 3) uint16 if debayered, or (H, W) uint16 for raw Bayer.
    """
    global _last_image_id, _last_frame_checksum
    from seestarpy.stream import parse_header, _decompress_payload, _ZIP_LOCAL_SIG

    deadline = time.time() + timeout
    if verbose:
        print(f"        📷 Requesting frame from camera...", flush=True)
    cap_start = time.time()
    frame_count = 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    sock.connect((host, STREAM_PORT))
    sock.settimeout(timeout)

    def _recv_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(min(n - len(buf), 65536))
            if not chunk:
                raise ConnectionError("stream socket closed")
            buf += chunk
        return buf

    try:
        msg = json.dumps({"id": 2, "method": "get_current_img"}) + "\r\n"
        sock.sendall(msg.encode())

        while time.time() < deadline:
            # Sync to frame magic 0x03C3
            while True:
                b = _recv_exact(1)
                if b[0] == 0x03:
                    b2 = _recv_exact(1)
                    if b2[0] == 0xC3:
                        break
                elif b[0] == ord("{"):
                    line = b
                    while not line.endswith(b"\r\n"):
                        line += _recv_exact(1)

            rest = _recv_exact(32)
            header = parse_header(b"\x03\xC3" + rest)
            payload = _recv_exact(header["length"])
            frame_count += 1

            _last_image_id = header.get("image_id", 0)
            w = header.get("width", 0)
            h = header.get("height", 0)

            # Content-based freshness check
            is_saturated = len(payload) < 50 * 1024
            if wait_for_new and _last_frame_checksum is not None and not is_saturated:
                checksum = (len(payload), payload[:1024], payload[-1024:])
                if checksum == _last_frame_checksum:
                    if frame_count <= 10:
                        if frame_count % 3 == 0:
                            print(f"        📷 Waiting for fresh frame... "
                                  f"(frame {frame_count} still stale)", flush=True)
                        continue
                    else:
                        print(f"        📷 ⚠ Frame unchanged after {frame_count} frames — "
                              f"using it anyway.")

            # Save checksum for next comparison (only for non-saturated)
            if not is_saturated:
                _last_frame_checksum = (len(payload), payload[:1024], payload[-1024:])
            else:
                _last_frame_checksum = None

            cap_elapsed = time.time() - cap_start

            if _ZIP_LOCAL_SIG in payload:
                raw = _decompress_payload(payload)
                if len(raw) == h * w * 3 * 2:
                    if verbose:
                        print(f"        📷 Frame received: {w}x{h} RGB, "
                              f"{len(payload)/1024:.0f}KB compressed ({cap_elapsed:.1f}s)")
                    return np.frombuffer(raw, dtype=np.uint16).reshape(h, w, 3)
                elif len(raw) == h * w * 2:
                    if verbose:
                        print(f"        📷 Frame received: {w}x{h} Bayer, "
                              f"{len(payload)/1024:.0f}KB compressed ({cap_elapsed:.1f}s)")
                    return np.frombuffer(raw, dtype=np.uint16).reshape(h, w)

            if len(payload) == h * w * 2:
                if verbose:
                    print(f"        📷 Frame received: {w}x{h} raw, "
                          f"{len(payload)/1024:.0f}KB ({cap_elapsed:.1f}s)")
                return np.frombuffer(payload, dtype=np.uint16).reshape(h, w)

            raise RuntimeError(
                f"Cannot decode frame: {len(payload)} bytes, {w}x{h}, "
                f"ZIP={'yes' if _ZIP_LOCAL_SIG in payload else 'no'}"
            )
    except KeyboardInterrupt:
        raise
    finally:
        sock.close()

    raise TimeoutError("No new frame received within timeout")


def _log_classify(result, alt, az, context=""):
    """Print a human-readable summary of a frame classification."""
    is_sky = result.get("is_sky")
    bright = result.get("brightness", 0)
    verdict = "SKY ☀" if is_sky else "OBSTRUCTION ■"
    sat = " (saturated)" if is_sky else ""
    ctx = f" {context}" if context else ""
    print(f"        Frame{ctx} → {verdict}  brightness={bright:.2f}{sat}")


def find_boundary(scope, az_deg, location, obstime, host=DEFAULT_HOST,
                  alt_min=5.0, alt_max=85.0, step_size=3.0,
                  start_alt=None, confirm_count=2):
    """Find the sky/obstruction boundary by stepping up from a starting altitude.

    Uses small incremental steps (no big jumps) to avoid triggering meridian
    flips in EQ mode. Requires multiple consecutive sky readings to confirm
    the boundary (handles holes in tree canopy).

    Returns the lowest altitude (in degrees) where confirmed sky begins.
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
            if img8.ndim == 2:
                pil_img = Image.fromarray(img8, mode="L")
            else:
                pil_img = Image.fromarray(img8, mode="RGB")
            pil_img.save("horizon_scan.jpg", quality=85)
        except Exception:
            pass

    def _check_alt(alt, label=""):
        nonlocal obstime
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
            print(f"        ⚠ Slew may not have fully settled")

        # Capture frames until we get one different from pre-slew
        pixels = None
        deadline = time.time() + 15.0
        attempts = 0
        while time.time() < deadline:
            try:
                frame = capture_frame(host, wait_for_new=False, timeout=5,
                                      verbose=False)
            except Exception as e:
                print(f"        Frame grab failed: {e}")
                time.sleep(1)
                continue
            attempts += 1
            if ref_hash is None or frame.tobytes()[:4096] != ref_hash:
                pixels = frame
                bright = pixels.mean() / (65535.0 if pixels.dtype == np.uint16 else 255.0)
                sz = "saturated" if bright > 0.95 else f"{len(frame.tobytes())/1024:.0f}KB"
                wait_note = f" (waited {attempts}s)" if attempts > 1 else ""
                print(f"        📷 Fresh frame captured ({sz}){wait_note}")
                break
            if attempts == 1:
                print(f"        📷 Waiting for fresh frame...", end="", flush=True)
            elif attempts % 5 == 0:
                print(f" {attempts}s...", end="", flush=True)
            time.sleep(1)
        else:
            print(f"        📷 ⚠ Could not get new frame, using latest")
            pixels = frame

        if pixels is not None:
            result = classify_frame(pixels)
            _save_preview(pixels)
        else:
            print(f"        Frame capture failed, assuming obstruction")
            result = {"is_sky": False}

        _log_classify(result, alt, az_deg, label)
        return result

    result = _check_alt(current, "start")

    if result["is_sky"]:
        # Already sky — step DOWN to find where obstruction starts
        print(f"      Sky at start — stepping down to find obstruction...")
        sky_streak = 1
        while current > alt_min:
            current -= step_size
            current = max(current, alt_min)
            result = _check_alt(current, "stepping down")
            if not result["is_sky"]:
                # Found obstruction. Boundary is one step above.
                boundary = current + step_size
                print(f"      → Boundary found: obstruction at {current:.1f}°, "
                      f"sky confirmed above {boundary:.1f}°")
                return boundary
        # Hit the bottom — sky all the way down
        print(f"      → Sky visible all the way to {alt_min:.1f}°!")
        return alt_min
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
            if result["is_sky"]:
                sky_streak += 1
                if sky_streak >= confirm_count:
                    # Confirmed! Boundary is where the streak started.
                    boundary = current - (sky_streak - 1) * step_size
                    print(f"      → Boundary confirmed: {sky_streak} consecutive sky "
                          f"readings, sky starts at {boundary:.1f}°")
                    return boundary
                else:
                    print(f"        ({sky_streak}/{confirm_count} consecutive sky "
                          f"readings needed to confirm — could be hole in tree)")
            else:
                if sky_streak > 0:
                    print(f"        (streak broken — was probably a gap in foliage)")
                sky_streak = 0

        # Hit the top
        if sky_streak > 0:
            boundary = current - (sky_streak - 1) * step_size
            print(f"      → Reached {alt_max:.1f}°, boundary at {boundary:.1f}°")
            return boundary
        print(f"      → ⚠ No sky found up to {alt_max:.1f}°!")
        return alt_max


def scan_horizon(host=DEFAULT_HOST, coarse_step=15.0, fine_step=5.0,
                 refine_threshold=5.0, margin=5.0, output_path="masks/horizon.json",
                 alt_min=5.0, alt_max=85.0, start_alt=None,
                 gain=50, exposure_ms=10):
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
    """
    scan_start = time.time()
    scope = SeestarScope(host)

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
    print(f"  Sky detection: saturated (brightness>0.95) = sky, else = obstruction")
    print(f"  Tip: increase --gain or --exposure if sky doesn't fully saturate")
    print(f"  Preview: watch horizon_scan.jpg for the latest captured frame")
    print()

    # Pass 1: Coarse sweep
    coarse_azimuths = np.arange(0, 360, coarse_step)
    boundaries = {}
    prev_boundary = start_alt

    print("=" * 60)
    print(f"  PASS 1: Coarse sweep — {len(coarse_azimuths)} directions, "
          f"every {coarse_step:.0f}°")
    print(f"  Searching altitudes {alt_min:.0f}° to {alt_max:.0f}° at each direction")
    if start_alt is not None:
        print(f"  Starting first azimuth at {start_alt:.0f}° (--start-alt)")
    print("=" * 60)
    print()

    pass1_start = time.time()
    for i, az in enumerate(coarse_azimuths):
        obstime = Time.now()
        direction = _compass(az)
        elapsed = _fmt_elapsed(time.time() - pass1_start)
        print(f"  [{i+1}/{len(coarse_azimuths)}] Azimuth {az:.0f}° ({direction}) "
              f"[elapsed: {elapsed}]")
        boundary = find_boundary(scope, az, location, obstime, host,
                                 alt_min, alt_max,
                                 start_alt=prev_boundary)
        boundaries[az] = boundary
        prev_boundary = boundary
        print(f"    ✓ Horizon at {az:.0f}° ({direction}): sky visible above {boundary:.1f}°")
        print()

    pass1_elapsed = time.time() - pass1_start
    print(f"  Pass 1 complete in {_fmt_elapsed(pass1_elapsed)}")
    lowest = min(boundaries.values())
    highest = max(boundaries.values())
    avg = sum(boundaries.values()) / len(boundaries)
    print(f"  Horizon range: {lowest:.1f}°–{highest:.1f}° (avg {avg:.1f}°)")
    print()

    # Pass 2: Fill in at fine_step where neighbors disagree
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
            boundary = find_boundary(scope, az, location, obstime, host,
                                     alt_min, alt_max,
                                     start_alt=hint)
            boundaries[az] = boundary
            print(f"    ✓ Horizon at {az:.1f}° ({direction}): sky visible above {boundary:.1f}°")
            print()
        print(f"  Pass 2 complete in {_fmt_elapsed(time.time() - pass2_start)}")
    else:
        print("  Pass 2: Skipped (horizon is smooth between all coarse samples)")
    print()

    # Stop view session
    print("Shutting down camera session...")
    scope.stop_view()

    # Build output with margin
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

    # Write output
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(mask_data, f, indent=2)

    total_elapsed = time.time() - scan_start
    print()
    print("=" * 60)
    print("  SCAN COMPLETE")
    print("=" * 60)
    print(f"  Total time:     {_fmt_elapsed(total_elapsed)}")
    print(f"  Directions:     {len(boundaries)} azimuths sampled")
    print(f"  Safety margin:  +{margin}° added to all boundaries")
    print(f"  Output:         {out_path}")
    print()
    print("  Horizon summary (with margin applied):")
    sorted_b = sorted(boundaries.items())
    for az, alt in sorted_b:
        bar_len = int((alt + margin) / 2)
        bar = "█" * bar_len
        print(f"    {az:5.1f}° {_compass(az):>3s}  {alt+margin:5.1f}°  {bar}")
    print()

    return mask_data
