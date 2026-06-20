"""FastAPI application — serves capture data and the static frontend."""

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import yaml
from astropy.io import fits as afits
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from planner.scanner import scan_all
from planner.capture_cache import CaptureCache, find_db
from planner.horizon_mask import HorizonMask
from planner.target_resolver import resolve_target
from planner.visibility import compute_visibility

log = logging.getLogger(__name__)

# 7827 = STAR on a phone keypad
_ARCHIVE = os.environ.get(
    "ASTROPLANNER_ARCHIVE",
    "/mnt/zarchive/Pictures/Astrophotography",
)
_HORIZON_MASK = os.environ.get("ASTROPLANNER_MASK", "masks/horizon.json")
_MASKS_DIR = Path(os.environ.get("ASTROPLANNER_MASKS_DIR", "masks"))
_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="AstroPlanner", version="0.1.0")


def _load_captures() -> list[dict]:
    db = find_db(_ARCHIVE)
    if db:
        cache = CaptureCache(db, read_only=True)
        records = cache.load_all()
        cache.close()
    else:
        records = scan_all(_ARCHIVE)
    return [asdict(r) for r in records]


@app.get("/api/captures")
def get_captures(
    start: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
):
    records = _load_captures()
    if start:
        records = [r for r in records if (r["date_obs"] or "")[:10] >= start]
    if end:
        records = [r for r in records if (r["date_obs"] or "")[:10] <= end]
    return records


@app.get("/api/horizon")
def get_horizon():
    mask_path = Path(_HORIZON_MASK)
    if not mask_path.exists():
        return {"boundary": [], "lat": None, "lon": None}
    with open(mask_path) as f:
        data = json.load(f)
    loc = data.get("location", {})
    return {
        "boundary": data.get("boundary", []),
        "lat": loc.get("lat"),
        "lon": loc.get("lon"),
    }


@app.get("/api/horizons")
def get_horizons():
    results = []
    for p in sorted(_MASKS_DIR.glob("horizon*.json")):
        with open(p) as f:
            data = json.load(f)
        loc = data.get("location", {})
        results.append({
            "filename": p.name,
            "name": data.get("name", p.stem),
            "boundary": data.get("boundary", []),
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
        })
    return results


_TARGETS_FILE = Path("targets.json")
_EXTRA_TARGETS_FILE = os.environ.get("ASTROPLANNER_EXTRA_TARGETS", "")


def _load_extra_targets() -> list[dict]:
    """Load targets from a GMNJ-style YAML (list of catalog names) and resolve them."""
    if not _EXTRA_TARGETS_FILE:
        return []
    path = Path(_EXTRA_TARGETS_FILE)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text())
        names = data.get("targets", [])
    except Exception:
        log.warning("could not read extra targets from %s", path)
        return []

    if _TARGETS_FILE.exists():
        with open(_TARGETS_FILE) as f:
            existing_ids = {t["id"].upper() for t in json.load(f).get("targets", [])}
    else:
        existing_ids = set()

    results = []
    for name in names:
        if name.strip().upper() in existing_ids:
            continue
        try:
            results.append(resolve_target(name))
        except ValueError:
            log.warning("could not resolve extra target %r, skipping", name)
    return results


@app.get("/api/targets")
def get_targets():
    if not _TARGETS_FILE.exists():
        return {"targets": []}
    with open(_TARGETS_FILE) as f:
        return json.load(f)


@app.get("/api/visibility")
def get_visibility(
    targets: str = Query("all", description="Comma-separated target IDs/names, or 'all'"),
    horizon: Optional[str] = Query(None, description="Horizon filename in masks/"),
    time: Optional[str] = Query(None, description="ISO datetime (default: now UTC)"),
    extras: bool = Query(True, description="Include extra targets from GMNJ config"),
):
    if _TARGETS_FILE.exists():
        with open(_TARGETS_FILE) as f:
            known_targets = json.load(f).get("targets", [])
    else:
        known_targets = []

    known_by_id = {t["id"].upper(): t for t in known_targets}

    if targets == "all":
        all_targets = list(known_targets)
        if extras:
            all_targets.extend(_load_extra_targets())
    else:
        all_targets = []
        for name in targets.split(","):
            name = name.strip()
            if not name:
                continue
            matched = known_by_id.get(name.upper())
            if matched:
                all_targets.append(matched)
            else:
                try:
                    all_targets.append(resolve_target(name))
                except ValueError as e:
                    log.warning("skipping unresolvable target %r: %s", name, e)

    if horizon:
        mask_path = _MASKS_DIR / horizon
    else:
        files = sorted(_MASKS_DIR.glob("horizon*.json"))
        mask_path = files[0] if files else None

    if not mask_path or not mask_path.exists():
        raise HTTPException(status_code=404, detail="Horizon file not found")

    mask = HorizonMask.from_file(mask_path)
    with open(mask_path) as f:
        loc = json.load(f).get("location", {})
    lat, lon = loc.get("lat"), loc.get("lon")
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="Horizon file missing location")

    t0 = None
    if time:
        t0 = datetime.fromisoformat(time)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)

    results = []
    for t in all_targets:
        vis = compute_visibility(t["ra"], t["dec"], mask, lat, lon, t0)
        vis["id"] = t["id"]
        vis["name"] = t.get("name", t["id"])
        vis["ra"] = t["ra"]
        vis["dec"] = t["dec"]
        results.append(vis)

    return results


