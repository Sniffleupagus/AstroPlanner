"""Load and query horizon mask profiles for visibility calculations."""

import json
from pathlib import Path


class HorizonMask:
    """Altitude-vs-azimuth obstruction profile for an observing site.

    The mask stores the minimum altitude (in degrees) at which the sky
    is visible for sampled azimuths. Queries interpolate between samples.
    """

    def __init__(self, boundary: list[dict], margin: float = 0.0, metadata: dict = None):
        """
        Parameters
        ----------
        boundary : list of dict
            Each dict has "azimuth" (degrees) and "min_altitude" (degrees).
            Must be sorted by azimuth.
        margin : float
            Additional margin already applied to the boundary values.
        metadata : dict
            Optional metadata (location, generation time, etc.)
        """
        self.boundary = sorted(boundary, key=lambda x: x["azimuth"])
        self.margin = margin
        self.metadata = metadata or {}
        self._azimuths = [b["azimuth"] for b in self.boundary]
        self._altitudes = [b["min_altitude"] for b in self.boundary]

    @classmethod
    def from_file(cls, path):
        """Load a mask from a JSON file produced by horizon_scan."""
        with open(path) as f:
            data = json.load(f)
        return cls(
            boundary=data["boundary"],
            margin=data.get("margin_degrees", 0),
            metadata={k: v for k, v in data.items() if k != "boundary"},
        )

    @classmethod
    def flat(cls, altitude=0.0):
        """Create a flat mask (constant altitude at all azimuths)."""
        boundary = [{"azimuth": az, "min_altitude": altitude}
                    for az in range(0, 360, 5)]
        return cls(boundary)

    def min_altitude(self, azimuth: float) -> float:
        """Get the minimum visible altitude at a given azimuth.

        Linearly interpolates between sampled points. Wraps around 360°.
        """
        az = azimuth % 360.0
        n = len(self._azimuths)
        if n == 0:
            return 0.0
        if n == 1:
            return self._altitudes[0]

        # Find bracketing indices
        for i in range(n):
            if self._azimuths[i] > az:
                break
        else:
            i = n

        i_hi = i % n
        i_lo = (i - 1) % n

        az_lo = self._azimuths[i_lo]
        az_hi = self._azimuths[i_hi]
        alt_lo = self._altitudes[i_lo]
        alt_hi = self._altitudes[i_hi]

        # Handle wraparound
        span = (az_hi - az_lo) % 360.0
        if span == 0:
            return alt_lo
        offset = (az - az_lo) % 360.0
        t = offset / span

        return alt_lo + t * (alt_hi - alt_lo)

    def is_visible(self, azimuth: float, altitude: float) -> bool:
        """Check if a point in the sky is above the horizon mask."""
        return altitude >= self.min_altitude(azimuth)

    def summary(self) -> str:
        """Return a brief text summary of the mask."""
        if not self._altitudes:
            return "Empty mask"
        mn = min(self._altitudes)
        mx = max(self._altitudes)
        avg = sum(self._altitudes) / len(self._altitudes)
        return (f"Horizon mask: {len(self.boundary)} points, "
                f"alt range {mn:.1f}°–{mx:.1f}°, mean {avg:.1f}°, "
                f"margin {self.margin}°")
