"""Classify a telescope frame as sky vs. obstruction during daytime."""

import numpy as np


def classify_frame(pixels: np.ndarray) -> dict:
    """Analyze a frame and return sky/obstruction metrics.

    Parameters
    ----------
    pixels : np.ndarray
        Image array — either (H, W, 3) uint16 RGB or (H, W) uint16 raw Bayer.

    Returns
    -------
    dict with keys:
        is_sky: bool - True if frame shows predominantly open sky
        sky_fraction: float - proportion of frame that looks like sky (0-1)
        brightness: float - mean brightness (0-1 normalized)
        uniformity: float - inverse of coefficient of variation (higher = more uniform)
    """
    if pixels.dtype == np.uint16:
        img = (pixels / 256).astype(np.uint8)
    else:
        img = pixels.astype(np.uint8)

    if img.ndim == 2:
        # Raw Bayer frame: downsample 2x2 to eliminate mosaic pattern artifacts
        h, w = img.shape
        h2, w2 = h // 2 * 2, w // 2 * 2
        img = img[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2).mean(axis=(1, 3)).astype(np.uint8)
        gray = img.astype(np.float64)
        img = np.stack([img, img, img], axis=2)
    else:
        gray = np.mean(img, axis=2)

    brightness = gray.mean() / 255.0

    std = gray.std()
    mean = gray.mean()
    uniformity = mean / (std + 1e-6)

    edge_strength = _sobel_energy(gray)

    sky_fraction = _estimate_sky_fraction(img, gray)

    is_sky = (brightness > 0.3 and uniformity > 3.0 and edge_strength < 0.15
              and sky_fraction > 0.6)

    return {
        "is_sky": is_sky,
        "sky_fraction": sky_fraction,
        "brightness": brightness,
        "uniformity": uniformity,
        "edge_strength": edge_strength,
    }


def _sobel_energy(gray: np.ndarray) -> float:
    """Compute normalized edge energy using simple Sobel-like gradients."""
    gy = np.diff(gray, axis=0)
    gx = np.diff(gray, axis=1)
    energy_y = np.mean(np.abs(gy)) / 255.0
    energy_x = np.mean(np.abs(gx)) / 255.0
    return (energy_y + energy_x) / 2.0


def _estimate_sky_fraction(img: np.ndarray, gray: np.ndarray) -> float:
    """Estimate what fraction of pixels look like sky.

    Sky pixels during daytime: relatively bright, low local variance.
    Works for blue sky, overcast grey, and hazy white sky.
    """
    h, w = gray.shape
    block_h, block_w = max(h // 8, 1), max(w // 8, 1)

    sky_blocks = 0
    total_blocks = 0

    for i in range(0, h - block_h + 1, block_h):
        for j in range(0, w - block_w + 1, block_w):
            block = gray[i:i + block_h, j:j + block_w]
            block_mean = block.mean()
            block_std = block.std()

            is_bright = block_mean > 80
            is_uniform = block_std < 30

            if is_bright and is_uniform:
                sky_blocks += 1
            total_blocks += 1

    return sky_blocks / max(total_blocks, 1)