@app.get("/api/thumbnail")
def get_thumbnail(path: str):
    """Serve the first JPEG/PNG found in a session directory."""
    target = Path(path).resolve()
    archive = Path(_ARCHIVE).resolve()
    if not str(target).startswith(str(archive)):
        raise HTTPException(status_code=403, detail="Path not in archive")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    for f in sorted(target.iterdir()):
        if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
            return FileResponse(str(f))
    raise HTTPException(status_code=404, detail="No thumbnail found")


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".html"}


@app.get("/files/{filename:path}")
def serve_root_file(filename: str):
    """Serve .jpg/.png/.html files from the project root."""
    target = (_PROJECT_ROOT / filename).resolve()
    if not str(target).startswith(str(_PROJECT_ROOT)):
        raise HTTPException(status_code=403, detail="Forbidden")
    if target.suffix.lower() not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(target))


_STACKS_DIR = Path(os.environ.get("ASTROPLANNER_STACKS", "Stack_Work"))
_STACK_EXTENSIONS = {".fit", ".fits", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


@app.get("/api/stacks")
def get_stacks():
    if not _STACKS_DIR.is_dir():
        return []
    result = []
    for subdir in sorted(_STACKS_DIR.iterdir()):
        if not subdir.is_dir():
            continue
        files = []
        for f in sorted(subdir.iterdir()):
            if f.is_file() and f.suffix.lower() in _STACK_EXTENSIONS:
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "ext": f.suffix.lower(),
                })
        if files:
            result.append({"dir": subdir.name, "files": files})
    return result


@app.get("/api/stacks/file/{subdir}/{filename}")
def get_stack_file(subdir: str, filename: str):
    target = (_STACKS_DIR / subdir / filename).resolve()
    if not str(target).startswith(str(_STACKS_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if target.suffix.lower() not in _STACK_EXTENSIONS:
        raise HTTPException(status_code=403, detail="File type not allowed")
    return FileResponse(str(target), filename=filename)


def _render_fits(path: Path, max_dim: int = 1600,
                  stretch_sigma: float = 25, stretch_softness: float = 0.1) -> BytesIO:
    with afits.open(str(path), memmap=True) as hdul:
        data = hdul[0].data

    if data is None:
        raise HTTPException(status_code=400, detail="No image data in FITS")

    data = np.array(data, dtype=np.float64)
    data = np.nan_to_num(data, nan=0.0)

    color = False
    if data.ndim == 3:
        if data.shape[0] in (3, 4):
            data = np.moveaxis(data[:3], 0, -1)
            color = True
        elif data.shape[2] in (3, 4):
            data = data[:, :, :3]
            color = True
        else:
            data = data[0]

    data = np.flipud(data)

    h, w = data.shape[:2]
    if max(h, w) > max_dim:
        step = int(np.ceil(max(h, w) / max_dim))
        data = data[::step, ::step]

    def _asinh_stretch(arr):
        bg = np.median(arr)
        mad = np.median(np.abs(arr - bg))
        sig = mad * 1.4826
        arr = np.clip((arr - bg) / max(stretch_sigma * sig, 1e-10), 0, 1)
        arr = np.arcsinh(arr / stretch_softness) / np.arcsinh(1.0 / stretch_softness)
        return arr

    if color:
        for c in range(data.shape[2]):
            data[:, :, c] = _asinh_stretch(data[:, :, c])
    else:
        data = _asinh_stretch(data)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    buf = BytesIO()
    plt.imsave(buf, data, format="png", cmap="gray" if not color else None)
    buf.seek(0)
    return buf


@app.get("/api/stacks/preview/{subdir}/{filename}")
def get_stack_preview(
    subdir: str, filename: str,
    sigma: float = Query(25, description="White-point in sky-sigma units"),
    softness: float = Query(0.1, description="Asinh softness (lower = more lift)"),
):
    target = (_STACKS_DIR / subdir / filename).resolve()
    if not str(target).startswith(str(_STACKS_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    if target.suffix.lower() not in {".fit", ".fits"}:
        raise HTTPException(status_code=400, detail="Not a FITS file")
    buf = _render_fits(target, stretch_sigma=sigma, stretch_softness=softness)
    return StreamingResponse(buf, media_type="image/png")


# Static SPA — mounted last so /api/* routes always take priority
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
