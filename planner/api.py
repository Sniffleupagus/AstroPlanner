"""FastAPI application — serves capture data and the static frontend."""

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from planner.scanner import scan_all
from planner.capture_cache import CaptureCache, find_db
from planner.horizon_mask import HorizonMask
from planner.visibility import compute_visibility

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


@app.get("/api/targets")
def get_targets():
    if not _TARGETS_FILE.exists():
        return {"targets": []}
    with open(_TARGETS_FILE) as f:
        return json.load(f)


@app.get("/api/visibility")
def get_visibility(
    targets: str = Query("all", description="Comma-separated target IDs, or 'all'"),
    horizon: Optional[str] = Query(None, description="Horizon filename in masks/"),
    time: Optional[str] = Query(None, description="ISO datetime (default: now UTC)"),
):
    if not _TARGETS_FILE.exists():
        return []
    with open(_TARGETS_FILE) as f:
        all_targets = json.load(f).get("targets", [])

    if targets != "all":
        wanted = {t.strip() for t in targets.split(",")}
        all_targets = [t for t in all_targets if t["id"] in wanted]

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


# Static SPA — mounted last so /api/* routes always take priority
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
