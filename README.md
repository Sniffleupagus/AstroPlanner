# AstroPlanner

Personal astrophotography planning tool. Scans a RAID archive of Seestar S50,
Dwarf 3, and Dwarf mini captures, caches metadata in SQLite, and serves an
interactive sky coverage map at **http://hostname:7827** (STAR on a phone keypad).

## Setup

```bash
python -m venv ~/.venv
~/.venv/bin/pip install astropy plotly fastapi uvicorn aiofiles
```

Set the archive path if it differs from the default:

```bash
export ASTROPLANNER_ARCHIVE=/mnt/zarchive/Pictures/Astrophotography
export ASTROPLANNER_MASK=masks/horizon.json   # optional
```

---

## Tools

### `update_cache.py` — build the metadata cache

Scans the archive and writes a SQLite DB so the server starts fast. Run this
whenever new captures are added.

```bash
# Build/refresh local cache (cache/captures.db):
python update_cache.py --local

# Or write directly to the archive (if it's writable):
python update_cache.py

# Non-default archive path (e.g. on macOS):
python update_cache.py --archive /Volumes/zarchive/Pictures/Astrophotography --local

# Copy local cache to the archive for use on other machines:
scp cache/captures.db user@nas:/path/to/archive/astroplanner_cache.db
```

The cache is keyed on `(file_path, mtime, size)` — only new or changed files are
re-parsed on subsequent runs.

---

### `serve.py` — interactive sky map server

Serves the planning UI at `http://0.0.0.0:7827`.

```bash
python serve.py
```

Or run as a systemd user service so it starts automatically on login:

```bash
systemctl --user enable --now astroplanner.service
systemctl --user restart astroplanner.service   # after config changes
```

**API endpoints** (also browseable at `/docs`):

| Endpoint | Description |
|---|---|
| `GET /api/captures` | All capture records (`?start=YYYY-MM-DD&end=YYYY-MM-DD` to filter) |
| `GET /api/horizon` | Horizon mask boundary + observer lat/lon |
| `GET /api/thumbnail?path=<dir>` | First JPEG/PNG found in a session directory |

The sky map overlays (horizon shadow, sun proximity, horizon line) recalculate
from wall-clock time every 5 minutes in the browser — no server restart needed.

---

### `scan_horizon.py` — map your local horizon

Drives a Seestar S50 over the network to binary-search the sky/obstruction
boundary at each azimuth. Produces `masks/horizon.json`.

```bash
python scan_horizon.py \
    --host 10.4.14.165 \
    --alt-min 35 --alt-max 80 \
    --coarse 15 --fine 5 --margin 5
```

Known-good values: `--alt-min 35 --alt-max 80`. The Seestar won't slew below
~30° in GoTo mode, so don't set `--alt-min` lower than 30.

The resulting `masks/horizon.json` is read automatically by `serve.py` (via
`ASTROPLANNER_MASK`) to shade below-horizon regions and draw the horizon line.

---

### `scan.py` — generate a static skymap (legacy)

Generates `skymap.html` and `skymap.png` without running a server. Useful for
one-off exports or the static PNG.

```bash
python scan.py --mask masks/horizon.json
```

---

## File layout

```
AstroPlanner/
├── serve.py                 # server entry point (port 7827)
├── scan.py                  # static HTML/PNG export (legacy)
├── scan_horizon.py          # Seestar horizon scanner
├── update_cache.py          # build/refresh SQLite cache
├── planner/
│   ├── api.py               # FastAPI routes
│   ├── capture_cache.py     # SQLite cache (read/write)
│   ├── scanner.py           # RAID scan, CaptureRecord
│   ├── skymap.py            # static HTML generator
│   ├── skymap_static.py     # static PNG generator
│   ├── horizon_scan.py      # Seestar control + boundary search
│   ├── horizon_mask.py      # HorizonMask class
│   ├── sky_detect.py        # frame classifier (sky vs obstruction)
│   └── static/
│       └── index.html       # SPA frontend
├── masks/                   # horizon.json (gitignored)
└── cache/                   # local captures.db (gitignored)
```

## Scope connection

- Seestar S50 IP: `10.4.14.165` (local WiFi)
- Command port: 4700, stream port: 4800
- Uses the `seestarpy` library; per-command sockets
