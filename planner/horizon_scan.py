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
from astropy.coordinates import EarthLocation, AltAz, SkyCoord
from astropy.time import Time
import astropy.units as u

from .sky_detect import classify_frame


DEFAULT_HOST = "10.4.14.165"
DEFAULT_PORT = 4700
STREAM_PORT = 4800

# Legacy alias
SeestarConnection = None  # removed; use SeestarScope

SETTLE_TIMEOUT = 55.0
SETTLE_THRESHOLD = 5.0  # degrees - generous for EQ tracking drift
SLEW_POLL_INTERVAL = 10.0


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

    def start_view(self, ra_hours, dec_deg, name="_horizon_scan"):
        """Start a view session (activates camera and slews)."""
        return self._send("iscope_start_view", {
            "mode": "star",
            "target_ra_dec": [ra_hours, dec_deg],
            "target_name": name,
            "lp_filter": False,
        })

    def goto(self, ra_hours, dec_deg):
        """Slew to RA/Dec within an active view session.

        Retries if scope reports 'equipment is moving'.
        """
        for _ in range(5):
            resp = self._send("scope_goto", [ra_hours, dec_deg])
            if resp and resp.get("code") == 0:
                return resp
            if resp and resp.get("code") == 203:
                time.sleep(3)
                continue
            return resp
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


