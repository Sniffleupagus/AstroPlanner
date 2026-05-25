"""FastAPI application — serves capture data and the static frontend."""

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from planner.scanner import scan_all
from planner.capture_cache import CaptureCache, find_db

# 7827 = STAR on a phone keypad
_ARCHIVE = os.environ.get(
    "ASTROPLANNER_ARCHIVE",
    "/mnt/zarchive/Pictures/Astrophotography",
)
_HORIZON_MASK = os.environ.get("ASTROPLANNER_MASK", "masks/horizon.json")
_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="AstroPlanner", version="0.1.0")


def _load_captures() -> list[dict]:
    db = find_db(_ARCHIVE)
    cache = CaptureCache(db, read_only=True) if db else None
    try:
        records = scan_all(_ARCHIVE, cache)
    finally:
        if cache:
            cache.close()
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


# Static SPA — mounted last so /api/* routes always take priority
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
