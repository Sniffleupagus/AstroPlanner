"""Classify a telescope frame as sky vs. obstruction during daytime.

Uses a saturation-based approach: at short exposures, open sky fully
saturates the sensor while obstructions (trees, buildings) do not.
Adjust --gain and --exposure until sky saturates for your conditions.
"""

import cv2
import numpy as np


DEFAULT_SKY_BRIGHT = 0.8
DEFAULT_SKY_FRACTION = 0.95


def debayer(pixels: np.ndarray) -> np.ndarray:
    """Demosaic a raw Bayer frame to RGB. Passes through non-Bayer frames."""
    if pixels.ndim == 2:
        # Seestar S50 IMX462 uses GBRG Bayer pattern
        return cv2.cvtColor(pixels, cv2.COLOR_BayerGB2RGB)
    return pixels


def classify_frame(pixels: np.ndarray,
                   sky_bright: float = DEFAULT_SKY_BRIGHT,
                   sky_fraction: float = DEFAULT_SKY_FRACTION) -> dict:
    """Classify a frame as sky or obstruction.

    A pixel is "bright" if its normalized value exceeds sky_bright.
    The frame is "sky" if at least sky_fraction of pixels are bright.

    Parameters
    ----------
    pixels : np.ndarray
        Image array — (H, W, 3) uint16 RGB or (H, W) uint16 raw Bayer.
    sky_bright : float
        Per-pixel brightness floor (default 0.8).
    sky_fraction : float
        Fraction of pixels that must exceed sky_bright (default 0.95).

    Returns
    -------
    dict with keys:
        is_sky: bool
        brightness: float - mean brightness (0-1)
        bright_fraction: float - fraction of pixels above sky_bright
        dark_fraction: float - fraction of pixels below 0.3
        sky_bright: float - the per-pixel threshold used
        sky_fraction: float - the fraction threshold used
    """
    if pixels.dtype == np.uint16:
        norm = pixels.astype(np.float64) / 65535.0
    else:
        norm = pixels.astype(np.float64) / 255.0

    brightness = norm.mean()
    bright_frac = (norm > sky_bright).mean()
    dark_frac = (norm < 0.3).mean()

    is_sky = bright_frac >= sky_fraction

    return {
        "is_sky": is_sky,
        "brightness": brightness,
        "bright_fraction": bright_frac,
        "dark_fraction": dark_frac,
        "sky_bright": sky_bright,
        "sky_fraction": sky_fraction,
    }
