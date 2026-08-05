"""Classify a telescope frame as sky vs. obstruction during daytime.

For raw sensor frames (uint16): uses saturation — open sky fully
saturates the sensor at short exposures while obstructions do not.

For RTSP/video frames (uint8 BGR): uses blue-channel dominance and
pixel variance — daytime sky is distinctly blue and uniform, while
obstructions (trees, buildings) are less blue and more textured.
"""

import cv2
import numpy as np


DEFAULT_SKY_BRIGHT = 0.8
DEFAULT_SKY_FRACTION = 0.92
DEFAULT_BLUE_RATIO = 0.50
DEFAULT_MAX_VARIANCE = 0.01


def debayer(pixels: np.ndarray) -> np.ndarray:
    """Demosaic a raw Bayer frame to RGB. Passes through non-Bayer frames."""
    if pixels.ndim == 2:
        # Seestar S50 IMX462 uses GBRG Bayer pattern
        return cv2.cvtColor(pixels, cv2.COLOR_BayerGB2RGB)
    return pixels


def classify_frame(pixels: np.ndarray,
                   sky_bright: float = DEFAULT_SKY_BRIGHT,
                   sky_fraction: float = DEFAULT_SKY_FRACTION,
                   blue_ratio_thresh: float = DEFAULT_BLUE_RATIO,
                   max_variance: float = DEFAULT_MAX_VARIANCE) -> dict:
    """Classify a frame as sky or obstruction.

    Automatically picks the right strategy based on dtype:
    - uint16 (raw sensor): saturation-based (bright_fraction >= sky_fraction)
    - uint8 (RTSP video): blue ratio + variance (sky is blue and uniform)

    Returns
    -------
    dict with is_sky, brightness, bright_fraction, dark_fraction,
    blue_ratio, variance, and the thresholds used.
    """
    if pixels.dtype == np.uint16:
        norm = pixels.astype(np.float64) / 65535.0
        brightness = norm.mean()
        bright_frac = (norm > sky_bright).mean()
        dark_frac = (norm < 0.3).mean()
        is_sky = bright_frac >= sky_fraction
        return {
            "is_sky": is_sky,
            "brightness": brightness,
            "bright_fraction": bright_frac,
            "dark_fraction": dark_frac,
            "blue_ratio": None,
            "variance": None,
            "sky_bright": sky_bright,
            "sky_fraction": sky_fraction,
        }

    # uint8 RTSP path — BGR channel order from OpenCV
    f = pixels.astype(np.float64)
    if pixels.ndim == 3 and pixels.shape[2] == 3:
        b, g, r = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    else:
        b = g = r = f if pixels.ndim == 2 else f[:, :, 0]

    norm = f / 255.0
    brightness = norm.mean()
    bright_frac = (norm > sky_bright).mean()
    dark_frac = (norm < 0.3).mean()

    total = b + g + r + 1e-6
    pixel_blue_ratio = b / total
    blue_ratio = float(pixel_blue_ratio.mean())

    # Per-pixel sky test: blue must be the dominant channel AND exceed
    # the minimum blue ratio.  Foliage has G >> B so these pixels fail.
    sky_pixel = (b > g) & (b > r) & (pixel_blue_ratio >= blue_ratio_thresh)
    sky_pixel_frac = float(sky_pixel.mean())

    gray = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    variance = float(gray.var())

    is_sky = sky_pixel_frac >= sky_fraction and variance < max_variance

    return {
        "is_sky": is_sky,
        "brightness": brightness,
        "bright_fraction": bright_frac,
        "dark_fraction": dark_frac,
        "blue_ratio": blue_ratio,
        "sky_pixel_frac": sky_pixel_frac,
        "variance": variance,
        "sky_bright": sky_bright,
        "sky_fraction": sky_fraction,
    }
