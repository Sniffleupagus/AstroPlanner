"""Automated horizon boundary scanner using Seestar S50.

Systematically scans the sky dome to find the lowest unobstructed altitude
at each azimuth, producing a horizon mask for the AstroPlanner recommender.
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import math
from astropy.coordinates import EarthLocation, AltAz, SkyCoord, get_sun
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
SUN_AVOIDANCE_DEG = 60.0   # minimum angular distance from the sun


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

    def start_view(self, ra_hours=None, dec_deg=None, name="_horizon_scan",
                   mode="star"):
        """Start a view session.

        mode can be "star", "scenery", "moon", or "sun".
        Scenery/moon/sun modes enable RTSP on port 4554.
        """
        params = {
            "mode": mode,
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

    def stop_view(self, mode="star"):
        self._send("iscope_stop_view", {"mode": mode})

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


class FrameStream:
    """RTSP frame grabber using a single persistent connection.

    A reader thread keeps the buffer drained and stashes the latest
    frame. wait_for_new_frame() returns it instantly. If the reader
    detects the stream died, it reconnects automatically. Only one
    RTSP connection is ever open (the Seestar appears to allow only one).

    Requires the Seestar to be in scenery/moon/sun mode.
    """

    RTSP_PORT = 4554

    def __init__(self, host=DEFAULT_HOST):
        import cv2
        self._cv2 = cv2
        self._host = host
        self._url = f"rtsp://{host}:{self.RTSP_PORT}/stream"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame = None
        self._frame_time = 0.0
        self._cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open RTSP stream at {self._url}")
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True)
        self._reader_thread.start()

    def _reader(self):
        cv2 = self._cv2
        last_save = 0
        fail_count = 0
        while not self._stop.is_set():
            cap = self._cap
            if cap is None or not cap.isOpened():
                fail_count += 1
                if fail_count > 3:
                    self._stop.wait(2)
                    fail_count = 0
                try:
                    self._cap = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
                except Exception:
                    self._stop.wait(2)
                continue
            ret, frame = cap.read()
            if not ret:
                fail_count += 1
                if fail_count > 5:
                    try:
                        cap.release()
                    except Exception:
                        pass
                    self._cap = None
                    fail_count = 0
                continue
            fail_count = 0
            now = time.time()
            with self._lock:
                self._frame = frame
                self._frame_time = now
            if now - last_save >= 1.0:
                try:
                    cv2.imwrite("live_scan.jpg", frame,
                                [cv2.IMWRITE_JPEG_QUALITY, 80])
                    last_save = now
                except Exception:
                    pass

    def wait_for_new_frame(self, timeout=10.0, verbose=True):
        """Return the latest frame from the reader thread.

        Returns (pixels, header-dict) or (None, None).
        """
        t0 = time.time()
        deadline = t0 + timeout

        while time.time() < deadline:
            with self._lock:
                if self._frame is not None:
                    age = time.time() - self._frame_time
                    if age < 3.0:
                        frame = self._frame.copy()
                        h, w = frame.shape[:2]
                        elapsed = time.time() - t0
                        if verbose:
                            print(f"        📷 RTSP frame: {w}x{h} "
                                  f"({elapsed:.2f}s)")
                        return frame, {"width": w, "height": h}
            time.sleep(0.2)

        if verbose:
            print(f"        📷 ⚠ No RTSP frame in {timeout:.0f}s")
        return None, None

    def annotate_and_save(self, frame, alt, az, result, path="live_scan.jpg"):
        """Save frame with classification overlay text."""
        cv2 = self._cv2
        img = frame.copy()
        h, w = img.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = w / 600.0
        thick = max(1, int(scale * 2))
        outline = max(1, int(scale * 4))

        verdict = "SKY" if result.get("is_sky") else "OBSTRUCTION"
        blue = result.get("blue_ratio")
        var = result.get("variance")
        lines = [
            f"Alt {alt:.0f}  Az {az:.0f} ({_compass(az)})",
            verdict,
        ]
        if blue is not None:
            lines.append(f"blue={blue:.3f} var={var:.4f}")

        y = int(h * 0.45)
        for line in lines:
            sz = cv2.getTextSize(line, font, scale, thick)[0]
            x = (w - sz[0]) // 2
            cv2.putText(img, line, (x, y), font, scale, (0, 0, 0), outline)
            cv2.putText(img, line, (x, y), font, scale, (255, 255, 255), thick)
            y += int(sz[1] * 1.8)

        cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 85])

    @property
    def latest_jpeg(self):
        """Return the latest frame as JPEG bytes, or None."""
        with self._lock:
            if self._frame is not None:
                _, buf = self._cv2.imencode('.jpg', self._frame,
                                            [self._cv2.IMWRITE_JPEG_QUALITY, 80])
                return buf.tobytes()
        return None

    def stop(self):
        self._stop.set()
        self._reader_thread.join(timeout=5)
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass


def _sun_altaz(location, obstime):
    """Return the sun's (alt_deg, az_deg) for the given location and time."""
    frame = AltAz(obstime=obstime, location=location)
    sun = get_sun(obstime).transform_to(frame)
    return sun.alt.deg, sun.az.deg


