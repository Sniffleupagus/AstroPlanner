#!/usr/bin/env python3
"""AstroPlanner server — port 7827 (STAR on a phone keypad).

Run directly:    python serve.py
Or via uvicorn:  uvicorn serve:app --host 127.0.0.1 --port 7827 --reload

Environment:
  ASTROPLANNER_ARCHIVE   path to astrophotography archive
                         (default: /mnt/zarchive/Pictures/Astrophotography)
  ASTROPLANNER_MASK      path to horizon mask JSON
                         (default: masks/horizon.json)
"""
from planner.api import app  # noqa: F401 — uvicorn/gunicorn import 'app' from here

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("serve:app", host="0.0.0.0", port=7827, reload=True)
