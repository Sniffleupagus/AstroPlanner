# AstroPlanner — Session Handoff

See `ASTRO_PLANNER_HANDOFF.md` for the original architecture spec.

## What exists right now

### Capture scanner (`scan.py`)
Scans RAID at `/mnt/zarchive/Pictures/Astrophotography/{Seestar,Dwarf3,Dwarf-mini}`,
deduplicates stacks, builds a list of `CaptureRecord` objects with RA/Dec, exposure,
filter, scope, target name.

### Interactive skymap (`planner/skymap.py` → `skymap.html`)
Plotly scatter chart: RA (x, reversed 360→0) × DEC (y). Marker size = total exposure.
Run `python scan.py` to regenerate.

**Orientation**: North=top, South=bottom, East=LEFT, West=RIGHT (standard sky-up convention).
Dec=±90° are the top/bottom edges, stretched — they're really single points. Left/right
edges wrap (RA=0h = RA=24h).

**Dynamic overlays** (JavaScript, auto-refresh every 5 min):
- Brown heatmap: below-horizon region from the mask
- Yellow→orange gradient: within 60° of the sun
- Gold dot: sun position
- Dotted line: horizon boundary (in azimuth order, so building overhangs show as upward
  spikes rather than zig-zags)

Pass `--mask masks/horizon.json` (or `--lat`/`--lon`) to `scan.py` to enable overlays.
Location is read from the mask JSON automatically.

### Horizon scanner (`scan_horizon.py`)
Drives the Seestar S50 over the network to binary-search the sky/obstruction boundary
at each azimuth. Produces `masks/horizon.json`.

```
python scan_horizon.py --host 10.4.14.165 --alt-min 35 --alt-max 80 \
                       --coarse 15 --fine 5 --margin 5
```

Known good values from first run: `--alt-min 35 --alt-max 80`.
Seestar won't slew below ~30° in GoTo mode, so don't set alt-min below 30.

Submodules:
- `planner/horizon_scan.py` — SeestarScope class, binary_search_boundary, scan_horizon
- `planner/horizon_mask.py` — HorizonMask: load JSON, interpolate min_altitude(az)
- `planner/sky_detect.py` — classify_frame: is this frame sky or obstruction?

## Next session: warm-start horizon scan

The current `binary_search_boundary()` always starts by slewing to `alt_max` then
`alt_min` before searching, regardless of what the previous azimuth found. Wasteful.

**The better algorithm** (from `planner/NOTES.md`):
- For the first azimuth, start at `(alt_min + alt_max) / 2`
- For each subsequent azimuth, start from the **previous boundary result**
- Capture a frame at that altitude:
  - If sky → scan downward (set `hi = current, lo = alt_min`) until obstruction found
  - If obstruction → scan upward (set `lo = current, hi = alt_max`) until sky found
- Then binary-search the remaining range to `precision` degrees
- This typically finds the answer in 1-3 slews instead of 6-7 for smooth horizon sections

The function signature stays the same; just pass `start_alt` in from the caller loop.

## Feature ideas backlog (from `planner/NOTES.md`)

- **Moon overlay** — same JS approach as sun; add `moonPos()` with phase-weighted glow
- **Target names in tooltips** — M42 → "Orion Nebula" via Sesame/catalog lookup
- **Time scrubber** — slider in the HTML to advance time forward/back so you can preview
  where the horizon and sun will be tonight
- **Thumbnail view on zoom** — when zoomed in, show scaled/rotated FITS or JPEG previews
  at their actual sky position (a "bad full-sky stack" — but just for planning)
- **Unknown target clustering** — group "Unknown" captures by RA/Dec proximity, suggest
  likely targets, allow bulk rename

## Scope connection

- Seestar S50 IP: `10.4.14.165` (local WiFi, not a secret)
- Seestar uses seestarpy library; per-command sockets (not persistent connection)
- Stream port 4800 for live frames; command port 4700
- `test_live_frame.py` — scratch script to verify frame capture and sky_detect

## File layout

```
AstroPlanner/
├── scan.py                  # entry point: scan RAID → skymap.html + skymap.png
├── scan_horizon.py          # entry point: drive Seestar → masks/horizon.json
├── planner/
│   ├── scanner.py           # RAID scan, CaptureRecord
│   ├── skymap.py            # interactive HTML skymap with JS overlays
│   ├── skymap_static.py     # static PNG skymap (matplotlib)
│   ├── horizon_scan.py      # Seestar control + boundary scan logic
│   ├── horizon_mask.py      # HorizonMask class
│   ├── sky_detect.py        # frame classifier (sky vs obstruction)
│   └── NOTES.md             # running ideas / observations
├── masks/                   # horizon.json lives here (gitignored)
└── ASTRO_PLANNER_HANDOFF.md # original architecture spec
```
