"""Compute rise / transit / set times for targets against a horizon mask."""

from datetime import datetime, timezone

import numpy as np
from astropy.coordinates import EarthLocation, AltAz, SkyCoord
from astropy.time import Time
import astropy.units as u

from .horizon_mask import HorizonMask


def _sample_altaz(target_coord, location, t0, hours=24, step_min=2):
    """Sample a target's alt/az over a time window.

    Returns list of (datetime, alt_deg, az_deg) tuples.
    """
    steps = int(hours * 60 / step_min)
    offsets = np.arange(steps) * step_min * u.min
    times = Time(t0) + offsets
    frame = AltAz(obstime=times, location=location)
    altaz = target_coord.transform_to(frame)
    return [
        (times[i].to_datetime(timezone=timezone.utc),
         altaz.alt[i].deg,
         altaz.az[i].deg)
        for i in range(steps)
    ]


def compute_visibility(ra_deg, dec_deg, mask, lat, lon, t0=None):
    """Compute visibility window for a target over a horizon mask.

    Parameters
    ----------
    ra_deg, dec_deg : float
        Target J2000 coordinates in degrees.
    mask : HorizonMask
        Horizon obstruction profile.
    lat, lon : float
        Observer location in degrees.
    t0 : datetime, optional
        Start of the 24-hour window (default: now UTC).

    Returns
    -------
    dict with keys: rise, transit, transit_alt, set, az/alt at t0,
    and a boolean 'visible' for current visibility.
    """
    if t0 is None:
        t0 = datetime.now(timezone.utc)

    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame='icrs')
    location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg)

    samples = _sample_altaz(target, location, t0, hours=24, step_min=2)

    rise_time = None
    set_time = None
    transit_time = None
    transit_alt = -90
    transit_az = 0

    prev_vis = None
    for dt, alt, az in samples:
        vis = alt > 0 and mask.is_visible(az, alt)

        if prev_vis is not None:
            if vis and not prev_vis and rise_time is None:
                rise_time = dt
            elif not vis and prev_vis and rise_time is not None and set_time is None:
                set_time = dt

        if vis and alt > transit_alt:
            transit_alt = alt
            transit_az = az
            transit_time = dt

        prev_vis = vis

    now_dt, now_alt, now_az = samples[0]

    return {
        "alt": round(float(now_alt), 2),
        "az": round(float(now_az), 2),
        "visible": bool(now_alt > 0 and mask.is_visible(now_az, now_alt)),
        "rise": rise_time.isoformat() if rise_time else None,
        "transit": transit_time.isoformat() if transit_time else None,
        "transit_alt": round(float(transit_alt), 2) if transit_time else None,
        "transit_az": round(float(transit_az), 2) if transit_time else None,
        "set": set_time.isoformat() if set_time else None,
    }