def _altaz_separation(alt1, az1, alt2, az2):
    """Angular separation in degrees between two alt/az positions."""
    a1, z1 = math.radians(alt1), math.radians(az1)
    a2, z2 = math.radians(alt2), math.radians(az2)
    cos_d = (math.sin(a1) * math.sin(a2)
             + math.cos(a1) * math.cos(a2) * math.cos(z1 - z2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))


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
    verdict = "SKY" if is_sky else "OBSTRUCTION"
    ctx = f" {context}" if context else ""
    blue = result.get("blue_ratio")
    var = result.get("variance")
    if blue is not None:
        print(f"        Frame{ctx} -> {verdict}  "
              f"mean={bright:.2f}  blue={blue:.3f}  var={var:.4f}")
    else:
        bright_frac = result.get("bright_fraction", 0)
        print(f"        Frame{ctx} -> {verdict}  "
              f"mean={bright:.2f}  bright={bright_frac:.1%}")


def find_boundary(scope, az_deg, location, obstime, host=DEFAULT_HOST,
                  alt_min=5.0, alt_max=85.0, step_size=3.0,
                  start_alt=None, confirm_count=2,
                  sky_bright=DEFAULT_SKY_BRIGHT,
                  sky_fraction=DEFAULT_SKY_FRACTION,
                  gain=None, exposure_ms=10,
                  stream=None):
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
            import cv2 as _cv2
            _cv2.imwrite("horizon_scan.jpg", pixels,
                         [_cv2.IMWRITE_JPEG_QUALITY, 85])
        except Exception:
            pass

    def _check_alt(alt, label=""):
        nonlocal obstime, gain, exposure_ms
        obstime = Time.now()

        sun_alt, sun_az = _sun_altaz(location, obstime)
        if sun_alt > -5:
            sun_dist = _altaz_separation(alt, az_deg, sun_alt, sun_az)
            if sun_dist < SUN_AVOIDANCE_DEG:
                print(f"        ☀ Sun too close ({sun_dist:.0f}° away) — skipping")
                return {"is_sky": None, "sun_avoided": True}

        ra_h, dec_d = altaz_to_radec(alt, az_deg, location, obstime)

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

        # Let auto-exposure settle after slew
        time.sleep(1)

        # Grab frame from RTSP stream (reader thread keeps buffer drained)
        pixels, _hdr = stream.wait_for_new_frame(timeout=10.0)

        if pixels is None:
            print(f"        ⚠ No frame obtained — cannot classify")
            return {"is_sky": None, "failed": True}

        result = classify_frame(pixels, sky_bright=sky_bright,
                                sky_fraction=sky_fraction)
        _save_preview(pixels)
        stream.annotate_and_save(pixels, alt, az_deg, result,
                                 path="horizon_scan.jpg")
        _log_classify(result, alt, az_deg, label)
        return result

    result = _check_alt(current, "start")

    if result.get("sun_avoided"):
        print(f"      → Sun too close at this azimuth — skipping")
        return None, gain, exposure_ms

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
            if result.get("failed") or result.get("sun_avoided"):
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
            if result.get("failed") or result.get("sun_avoided"):
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
            if result.get("failed") or result.get("sun_avoided"):
                print(f"      → Frame capture failed at top with gain {gain}")
                continue
            if result["is_sky"]:
                print(f"      Sky at {alt_max:.1f}° with gain {gain} — "
                      f"stepping down to find obstruction...")
                while current > alt_min:
                    current -= step_size
                    current = max(current, alt_min)
                    result = _check_alt(current, "stepping down (gain adjusted)")
                    if result.get("failed") or result.get("sun_avoided"):
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
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  ⚠ WARNING: Cannot parse {output_path}: {e}")
        print(f"  ⚠ Existing data will NOT be merged. Fix or remove the file to proceed.")
        raise SystemExit(1)


