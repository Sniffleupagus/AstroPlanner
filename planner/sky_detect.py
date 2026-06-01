"""Classify a telescope frame as sky vs. obstruction during daytime.

Uses a saturation-based approach: at short exposures, open sky fully
saturates the sensor while obstructions (trees, buildings) do not.
Adjust --gain and --exposure until sky saturates for your conditions.
"""

import numpy as np


def classify_frame(pixels: np.ndarray) -> dict:
    """Classify a frame as sky (saturated) or obstruction (not saturated).

    Parameters
    ----------
    pixels : np.ndarray
        Image array — either (H, W, 3) uint16 RGB or (H, W) uint16 raw Bayer.

    Returns
    -------
    dict with keys:
        is_sky: bool - True if frame is saturated (open sky)
        brightness: float - mean brightness (0-1 normalized to sensor max)
        saturated: bool - same as is_sky
    """
    if pixels.dtype == np.uint16:
        brightness = pixels.mean() / 65535.0
    else:
        brightness = pixels.mean() / 255.0

    is_sky = brightness > 0.95

    return {
        "is_sky": is_sky,
        "brightness": brightness,
        "saturated": is_sky,
    }