def wait_for_slew(scope, target_az, target_alt, timeout=SETTLE_TIMEOUT):
    """Poll until the scope settles near the target position.

    Uses altitude convergence as the primary check since azimuth drifts
    from sidereal tracking (especially near zenith).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(SLEW_POLL_INTERVAL)
        try:
            pos = scope.get_horiz_coord()
        except Exception:
            continue
        if pos is None:
            continue
        alt, az = pos  # scope returns [alt, az]
        dalt = abs(alt - target_alt)
        if dalt < SETTLE_THRESHOLD:
            return True
    return False


_last_image_id = 0


def capture_frame(host=DEFAULT_HOST, wait_for_new=True, timeout=20.0):
    """Capture a single frame and return as numpy array.

    Returns (H, W, 3) uint16 if debayered, or (H, W) uint16 for raw Bayer.
    The sky_detect module handles both shapes (Bayer is downsampled 2x2).

    Parameters
    ----------
    wait_for_new : bool
        If True, wait until the stream delivers a frame with a different
        image_id than the last capture (ensures fresh data after a slew).
    timeout : float
        Max seconds to wait for a new frame.
    """
    global _last_image_id
    from seestarpy.stream import parse_header, _decompress_payload, _ZIP_LOCAL_SIG

    deadline = time.time() + timeout

    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.connect((host, STREAM_PORT))
        sock.settimeout(timeout)

        try:
            import json as _json
            msg = _json.dumps({"id": 2, "method": "get_current_img"}) + "\r\n"
            sock.sendall(msg.encode())

            def _recv_exact(n):
                buf = b""
                while len(buf) < n:
                    chunk = sock.recv(min(n - len(buf), 65536))
                    if not chunk:
                        raise ConnectionError("stream socket closed")
                    buf += chunk
                return buf

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
        finally:
            sock.close()

        img_id = header.get("image_id", 0)
        if wait_for_new and img_id == _last_image_id and img_id != 0:
            time.sleep(5)
            continue

        _last_image_id = img_id
        w = header.get("width", 0)
        h = header.get("height", 0)

        if _ZIP_LOCAL_SIG in payload:
            raw = _decompress_payload(payload)
            if len(raw) == h * w * 3 * 2:
                return np.frombuffer(raw, dtype=np.uint16).reshape(h, w, 3)
            elif len(raw) == h * w * 2:
                return np.frombuffer(raw, dtype=np.uint16).reshape(h, w)

        if len(payload) == h * w * 2:
            return np.frombuffer(payload, dtype=np.uint16).reshape(h, w)

        raise RuntimeError(
            f"Cannot decode frame: {len(payload)} bytes, {w}x{h}, "
            f"ZIP={'yes' if _ZIP_LOCAL_SIG in payload else 'no'}"
        )

    raise TimeoutError("No new frame received within timeout")


def binary_search_boundary(scope, az_deg, location, obstime, host=DEFAULT_HOST,
                           alt_min=5.0, alt_max=85.0, precision=1.5):
    """Find the sky/obstruction boundary at a given azimuth via binary search.

    Returns the lowest altitude (in degrees) where sky is visible.
    """
    lo, hi = alt_min, alt_max

    # First check if even the max altitude is obstructed
    ra_h, dec_d = altaz_to_radec(hi, az_deg, location, obstime)
    scope.goto(ra_h, dec_d)
    if not wait_for_slew(scope, az_deg, hi):
        pass  # proceed anyway, position might be close enough
    time.sleep(3)

    try:
        pixels = capture_frame(host)
        result = classify_frame(pixels)
        if not result["is_sky"]:
            return hi  # obstructed all the way up
    except Exception:
        return hi

    # Check if the min altitude is already clear
    ra_h, dec_d = altaz_to_radec(lo, az_deg, location, obstime)
    scope.goto(ra_h, dec_d)
    if not wait_for_slew(scope, az_deg, lo):
        pass
    time.sleep(3)

    try:
        pixels = capture_frame(host)
        result = classify_frame(pixels)
        if result["is_sky"]:
            return lo  # clear all the way down
    except Exception:
        pass

    # Binary search
    while (hi - lo) > precision:
        mid = (lo + hi) / 2.0
        obstime = Time.now()  # refresh time for coord conversion
        ra_h, dec_d = altaz_to_radec(mid, az_deg, location, obstime)
        scope.goto(ra_h, dec_d)
        if not wait_for_slew(scope, az_deg, mid):
            pass
        time.sleep(3)

        try:
            pixels = capture_frame(host)
            result = classify_frame(pixels)
        except Exception:
            hi = mid
            continue

        if result["is_sky"]:
            hi = mid  # boundary is below this point
        else:
            lo = mid  # boundary is above this point

    return hi


def scan_horizon(host=DEFAULT_HOST, coarse_step=15.0, fine_step=5.0,
                 refine_threshold=5.0, margin=5.0, output_path="masks/horizon.json",
                 alt_min=5.0, alt_max=85.0):
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
    """
    scope = SeestarScope(host)
    print("Connecting to Seestar...")

    # Get location and time
    loc_data = scope.get_location()
    if loc_data is None:
        raise RuntimeError("Could not get location from scope")
    lon, lat = loc_data
    location = EarthLocation(lon=lon * u.deg, lat=lat * u.deg, height=50 * u.m)
    print(f"Location: {lat:.4f}N, {lon:.4f}E")

    scope_time = scope.get_time()
    print(f"Scope time: {scope_time}")

    # Start a view session to activate the camera
    obstime = Time.now()
    init_ra, init_dec = altaz_to_radec(45.0, 0.0, location, obstime)
    scope.start_view(init_ra, init_dec)
    print("Camera session started, waiting for activation...")
    time.sleep(8)

    # Wait for AutoGoto to finish (plate-solve will fail in daytime)
    for _ in range(20):
        resp = scope._send("iscope_get_app_state")
        if resp and resp.get("code") == 0:
            view = resp["result"].get("View", {})
            if view.get("stage") == "ContinuousExposure":
                break
        time.sleep(5)

    # Set short exposure for daytime imaging
    scope._send("set_setting", {"exp_ms": {"continuous": 10}})
    time.sleep(2)
    print("Exposure set for daytime scanning")

    # Pass 1: Coarse sweep
    coarse_azimuths = np.arange(0, 360, coarse_step)
    boundaries = {}

    print(f"\n=== Pass 1: Coarse sweep ({len(coarse_azimuths)} positions, {coarse_step}° steps) ===")
    for i, az in enumerate(coarse_azimuths):
        obstime = Time.now()
        print(f"  [{i+1}/{len(coarse_azimuths)}] Az {az:.0f}°...", end=" ", flush=True)
        boundary = binary_search_boundary(scope, az, location, obstime, host,
                                          alt_min, alt_max)
        boundaries[az] = boundary
        print(f"boundary at {boundary:.1f}°")

    # Pass 2: Fill in at fine_step where neighbors disagree
    coarse_sorted = sorted(boundaries.keys())
    fine_azimuths = []
    for i in range(len(coarse_sorted)):
        az1 = coarse_sorted[i]
        az2 = coarse_sorted[(i + 1) % len(coarse_sorted)]
        diff = abs(boundaries[az1] - boundaries[az2])
        if diff > refine_threshold:
            # Fill in between these two
            step = fine_step
            if az2 > az1:
                fill = np.arange(az1 + step, az2, step)
            else:
                fill = np.arange(az1 + step, az1 + (360 - az1 + az2), step) % 360
            for az in fill:
                if az not in boundaries:
                    fine_azimuths.append(az)

    if fine_azimuths:
        print(f"\n=== Pass 2: Fine fill ({len(fine_azimuths)} positions, {fine_step}° steps) ===")
        for i, az in enumerate(fine_azimuths):
            obstime = Time.now()
            print(f"  [{i+1}/{len(fine_azimuths)}] Az {az:.1f}°...", end=" ", flush=True)
            boundary = binary_search_boundary(scope, az, location, obstime, host,
                                              alt_min, alt_max)
            boundaries[az] = boundary
            print(f"boundary at {boundary:.1f}°")
    else:
        print("\n=== Pass 2: Skipped (coarse boundary is smooth) ===")

    # Stop view session
    print("Stopping view session...")
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

    print(f"\n=== Done! Mask written to {out_path} ===")
    print(f"Scanned {len(boundaries)} azimuths, margin={margin}°")
    return mask_data