def _save_boundaries(boundaries, margin, lat, lon, coarse_step, fine_step,
                     output_path):
    """Write the current boundary state to the output JSON atomically."""
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
    tmp_path = out_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(mask_data, f, indent=2)
        f.flush()
        import os
        os.fsync(f.fileno())
    if out_path.exists():
        bak_path = out_path.with_suffix(".bak")
        bak_path.unlink(missing_ok=True)
        out_path.rename(bak_path)
    tmp_path.rename(out_path)
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

    obstime_now = Time.now()
    sun_alt, sun_az = _sun_altaz(location, obstime_now)
    if sun_alt > -5:
        print(f"  Sun: Alt {sun_alt:.1f}° Az {sun_az:.0f}° ({_compass(sun_az)}) "
              f"— avoiding pointings within {SUN_AVOIDANCE_DEG:.0f}°")
    else:
        print(f"  Sun: below horizon ({sun_alt:.1f}°) — no avoidance needed")
    print()

    # Start camera in moon mode (enables RTSP while keeping EQ mount,
    # so scope_goto correctly interprets RA/Dec coordinates)
    print("Starting camera in moon mode (RTSP + EQ tracking)...")
    scope.start_view(mode="moon")
    time.sleep(4)

    print("  Autofocus...")
    #scope._send("start_auto_focuse")
    #time.sleep(8)
    print(f"  Sky detection: blue_ratio + variance (auto-exposed video)")
    print(f"  Preview: watch horizon_scan.jpg (annotated) and live_scan.jpg (raw)")
    print()

    # Open RTSP stream with reader thread
    stream = FrameStream(host)
    print(f"  RTSP stream connected (rtsp://{host}:4554/stream)")
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
                                       gain=gain, exposure_ms=exposure_ms,
                                       stream=stream)
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
        def _az_in_scan_range(az):
            """Check if an azimuth falls within the requested scan range."""
            if az_start is None:
                return True
            s = az_start
            e = az_end if az_end is not None else (az_start + 360.0) % 360
            if s <= e:
                return s <= az <= e
            return az >= s or az <= e

        coarse_sorted = sorted(boundaries.keys())
        fine_azimuths = []
        refine_regions = []
        for i in range(len(coarse_sorted)):
            az1 = coarse_sorted[i]
            az2 = coarse_sorted[(i + 1) % len(coarse_sorted)]
            if not (_az_in_scan_range(az1) and _az_in_scan_range(az2)):
                continue
            diff = abs(boundaries[az1] - boundaries[az2])
            if diff > refine_threshold:
                refine_regions.append((az1, az2, diff))
                step = fine_step
                if az2 > az1:
                    fill = np.arange(az1 + step, az2, step)
                else:
                    fill = np.arange(az1 + step, az1 + (360 - az1 + az2), step) % 360
                for az in fill:
                    if az not in boundaries and _az_in_scan_range(az):
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
                                               gain=gain, exposure_ms=exposure_ms,
                                               stream=stream)
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

    # Final save before cleanup (incremental saves already ran, but capture
    # any pass-2 stragglers and ensure the file is current before we touch
    # the camera — cleanup has historically segfaulted in OpenCV)
    mask_data = _save_boundaries(boundaries, margin, lat, lon, coarse_step,
                                 fine_step, output_path)

    # Stop stream and view session
    print("Shutting down camera session...")
    stream.stop()
    scope.stop_view(mode="moon")

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
